"""Brief operations, provenance, question states and gap analysis.

The rules here are the floor, not the ceiling: a model (through
``ModelRoleRuntime``) reads the whole conversation and states explicit
operations; the deterministic extractor answers when no model is reachable.
Both go through ``apply_operations``, which enforces the one rule that keeps
the brief honest: a value the user established is only ever replaced or
removed on the user's own words, never on an inference. Every field carries
its provenance, and every question carries a state, so "asked" is never
mistaken for "answered".
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from production_domain.models import CreativeFormat

from .schemas import (
    ASPECT_RATIOS,
    ASSUMED_SOURCES,
    BRIEF_FIELD_SPECS,
    CHARACTER_LIST_PATH,
    FORMAT_DEFAULTS,
    INTEGER_PATHS,
    MAX_QUESTIONS_PER_TURN,
    NESTED_OBJECT_PATHS,
    RESOLVED_QUESTION_STATUSES,
    SCALAR_STRING_PATHS,
    SPECS_BY_CODE,
    STRING_LIST_PATHS,
    USER_ESTABLISHED_SOURCES,
    BriefOperation,
    BriefOperationKind,
    FieldWeight,
    ProvenanceSource,
    QuestionStatus,
    normalize_name,
)

_FORMAT_CUES: tuple[tuple[tuple[str, ...], CreativeFormat], ...] = (
    (("短剧", "连续剧", "剧集", "short drama", "series", "episodic"), CreativeFormat.SHORT_DRAMA),
    (("广告", "commercial", "advert", " ad ", "宣传片"), CreativeFormat.ADVERTISEMENT),
    (("产品", "product showcase", "product video", "开箱", "unboxing"), CreativeFormat.PRODUCT_SHOWCASE),
    (("音乐", "mv", "music video", "music visual", "歌"), CreativeFormat.MUSIC_VISUAL),
    (("时尚", "穿搭", "fashion", "lookbook", "服装"), CreativeFormat.FASHION_LOOKBOOK),
    (("美妆", "化妆", "beauty", "makeup", "护肤"), CreativeFormat.BEAUTY_TUTORIAL),
    (("概念", "concept film", "concept video", "艺术短片"), CreativeFormat.CONCEPT_FILM),
    (("社交", "抖音", "tiktok", "reels", "小红书", "social"), CreativeFormat.SOCIAL_SHORT),
)

_PLATFORM_CUES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("抖音", "douyin", "tiktok"), "tiktok"),
    (("小红书", "xiaohongshu", "rednote"), "xiaohongshu"),
    (("instagram", "reels", "ig "), "instagram"),
    (("youtube", "b站", "bilibili"), "youtube"),
    (("快手", "kuaishou"), "kuaishou"),
)

_TONE_CUES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("温暖", "治愈", "warm", "heartwarming", "cozy"), "warm"),
    (("悬疑", "紧张", "suspense", "thriller", "tense"), "suspenseful"),
    (("搞笑", "幽默", "funny", "comedy", "humorous"), "funny"),
    (("浪漫", "爱情", "romantic", "romance"), "romantic"),
    (("热血", "史诗", "epic", "high-energy"), "epic"),
    (("高级", "极简", "minimal", "elegant", "luxury", "奢华"), "elegant"),
    (("赛博朋克", "cyberpunk", "未来", "futuristic", "sci-fi", "科幻"), "futuristic"),
    (("黑暗", "dark", "gritty", "noir"), "dark"),
)

_MEDIUM_CUES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("动画", "anime", "二次元", "animated"), "anime"),
    (("3d", "render", "cgi", "三维"), "3d render"),
    (("胶片", "film look", "35mm", "复古"), "35mm film"),
    (("纪录", "documentary", "纪实"), "documentary realism"),
    (("水彩", "watercolor", "油画", "oil paint", "插画", "illustrated"), "painterly illustration"),
    (("实拍", "live-action", "live action", "photoreal", "写实"), "cinematic live-action"),
)

_DURATION_SECONDS = re.compile(r"(\d{1,3})\s*(?:秒|s\b|sec\b|secs\b|seconds?\b)", re.IGNORECASE)
_DURATION_MINUTES = re.compile(r"(\d{1,2})\s*(?:分钟|分\b|min\b|mins\b|minutes?\b)", re.IGNORECASE)
_ASPECT = re.compile(r"\b(9:16|16:9|1:1|4:3|3:4|21:9)\b")
_EPISODES = re.compile(r"(\d{1,3})\s*(?:集|episodes?\b|eps?\b)", re.IGNORECASE)
_QUOTED = re.compile(r"[「『\"“']([^「『\"”'』]{1,40})[」』\"”']")
_CH_NAME_INTRO = re.compile(
    r"(?:主角|女主|男主|主人公)(?:是|叫|名叫|：|:)?\s*([A-Za-z一-鿿][A-Za-z一-鿿·]{0,15})"
)
_EN_NAME_INTRO = re.compile(
    r"(?:named|called|protagonist(?: is)?|hero(?:ine)? is)\s+([A-Z][A-Za-z-]{1,20})", re.IGNORECASE
)
_PRODUCT_INTRO = re.compile(
    r"(?:产品|品牌)(?:是|叫|名叫|：|:)?\s*([A-Za-z0-9一-鿿][A-Za-z0-9一-鿿·\- ]{0,30})"
)
_LOCATION_INTRO = re.compile(
    r"(?:在|发生在|地点(?:是|：|:)?|set in|takes place in|located in)\s*"
    r"([A-Za-z一-鿿][A-Za-z一-鿿· ]{1,20}?)"
    r"(?=[，。,.;；!！?？\s]|$)"
)
_TIME_CUES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("夜", "night", "晚上", "凌晨", "midnight"), "NIGHT"),
    (("黄昏", "傍晚", "dusk", "sunset", "golden hour"), "DUSK"),
    (("清晨", "早晨", "dawn", "morning", "sunrise"), "DAY"),
    (("白天", "day", "daytime", "中午"), "DAY"),
)

#: Sentences the context builder must never compress away.
PROHIBITION_CUES = ("不要", "不能", "不准", "禁止", "别", "never", "don't", "do not", "must not", "no ")


def brief_hash(fields: dict[str, Any]) -> str:
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def get_path(fields: dict[str, Any], path: str) -> Any:
    node: Any = fields
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def set_path(fields: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = fields
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def delete_path(fields: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    node: Any = fields
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def is_user_established(record: dict[str, Any] | None) -> bool:
    return bool(record) and str(record.get("source")) in USER_ESTABLISHED_SOURCES


def is_assumed(record: dict[str, Any] | None) -> bool:
    return bool(record) and str(record.get("source")) in ASSUMED_SOURCES


def character_key(name: str) -> str:
    return f"{CHARACTER_LIST_PATH}/{normalize_name(name)}"


# ------------------------------------------------------------- value hygiene
def sanitize_value(path: str, value: Any) -> Any:
    """Coerce one operation value to the type its path demands, or return None.

    ``None`` means "not a usable value for this path" and the operation is
    rejected with a reason; a model that returns nonsense for a field changes
    nothing.
    """

    if path == "format":
        try:
            return CreativeFormat(str(value).strip().upper()).value
        except (ValueError, AttributeError):
            return None
    if path in INTEGER_PATHS:
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None
        if path == "duration_seconds" and not 1 <= number <= 3600:
            return None
        if path == "episode_count" and not 1 <= number <= 500:
            return None
        return number
    if path in SCALAR_STRING_PATHS:
        if isinstance(value, (list, dict)):
            return None
        text = " ".join(str(value if value is not None else "").split())[:500]
        if not text:
            return None
        if path == "aspect_ratio":
            return text if text in ASPECT_RATIOS else None
        if path == "setting.time":
            return text.upper()[:40]
        return text
    if path in STRING_LIST_PATHS:
        if isinstance(value, str):
            value = [part for part in re.split(r"[,，、;；/]+", value) if part.strip()]
        if not isinstance(value, list):
            return None
        items: list[str] = []
        for item in value:
            if isinstance(item, (dict, list)):
                continue
            text = " ".join(str(item).split())[:80]
            if text and text.casefold() not in {existing.casefold() for existing in items}:
                items.append(text)
        return items[:8] or None
    if path == CHARACTER_LIST_PATH:
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list):
            return None
        members = [member for member in (sanitize_character(item) for item in value) if member]
        return members[:8] or None
    if path in NESTED_OBJECT_PATHS:
        return value if isinstance(value, dict) else None
    return None


def sanitize_character(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        return None
    name = " ".join(str(raw.get("name") or "").split())[:60]
    if not name:
        return None
    member: dict[str, Any] = {"name": name}
    for key in ("role", "look", "wants", "notes"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            member[key] = " ".join(value.split())[:400]
    relationships = raw.get("relationships")
    if isinstance(relationships, list):
        cleaned: list[dict[str, str]] = []
        for item in relationships[:8]:
            if isinstance(item, str) and item.strip():
                cleaned.append({"with": "", "relation": item.strip()[:120]})
            elif isinstance(item, dict):
                other = " ".join(str(item.get("with") or item.get("name") or "").split())[:60]
                relation = " ".join(str(item.get("relation") or "").split())[:120]
                if other or relation:
                    cleaned.append({"with": other, "relation": relation})
        if cleaned:
            member["relationships"] = cleaned
    return member


def _expand_nested(operation: BriefOperation) -> list[BriefOperation]:
    """A nested-object operation ("setting": {...}) becomes one per leaf path."""

    if operation.path not in NESTED_OBJECT_PATHS or not isinstance(operation.value, dict):
        return [operation]
    expanded: list[BriefOperation] = []
    for key, value in operation.value.items():
        leaf = f"{operation.path}.{key}"
        if leaf in SCALAR_STRING_PATHS or leaf in STRING_LIST_PATHS:
            expanded.append(operation.model_copy(update={"path": leaf, "value": value}))
    return expanded


# ------------------------------------------------------------ apply operations
@dataclass
class OperationOutcome:
    applied: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    #: Spec codes whose field the user established on this pass.
    answered_codes: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class OperationActor:
    """Who is applying operations, for provenance."""

    reasoner: str
    turn_id: str | None
    turn_sequence: int | None
    revision: int
    at: str
    #: When True (the brief editor), every operation is the user's own act.
    direct_user_edit: bool = False


def apply_operations(
    fields: dict[str, Any],
    provenance: dict[str, Any],
    operations: list[BriefOperation],
    actor: OperationActor,
) -> tuple[dict[str, Any], dict[str, Any], OperationOutcome]:
    """Apply explicit operations under the provenance rules.

    Returns new fields, new provenance and the outcome (applied / rejected
    with reasons). Inputs are not mutated.
    """

    result = deepcopy(fields)
    records = deepcopy(provenance)
    outcome = OperationOutcome()

    def source_for(operation: BriefOperation) -> str:
        if actor.direct_user_edit:
            return ProvenanceSource.USER_EDIT.value
        if operation.confidence == "USER_STATED":
            return ProvenanceSource.USER_STATED.value
        return ProvenanceSource.MODEL_INFERRED.value

    def record(path_key: str, operation: BriefOperation, source: str) -> None:
        records[path_key] = {
            "source": source,
            "operation": operation.op.value,
            "reasoner": actor.reasoner,
            "turn_id": actor.turn_id,
            "turn_sequence": actor.turn_sequence,
            "evidence": operation.evidence[:300],
            "revision": actor.revision,
            "at": actor.at,
        }

    def reject(operation: BriefOperation, reason: str) -> None:
        outcome.rejected.append({"op": operation.op.value, "path": operation.path, "reason": reason})

    def accept(operation: BriefOperation, path_key: str, source: str, value: Any) -> None:
        outcome.applied.append(
            {
                "op": operation.op.value,
                "path": operation.path,
                "key": path_key,
                "source": source,
                "value": value,
                "evidence": operation.evidence[:300],
            }
        )
        if source in USER_ESTABLISHED_SOURCES:
            for spec in BRIEF_FIELD_SPECS:
                if spec.path == operation.path or path_key.startswith(spec.path + "/"):
                    outcome.answered_codes.add(spec.code)

    def user_may_override(operation: BriefOperation, existing: dict[str, Any] | None) -> bool:
        """Only the user's own words move a fact the user established."""

        if actor.direct_user_edit or operation.confidence == "USER_STATED":
            return True
        return not is_user_established(existing)

    expanded: list[BriefOperation] = []
    for operation in operations:
        expanded.extend(_expand_nested(operation))

    for operation in expanded:
        path = operation.path
        kind = operation.op
        if kind is BriefOperationKind.KEEP:
            existing = records.get(path)
            if (
                existing is not None
                and is_assumed(existing)
                and (actor.direct_user_edit or operation.confidence == "USER_STATED")
            ):
                # The user confirmed a value the director had assumed.
                existing["source"] = ProvenanceSource.ASSUMPTION_ACCEPTED.value
                existing["confirmed_turn_id"] = actor.turn_id
                accept(operation, path, ProvenanceSource.ASSUMPTION_ACCEPTED.value, get_path(result, path))
            else:
                outcome.applied.append({"op": "KEEP", "path": path, "key": path})
            continue

        if path == CHARACTER_LIST_PATH:
            _apply_character_operation(
                operation,
                result,
                records,
                actor,
                outcome,
                source_for,
                record,
                reject,
                accept,
                user_may_override,
            )
            continue

        if (
            path not in SCALAR_STRING_PATHS
            and path not in INTEGER_PATHS
            and path not in STRING_LIST_PATHS
            and path != "format"
        ):
            reject(operation, "UNKNOWN_PATH")
            continue

        existing = records.get(path)
        current = get_path(result, path)
        if kind is BriefOperationKind.REMOVE:
            if not _present(current):
                reject(operation, "NOTHING_TO_REMOVE")
                continue
            if not (actor.direct_user_edit or operation.confidence == "USER_STATED"):
                reject(operation, "REMOVE_REQUIRES_USER_STATEMENT")
                continue
            delete_path(result, path)
            records.pop(path, None)
            outcome.applied.append(
                {"op": "REMOVE", "path": path, "key": path, "source": source_for(operation)}
            )
            continue

        value = sanitize_value(path, operation.value)
        if value is None:
            reject(operation, "INVALID_VALUE")
            continue
        source = source_for(operation)

        if kind is BriefOperationKind.UPSERT and path in STRING_LIST_PATHS:
            merged = list(current) if isinstance(current, list) else []
            for item in value:
                if item.casefold() not in {existing_item.casefold() for existing_item in merged}:
                    merged.append(item)
            merged = merged[:8]
            if merged == current:
                outcome.applied.append({"op": "KEEP", "path": path, "key": path})
                continue
            if _present(current) and not user_may_override(operation, existing):
                reject(operation, "INFERRED_CANNOT_OVERRIDE_USER_FACT")
                continue
            set_path(result, path, merged)
            record(path, operation, source if not is_user_established(existing) else str(existing["source"]))
            accept(operation, path, records[path]["source"], merged)
            continue

        if kind is BriefOperationKind.SET or kind is BriefOperationKind.UPSERT:
            if _present(current):
                if current == value:
                    outcome.applied.append({"op": "KEEP", "path": path, "key": path})
                    continue
                if is_user_established(existing):
                    reject(operation, "SET_ON_USER_FACT_USE_REPLACE")
                    continue
                # An assumed or inferred value may be refined by a newer reading.
            set_path(result, path, value)
            record(path, operation, source)
            accept(operation, path, source, value)
            continue

        if kind is BriefOperationKind.REPLACE:
            if not user_may_override(operation, existing):
                reject(operation, "INFERRED_CANNOT_OVERRIDE_USER_FACT")
                continue
            if current == value:
                if is_assumed(existing) and source in USER_ESTABLISHED_SOURCES:
                    record(path, operation, source)
                    accept(operation, path, source, value)
                else:
                    outcome.applied.append({"op": "KEEP", "path": path, "key": path})
                continue
            set_path(result, path, value)
            record(path, operation, source)
            accept(operation, path, source, value)
            continue

        reject(operation, "UNSUPPORTED_OPERATION")

    return result, records, outcome


def _apply_character_operation(  # noqa: PLR0913 - one place for the member merge rules
    operation: BriefOperation,
    result: dict[str, Any],
    records: dict[str, Any],
    actor: OperationActor,
    outcome: OperationOutcome,
    source_for: Any,
    record: Any,
    reject: Any,
    accept: Any,
    user_may_override: Any,
) -> None:
    kind = operation.op
    members: list[dict[str, Any]] = [
        member for member in (result.get(CHARACTER_LIST_PATH) or []) if isinstance(member, dict)
    ]
    by_key = {normalize_name(str(member.get("name", ""))): member for member in members}

    if kind is BriefOperationKind.REMOVE:
        raw = operation.value
        name = raw.get("name") if isinstance(raw, dict) else raw
        key = normalize_name(str(name or ""))
        if not key or key not in by_key:
            reject(operation, "NOTHING_TO_REMOVE")
            return
        if not (actor.direct_user_edit or operation.confidence == "USER_STATED"):
            reject(operation, "REMOVE_REQUIRES_USER_STATEMENT")
            return
        result[CHARACTER_LIST_PATH] = [
            member for member in members if normalize_name(str(member.get("name", ""))) != key
        ]
        records.pop(character_key(str(name)), None)
        if not result[CHARACTER_LIST_PATH]:
            result.pop(CHARACTER_LIST_PATH, None)
        outcome.applied.append(
            {
                "op": "REMOVE",
                "path": CHARACTER_LIST_PATH,
                "key": character_key(str(name)),
                "source": source_for(operation),
            }
        )
        return

    incoming = sanitize_value(CHARACTER_LIST_PATH, operation.value)
    if incoming is None:
        reject(operation, "INVALID_VALUE")
        return
    source = source_for(operation)

    if kind is BriefOperationKind.REPLACE:
        # Replace the whole cast: only on the user's word when any member is a user fact.
        established = [
            key for key in by_key if is_user_established(records.get(f"{CHARACTER_LIST_PATH}/{key}"))
        ]
        if established and not (actor.direct_user_edit or operation.confidence == "USER_STATED"):
            reject(operation, "INFERRED_CANNOT_OVERRIDE_USER_FACT")
            return
        for key in list(by_key):
            records.pop(f"{CHARACTER_LIST_PATH}/{key}", None)
        result[CHARACTER_LIST_PATH] = incoming
        for member in incoming:
            record(character_key(member["name"]), operation, source)
            accept(operation, character_key(member["name"]), source, member)
        return

    # SET and UPSERT both merge member by member.
    changed = False
    for member in incoming:
        key = normalize_name(member["name"])
        path_key = f"{CHARACTER_LIST_PATH}/{key}"
        existing_record = records.get(path_key)
        current = by_key.get(key)
        if current is None:
            if kind is BriefOperationKind.SET and members and not user_may_override(operation, None):
                # SET adds members only when the cast is empty or the model
                # attributes the addition to the user.
                pass
            members.append(dict(member))
            by_key[key] = members[-1]
            record(path_key, operation, source)
            accept(operation, path_key, source, member)
            changed = True
            continue
        updates: dict[str, Any] = {}
        for field_name, value in member.items():
            if field_name == "name":
                continue
            if not _present(current.get(field_name)):
                updates[field_name] = value
            elif current.get(field_name) != value:
                if user_may_override(operation, existing_record):
                    updates[field_name] = value
                else:
                    reject(
                        operation.model_copy(update={"path": f"{CHARACTER_LIST_PATH}.{key}.{field_name}"}),
                        "INFERRED_CANNOT_OVERRIDE_USER_FACT",
                    )
        if updates:
            current.update(updates)
            new_source = source
            if is_user_established(existing_record) and source not in USER_ESTABLISHED_SOURCES:
                new_source = str(existing_record["source"])
            record(path_key, operation, new_source)
            accept(operation, path_key, new_source, current)
            changed = True
        else:
            outcome.applied.append({"op": "KEEP", "path": CHARACTER_LIST_PATH, "key": path_key})
    if changed:
        result[CHARACTER_LIST_PATH] = members


# ----------------------------------------------------------- question ledger
def question_state(states: dict[str, Any], code: str) -> dict[str, Any]:
    state = states.get(code)
    if not isinstance(state, dict):
        state = {"status": QuestionStatus.UNASKED.value, "asked_turns": []}
        states[code] = state
    state.setdefault("asked_turns", [])
    state.setdefault("status", QuestionStatus.UNASKED.value)
    return state


def reconcile_questions(
    states: dict[str, Any],
    fields: dict[str, Any],
    provenance: dict[str, Any],
    *,
    asked_now: list[str],
    skipped_now: list[str],
    turn_sequence: int | None,
) -> dict[str, Any]:
    """Move every question to the state the brief now supports.

    Answered means the field is present *and* user-established; a default or
    an inferred value leaves the question open and shows as an assumption.
    """

    result = deepcopy(states)
    for spec in BRIEF_FIELD_SPECS:
        state = question_state(result, spec.code)
        value = get_path(fields, spec.path)
        record = _provenance_for_spec(provenance, spec.path, fields)
        if _present(value) and is_user_established(record):
            if (
                state["status"] != QuestionStatus.ASSUMPTION_ACCEPTED.value
                or str(record.get("source")) != ProvenanceSource.ASSUMPTION_ACCEPTED.value
            ):  # type: ignore[union-attr]
                state["status"] = QuestionStatus.ANSWERED.value
                state.setdefault("answered_turn", turn_sequence)
            if str(record.get("source")) == ProvenanceSource.ASSUMPTION_ACCEPTED.value:  # type: ignore[union-attr]
                state["status"] = QuestionStatus.ASSUMPTION_ACCEPTED.value
        elif state["status"] in RESOLVED_QUESTION_STATUSES and not _present(value):
            # The user removed the answer: the question is open again.
            state["status"] = (
                QuestionStatus.ASKED.value if state["asked_turns"] else QuestionStatus.UNASKED.value
            )
            state.pop("answered_turn", None)
        if _present(value) and is_assumed(record):
            state["assumed_value"] = value
            state["assumed_source"] = str(record.get("source"))  # type: ignore[union-attr]
        else:
            state.pop("assumed_value", None)
            state.pop("assumed_source", None)
    for code in skipped_now:
        if code in SPECS_BY_CODE:
            state = question_state(result, code)
            if state["status"] not in RESOLVED_QUESTION_STATUSES:
                state["status"] = QuestionStatus.SKIPPED_BY_USER.value
                state["skipped_turn"] = turn_sequence
    for code in asked_now:
        if code in SPECS_BY_CODE:
            state = question_state(result, code)
            if turn_sequence is not None and turn_sequence not in state["asked_turns"]:
                state["asked_turns"].append(turn_sequence)
            if state["status"] in {QuestionStatus.UNASKED.value, QuestionStatus.SKIPPED_BY_USER.value}:
                state["status"] = QuestionStatus.ASKED.value
    return result


def _provenance_for_spec(
    provenance: dict[str, Any], path: str, fields: dict[str, Any]
) -> dict[str, Any] | None:
    if path != CHARACTER_LIST_PATH:
        return provenance.get(path)
    # The cast is answered when any member is user-established.
    best: dict[str, Any] | None = None
    for member in fields.get(CHARACTER_LIST_PATH) or []:
        if not isinstance(member, dict):
            continue
        record = provenance.get(character_key(str(member.get("name", ""))))
        if is_user_established(record):
            return record
        if record is not None and best is None:
            best = record
    return best


# ---------------------------------------------------------------- analysis
@dataclass(frozen=True)
class GapReport:
    code: str
    path: str
    weight: int
    question: str
    status: str
    assumed_value: Any = None


@dataclass(frozen=True)
class BriefAnalysis:
    """What is missing, what to ask next, whether a proposal can stand, and why not."""

    gaps: list[GapReport]
    questions: list[GapReport]
    proposable: bool
    #: CRITICAL codes that block proposal/approval and the reason each blocks.
    blocking: list[dict[str, Any]]
    #: Fields whose value is assumed (DEFAULT or MODEL_INFERRED) and awaits confirmation.
    assumptions: list[dict[str, Any]]
    applied_defaults: dict[str, Any] = field(default_factory=dict)

    def completeness(self) -> dict[str, Any]:
        return {
            "gaps": [
                {
                    "code": gap.code,
                    "path": gap.path,
                    "weight": gap.weight,
                    "status": gap.status,
                    "already_asked": gap.status != QuestionStatus.UNASKED.value,
                }
                for gap in self.gaps
            ],
            "blocking": self.blocking,
            "assumptions": self.assumptions,
            "applied_defaults": self.applied_defaults,
            "proposable": self.proposable,
        }


class BriefEngine:
    """Rules-based extraction plus per-format gap analysis over question states."""

    version = "creative-brief-v2"

    @staticmethod
    def _match(text: str, cues: tuple[tuple[tuple[str, ...], Any], ...]) -> Any | None:
        lowered = text.casefold()
        for terms, value in cues:
            if any(term.strip() and term.casefold() in lowered for term in terms):
                return value
        return None

    @classmethod
    def detect_format(cls, text: str) -> CreativeFormat | None:
        return cls._match(text, _FORMAT_CUES)

    @classmethod
    def extract_operations(
        cls, text: str, current: dict[str, Any], *, include_logline: bool
    ) -> list[BriefOperation]:
        """Deterministic SET operations this text supports, for empty fields only.

        Regex hits on the user's literal words are USER_STATED: they *are* the
        user's words, and a later explicit correction may replace them.
        """

        operations: list[BriefOperation] = []

        def fill(path: str, value: Any, evidence: str) -> None:
            if _present(value) and not _present(get_path(current, path)):
                operations.append(
                    BriefOperation(
                        op=BriefOperationKind.SET,
                        path=path,
                        value=value,
                        evidence=evidence[:300],
                        confidence="USER_STATED",
                    )
                )

        detected_format = cls.detect_format(text)
        if detected_format is not None:
            fill("format", detected_format.value, text)

        minutes = _DURATION_MINUTES.search(text)
        seconds = _DURATION_SECONDS.search(text)
        if minutes:
            fill("duration_seconds", int(minutes.group(1)) * 60, minutes.group(0))
        elif seconds:
            fill("duration_seconds", int(seconds.group(1)), seconds.group(0))

        aspect = _ASPECT.search(text)
        if aspect:
            fill("aspect_ratio", aspect.group(1), aspect.group(0))
        elif "竖屏" in text or "vertical" in text.casefold():
            fill("aspect_ratio", "9:16", "竖屏/vertical")
        elif "横屏" in text or "widescreen" in text.casefold():
            fill("aspect_ratio", "16:9", "横屏/widescreen")

        episodes = _EPISODES.search(text)
        if episodes:
            fill("episode_count", int(episodes.group(1)), episodes.group(0))

        platform = cls._match(text, _PLATFORM_CUES)
        if platform:
            fill("platform", platform, text)
        medium = cls._match(text, _MEDIUM_CUES)
        if medium:
            fill("visual_style.medium", medium, text)

        tones = [
            value for terms, value in _TONE_CUES if any(term.casefold() in text.casefold() for term in terms)
        ]
        if tones:
            fill("tone", tones, text)

        name_match = _CH_NAME_INTRO.search(text) or _EN_NAME_INTRO.search(text)
        if name_match and not _present(get_path(current, CHARACTER_LIST_PATH)):
            operations.append(
                BriefOperation(
                    op=BriefOperationKind.UPSERT,
                    path=CHARACTER_LIST_PATH,
                    value=[{"name": name_match.group(1).strip(), "role": "protagonist"}],
                    evidence=name_match.group(0)[:300],
                    confidence="USER_STATED",
                )
            )

        product = _PRODUCT_INTRO.search(text)
        if product:
            fill("product.name", product.group(1).strip(" ，。,."), product.group(0))
        elif not _present(get_path(current, "product.name")):
            quoted = _QUOTED.search(text)
            lowered = text.casefold()
            if quoted and any(term in lowered for term in ("产品", "品牌", "product", "brand")):
                fill("product.name", quoted.group(1).strip(), quoted.group(0))

        location = _LOCATION_INTRO.search(text)
        if location:
            candidate = location.group(1).strip()
            # "在30秒内" style matches are numbers, not places.
            if not re.fullmatch(r"[\d\s]+", candidate):
                fill("setting.location", candidate, location.group(0))
        time_value = cls._match(text, _TIME_CUES)
        if time_value:
            fill("setting.time", time_value, text)

        # The first substantive user text becomes the logline candidate when
        # no model is reading the conversation: the core idea is whatever they
        # opened with, until they replace it.
        stripped = text.strip()
        if include_logline and len(stripped) >= 12 and not _present(get_path(current, "logline")):
            fill("logline", stripped[:500], stripped[:300])
        return operations

    @classmethod
    def extract(cls, text: str, current: dict[str, Any]) -> dict[str, Any]:
        """Compatibility view of the deterministic extraction as a field patch."""

        patch: dict[str, Any] = {}
        for operation in cls.extract_operations(text, current, include_logline=True):
            value = sanitize_value(operation.path, operation.value)
            if value is not None:
                set_path(patch, operation.path, value)
        return patch

    @staticmethod
    def merge(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        """Fill-empty merge, kept for callers that still think in patches."""

        merged = deepcopy(current)

        def walk(target: dict[str, Any], source: dict[str, Any]) -> None:
            for key, value in source.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    walk(target[key], value)
                elif not _present(target.get(key)):
                    target[key] = value

        walk(merged, deepcopy(patch))
        return merged

    @staticmethod
    def prohibitions(text: str) -> list[str]:
        """Sentences that forbid something; preserved verbatim through compression."""

        found: list[str] = []
        for sentence in re.split(r"(?<=[。！？!?\.\n])\s*", text):
            lowered = sentence.casefold()
            if sentence.strip() and any(cue in lowered for cue in PROHIBITION_CUES):
                found.append(sentence.strip()[:300])
        return found

    @classmethod
    def analyze(
        cls,
        fields: dict[str, Any],
        provenance: dict[str, Any],
        question_states: dict[str, Any],
        *,
        format_value: str,
        proposed_questions: list[str] | None = None,
    ) -> BriefAnalysis:
        """Gap analysis over question states.

        A gap is a spec field that is missing or only assumed. It becomes a
        question when its weight is HIGH or CRITICAL for the format and it is
        UNASKED or still ASKED (re-asking is allowed, at lower priority - a
        code having appeared once never retires the question). The brief is
        proposable when the format is known, every CRITICAL field is ANSWERED
        or has an accepted assumption, and no HIGH gap is still UNASKED.
        """

        gaps: list[GapReport] = []
        blocking: list[dict[str, Any]] = []
        assumptions: list[dict[str, Any]] = []
        for spec in BRIEF_FIELD_SPECS:
            weight = spec.weights.get(format_value, spec.default_weight)
            if format_value == CreativeFormat.UNSPECIFIED.value:
                # Until the format is known, only format and logline are worth
                # asking for; every other weight depends on the answer.
                weight = weight if spec.code in {"FORMAT", "LOGLINE"} else FieldWeight.IRRELEVANT
            if weight is FieldWeight.IRRELEVANT:
                continue
            state = question_states.get(spec.code) or {}
            status = str(state.get("status") or QuestionStatus.UNASKED.value)
            value = get_path(fields, spec.path)
            record = _provenance_for_spec(provenance, spec.path, fields)
            present = _present(value)
            assumed = present and is_assumed(record)
            if assumed:
                assumptions.append(
                    {
                        "code": spec.code,
                        "path": spec.path,
                        "value": value,
                        "source": str(record.get("source")),  # type: ignore[union-attr]
                        "weight": int(weight),
                    }
                )
            if present and not assumed:
                continue
            gaps.append(
                GapReport(
                    code=spec.code,
                    path=spec.path,
                    weight=int(weight),
                    question=spec.question,
                    status=status,
                    assumed_value=value if assumed else None,
                )
            )
            if weight is FieldWeight.CRITICAL and status not in RESOLVED_QUESTION_STATUSES:
                blocking.append(
                    {
                        "code": spec.code,
                        "path": spec.path,
                        "status": status,
                        "reason": "CRITICAL_UNANSWERED",
                        "assumed_value": value if assumed else None,
                    }
                )

        askable = [
            gap
            for gap in gaps
            if gap.weight >= int(FieldWeight.HIGH)
            and gap.status in {QuestionStatus.UNASKED.value, QuestionStatus.ASKED.value}
        ]

        def priority(gap: GapReport) -> tuple[int, int, int, str]:
            asked_count = len((question_states.get(gap.code) or {}).get("asked_turns") or [])
            return (
                0 if gap.status == QuestionStatus.UNASKED.value else 1,
                asked_count,
                -gap.weight,
                gap.code,
            )

        askable.sort(key=priority)
        chosen: list[GapReport] = []
        if proposed_questions:
            by_code = {gap.code: gap for gap in askable}
            for code in proposed_questions:
                gap = by_code.get(code)
                if gap is not None and gap not in chosen:
                    chosen.append(gap)
        for gap in askable:
            if gap not in chosen:
                chosen.append(gap)
        questions = chosen[:MAX_QUESTIONS_PER_TURN]

        unasked_high = [
            gap
            for gap in gaps
            if gap.weight >= int(FieldWeight.HIGH)
            and gap.status == QuestionStatus.UNASKED.value
            and gap.assumed_value is None
        ]
        proposable = format_value != CreativeFormat.UNSPECIFIED.value and not blocking and not unasked_high

        applied_defaults: dict[str, Any] = {}
        if proposable:
            for path, value in FORMAT_DEFAULTS.get(format_value, {}).items():
                if not _present(get_path(fields, path)):
                    applied_defaults[path] = value
        return BriefAnalysis(
            gaps=gaps,
            questions=questions,
            proposable=proposable,
            blocking=blocking,
            assumptions=assumptions,
            applied_defaults=applied_defaults,
        )

    @staticmethod
    def user_facts(fields: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
        """Every user-established value by path - the facts nobody may silently move."""

        facts: dict[str, Any] = {}
        for key, record in provenance.items():
            if not is_user_established(record):
                continue
            if key.startswith(CHARACTER_LIST_PATH + "/"):
                wanted = key.split("/", 1)[1]
                for member in fields.get(CHARACTER_LIST_PATH) or []:
                    if isinstance(member, dict) and normalize_name(str(member.get("name", ""))) == wanted:
                        facts[key] = member
            else:
                value = get_path(fields, key)
                if _present(value):
                    facts[key] = value
        return facts
