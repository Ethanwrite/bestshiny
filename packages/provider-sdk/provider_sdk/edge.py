from __future__ import annotations

import difflib
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from .trust import (
    AssetCriticality,
    ProviderTrustLevel,
    ProviderTrustViolation,
    assert_provider_can_handle,
)


class EdgePolicyViolation(ValueError):
    pass


class EdgeTaskRole(StrEnum):
    PROMPT_DRAFT_REFINEMENT = "PROMPT_DRAFT_REFINEMENT"
    PROMPT_PARAPHRASING = "PROMPT_PARAPHRASING"
    PROMPT_TRANSLATION = "PROMPT_TRANSLATION"
    NEGATIVE_PROMPT_SUGGESTION = "NEGATIVE_PROMPT_SUGGESTION"
    STYLE_VOCABULARY_EXPANSION = "STYLE_VOCABULARY_EXPANSION"
    METADATA_CAPTION_GENERATION = "METADATA_CAPTION_GENERATION"
    ASSET_AUTO_CAPTION = "ASSET_AUTO_CAPTION"
    SEARCH_QUERY_REWRITING = "SEARCH_QUERY_REWRITING"
    LOW_VALUE_SEMANTIC_CLASSIFICATION = "LOW_VALUE_SEMANTIC_CLASSIFICATION"
    NON_CANONICAL_TEST_GENERATION = "NON_CANONICAL_TEST_GENERATION"
    TEMPORARY_PLACEHOLDER_ASSET = "TEMPORARY_PLACEHOLDER_ASSET"
    PROVIDER_INTEGRATION_SMOKE = "PROVIDER_INTEGRATION_SMOKE"


@dataclass(frozen=True)
class EdgeTask:
    task_id: str
    role: EdgeTaskRole
    asset_criticality: AssetCriticality
    estimated_cost_usd: Decimal

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EdgeTask:
        try:
            task_id = str(value["task_id"]).strip()
            role = EdgeTaskRole(str(value["task_role"]))
            criticality = AssetCriticality(str(value["asset_criticality"]))
            estimated = Decimal(str(value["estimated_cost_usd"]))
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise EdgePolicyViolation("invalid or incomplete edge task declaration") from exc
        if not task_id:
            raise EdgePolicyViolation("edge task_id is required")
        if estimated <= 0:
            raise EdgePolicyViolation("edge estimated_cost_usd must be positive")
        return cls(task_id, role, criticality, estimated)


class EdgeTaskPolicy:
    """Hard RunAPI policy; price can never override provider trust."""

    trust_level = ProviderTrustLevel.EDGE

    def authorize(self, task: EdgeTask) -> None:
        try:
            assert_provider_can_handle(self.trust_level, task.asset_criticality)
        except ProviderTrustViolation as exc:
            raise EdgePolicyViolation(str(exc)) from exc
        if task.asset_criticality not in {AssetCriticality.EDGE, AssetCriticality.TEMPORARY}:
            raise EdgePolicyViolation(
                f"RunAPI is restricted to EDGE/TEMPORARY tasks, got {task.asset_criticality.value}"
            )

    def authorize_generation(self, task: EdgeTask) -> None:
        self.authorize(task)
        if task.role not in {
            EdgeTaskRole.NON_CANONICAL_TEST_GENERATION,
            EdgeTaskRole.TEMPORARY_PLACEHOLDER_ASSET,
            EdgeTaskRole.PROVIDER_INTEGRATION_SMOKE,
        }:
            raise EdgePolicyViolation(f"{task.role.value} cannot create media")


class FactLockCategory(StrEnum):
    CHARACTER_IDENTITY = "character_identity"
    CHARACTER_COUNT = "character_count"
    CHARACTER_NAME = "character_name"
    CHARACTER_APPEARANCE = "character_appearance"
    COSTUME_VERSION = "costume_version"
    SCENE_IDENTITY = "scene_identity"
    REQUIRED_PROP = "required_prop"
    DIALOGUE_MEANING = "dialogue_meaning"
    NARRATIVE_EVENT = "narrative_event"
    CAMERA_DIRECTION = "camera_direction"
    PROVIDER_CONSTRAINTS = "provider_constraints"


@dataclass(frozen=True)
class FactLockSet:
    immutable_facts: dict[str, Any]
    required_literals: tuple[str, ...] = ()
    locked_spans: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: dict[str, Any] = {}
        for key, value in self.immutable_facts.items():
            category = FactLockCategory(str(key))
            # Fail at the boundary if the facts are not stable JSON values.
            json.dumps(value, ensure_ascii=False, sort_keys=True)
            normalized[category.value] = value
        object.__setattr__(self, "immutable_facts", normalized)
        object.__setattr__(
            self,
            "required_literals",
            tuple(item.strip() for item in self.required_literals if item.strip()),
        )
        normalized_spans: dict[str, tuple[str, ...]] = {}
        for key, spans in self.locked_spans.items():
            category_value = FactLockCategory(str(key)).value
            if category_value not in normalized:
                raise ValueError(f"locked spans require an immutable fact for {category_value}")
            values = tuple(dict.fromkeys(str(item).strip() for item in spans if str(item).strip()))
            if values:
                normalized_spans[category_value] = values
        object.__setattr__(self, "locked_spans", normalized_spans)

    @property
    def canonical_json(self) -> str:
        return json.dumps(self.immutable_facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def validate(
        self,
        candidate: str,
        echoed_facts: Mapping[str, Any],
        *,
        source_prompt: str | None = None,
    ) -> tuple[bool, list[str]]:
        """Validate both the structured echo and server-owned text anchors.

        A model echoing the original JSON is not evidence that its prose kept
        those facts. Every locked category therefore needs deterministic text
        evidence: an explicit server-owned span, a required literal associated
        with that fact, or a fact value already present in the source prompt.
        Facts that cannot be checked fail closed and the caller keeps the
        original prompt.
        """

        reasons: list[str] = []
        echoed_json = json.dumps(
            dict(echoed_facts), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if echoed_json != self.canonical_json:
            reasons.append("IMMUTABLE_FACTS_CHANGED")
        missing = [literal for literal in self.required_literals if not _contains_literal(candidate, literal)]
        if missing:
            reasons.append("REQUIRED_FACT_LITERAL_MISSING")
        for literal in self.required_literals:
            if not _contains_literal(candidate, literal):
                continue
            source_polarity = _literal_negation_polarity(source_prompt or literal, literal)
            candidate_polarity = _literal_negation_polarity(candidate, literal)
            if source_polarity is None:
                reasons.append("REQUIRED_FACT_LITERAL_POLARITY_UNVERIFIABLE")
            elif candidate_polarity is None or candidate_polarity != source_polarity:
                reasons.append("REQUIRED_FACT_LITERAL_POLARITY_CHANGED")
        for category, value in self.immutable_facts.items():
            if category == FactLockCategory.CHARACTER_COUNT.value:
                expected = _integer_fact(value)
                source_counts = (
                    _character_counts(source_prompt or "") if source_prompt is not None else {expected}
                )
                candidate_counts = _character_counts(candidate)
                if expected is None or expected not in source_counts:
                    reasons.append(f"IMMUTABLE_FACT_UNVERIFIABLE:{category}")
                elif expected not in candidate_counts or any(count != expected for count in candidate_counts):
                    reasons.append(f"IMMUTABLE_FACT_CONTENT_CHANGED:{category}")
                continue

            spans = self._category_spans(category, value, source_prompt)
            if not spans:
                reasons.append(f"IMMUTABLE_FACT_UNVERIFIABLE:{category}")
                continue
            if source_prompt is not None and any(
                not _contains_literal(source_prompt, span) for span in spans
            ):
                reasons.append(f"LOCKED_SPAN_NOT_IN_SOURCE:{category}")
                continue
            if any(not _contains_literal(candidate, span) for span in spans):
                reasons.append(f"IMMUTABLE_FACT_CONTENT_CHANGED:{category}")
                continue
            for span in spans:
                source_polarity = _literal_negation_polarity(source_prompt or span, span)
                candidate_polarity = _literal_negation_polarity(candidate, span)
                if source_polarity is None:
                    reasons.append(f"IMMUTABLE_FACT_POLARITY_UNVERIFIABLE:{category}")
                elif candidate_polarity is None or candidate_polarity != source_polarity:
                    reasons.append(f"IMMUTABLE_FACT_POLARITY_CHANGED:{category}")
        if not candidate.strip():
            reasons.append("EMPTY_REFINEMENT")
        deduplicated = list(dict.fromkeys(reasons))
        return not deduplicated, deduplicated

    def _category_spans(
        self,
        category: str,
        value: Any,
        source_prompt: str | None,
    ) -> tuple[str, ...]:
        explicit = self.locked_spans.get(category, ())
        if explicit:
            return explicit

        fact_literals = _fact_literals(value)
        associated_required = tuple(
            literal
            for literal in self.required_literals
            if any(_contains_literal(fact_literal, literal) for fact_literal in fact_literals)
        )
        if associated_required:
            return associated_required
        if source_prompt is None:
            return fact_literals
        return tuple(literal for literal in fact_literals if _contains_literal(source_prompt, literal))


_ENGLISH_COUNTS = {
    "zero": 0,
    "one": 1,
    "single": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_CHINESE_COUNTS = {
    "零": 0,
    "一": 1,
    "单": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_CHARACTER_NOUNS = r"characters?|people|persons?|actors?|men|women|boys?|girls?"


def _integer_fact(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 and str(parsed) == str(value).strip() else None


def _character_counts(value: str) -> set[int]:
    counts: set[int] = set()
    for match in re.finditer(
        rf"\b(\d+|{'|'.join(_ENGLISH_COUNTS)})\s+(?:{_CHARACTER_NOUNS})\b",
        value.casefold(),
    ):
        token = match.group(1)
        counts.add(int(token) if token.isdigit() else _ENGLISH_COUNTS[token])
    for match in re.finditer(r"([零一二两三四五六七八九十单]|\d+)\s*[个名位]?\s*(?:人|角色|演员)", value):
        token = match.group(1)
        counts.add(int(token) if token.isdigit() else _CHINESE_COUNTS[token])
    return counts


def _contains_literal(value: str, literal: str) -> bool:
    normalized_value = " ".join(value.casefold().split())
    normalized_literal = " ".join(str(literal).casefold().split())
    return bool(normalized_literal) and normalized_literal in normalized_value


_CLAUSE_BOUNDARY = re.compile(r"[\n\r.!?;:,\u3002\uff01\uff1f\uff1b\uff1a\uff0c]")
_ENGLISH_NEGATION = re.compile(
    r"\b(?:"
    r"no|not(?!\s+only\b)|never|without|neither|nor|none|cannot|"
    r"can['’]?t|isn['’]?t|aren['’]?t|wasn['’]?t|weren['’]?t|"
    r"doesn['’]?t|don['’]?t|didn['’]?t|won['’]?t|wouldn['’]?t|"
    r"shouldn['’]?t|couldn['’]?t|lack(?:s|ed|ing)?|absent"
    r")\b",
    re.IGNORECASE,
)
_CHINESE_NEGATION = re.compile(r"没有|不曾|未曾|从未|绝不|并非|不是|毫无|没|不|无|未|非|莫|勿")
_NEGATION_LOOKBACK_CHARACTERS = 64


def _literal_occurrences(value: str, literal: str) -> tuple[re.Match[str], ...]:
    words = str(literal).strip().split()
    if not words:
        return ()
    pattern = r"\s+".join(re.escape(word) for word in words)
    return tuple(re.finditer(pattern, value, flags=re.IGNORECASE))


def _literal_negation_polarity(value: str, literal: str) -> bool | None:
    """Return a uniform local negation flag, or None for missing/mixed evidence.

    This is deliberately a small lexical guard, not general semantic analysis.
    It only compares common English/Chinese negators in the same bounded clause
    immediately before (or inside) each locked literal occurrence.
    """

    occurrences = _literal_occurrences(value, literal)
    if not occurrences:
        return None

    polarities: set[bool] = set()
    for occurrence in occurrences:
        prefix = value[: occurrence.start()]
        boundary = max((match.end() for match in _CLAUSE_BOUNDARY.finditer(prefix)), default=0)
        local_start = max(boundary, occurrence.start() - _NEGATION_LOOKBACK_CHARACTERS)
        local_context = value[local_start : occurrence.end()]
        polarities.add(
            bool(_ENGLISH_NEGATION.search(local_context) or _CHINESE_NEGATION.search(local_context))
        )
    if len(polarities) != 1:
        return None
    return polarities.pop()


def _fact_literals(value: Any) -> tuple[str, ...]:
    literals: list[str] = []

    def collect(item: Any) -> None:
        if isinstance(item, Mapping):
            for nested in item.values():
                collect(nested)
        elif isinstance(item, (list, tuple, set)):
            for nested in item:
                collect(nested)
        elif isinstance(item, str):
            stripped = item.strip()
            if stripped:
                literals.append(stripped)
        elif isinstance(item, (int, float, Decimal)) and not isinstance(item, bool):
            literals.append(str(item))

    collect(value)
    return tuple(dict.fromkeys(literals))


def verifiable_spans(source_prompt: str, candidates: Iterable[str]) -> tuple[str, ...]:
    """Keep only the spans ``source_prompt`` actually contains, in order.

    A locked span the source does not carry fails closed as
    ``LOCKED_SPAN_NOT_IN_SOURCE``, and a span covering the whole prompt can only
    be satisfied by a candidate that repeats it verbatim — which no genuine
    rewrite does. Filtering here, under the same normalization ``validate``
    uses, is what keeps a lock both meaningful and satisfiable.
    """

    unique = dict.fromkeys(str(span).strip() for span in candidates if str(span).strip())
    return tuple(span for span in unique if _contains_literal(source_prompt, span))


def extract_fact_locks(
    approved_spec: Mapping[str, Any],
    *,
    required_literals: tuple[str, ...] = (),
    locked_spans: Mapping[str, tuple[str, ...]] | None = None,
) -> FactLockSet:
    """Deterministically copy only approved immutable fields; never infer new facts."""

    locked = {
        category.value: approved_spec[category.value]
        for category in FactLockCategory
        if category.value in approved_spec
    }
    return FactLockSet(locked, required_literals, dict(locked_spans or {}))


@dataclass(frozen=True)
class PromptRefinementResult:
    original_prompt: str
    optimized_candidate: str
    accepted: bool
    source: str
    reason_codes: tuple[str, ...]
    diff: str


DraftGenerator = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
FallbackGenerator = Callable[[str, FactLockSet], Awaitable[dict[str, Any]]]


class FactLockPromptRefiner:
    """Returns an optional candidate; it never mutates or overwrites the user prompt."""

    def __init__(
        self,
        draft_generator: DraftGenerator,
        *,
        fallback_generator: FallbackGenerator | None = None,
    ):
        self.draft_generator = draft_generator
        self.fallback_generator = fallback_generator

    async def refine(
        self,
        *,
        original_prompt: str,
        fact_locks: FactLockSet,
    ) -> PromptRefinementResult:
        request = {
            "instruction": (
                "Improve professional cinematic wording only. Return JSON with refined_prompt and "
                "immutable_facts. Do not add, remove, reinterpret, or rename immutable facts."
            ),
            "original_prompt": original_prompt,
            "immutable_facts": fact_locks.immutable_facts,
        }
        draft = await self.draft_generator(request)
        result = self._validate(original_prompt, fact_locks, draft, source="runapi")
        if result.accepted or self.fallback_generator is None:
            return result
        fallback = await self.fallback_generator(original_prompt, fact_locks)
        return self._validate(original_prompt, fact_locks, fallback, source="fallback")

    @staticmethod
    def _validate(
        original_prompt: str,
        fact_locks: FactLockSet,
        response: Mapping[str, Any],
        *,
        source: str,
    ) -> PromptRefinementResult:
        candidate = str(response.get("refined_prompt") or "")
        echoed = response.get("immutable_facts")
        if not isinstance(echoed, Mapping):
            echoed = {}
        valid, reasons = fact_locks.validate(
            candidate,
            echoed,
            source_prompt=original_prompt,
        )
        selected = candidate if valid else original_prompt
        diff = "\n".join(
            difflib.unified_diff(
                original_prompt.splitlines(),
                selected.splitlines(),
                fromfile="original",
                tofile="candidate",
                lineterm="",
            )
        )
        return PromptRefinementResult(
            original_prompt=original_prompt,
            optimized_candidate=selected,
            accepted=valid,
            source=source,
            reason_codes=tuple(reasons),
            diff=diff,
        )


__all__ = [
    "EdgePolicyViolation",
    "EdgeTask",
    "EdgeTaskPolicy",
    "EdgeTaskRole",
    "FactLockCategory",
    "FactLockPromptRefiner",
    "FactLockSet",
    "PromptRefinementResult",
    "extract_fact_locks",
]
