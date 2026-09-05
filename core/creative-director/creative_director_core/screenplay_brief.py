"""Does the screenplay obey the brief the user approved?

The screenplay passed a schema and nothing else: the model could rename the
protagonist, move the story to another city, drop the product from a product
film or invent a relationship, and the structure was still valid, so the
key visuals were derived and paid for from a story the user never approved.

Severity here is decided by *provenance*, not by a fixed table of fields. A
brief value the user established - stated, edited, or an assumption they
explicitly accepted - is a fact the model may not move: contradicting it is
BLOCKING. A value the director inferred or a format default supplied is the
creative space the director is *supposed* to fill: contradicting it is
ADVISORY, and is reported as enrichment rather than a rewrite. That single
rule is what separates "the director had an idea" from "the director changed
what the client asked for".

The validator is pure: no database, no session, no raising. It returns the
findings and lets the service decide which of them block an approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .brief import get_path, is_user_established
from .evidence import aligned_occurrences, normalize
from .schemas import (
    ASPECT_RATIOS,
    CHARACTER_LIST_PATH,
    COMMERCE_FORMATS,
    Screenplay,
    normalize_name,
)

BLOCKING = "BLOCKING"
ADVISORY = "ADVISORY"

#: What a prohibition sentence is made of besides the thing it forbids: the
#: negation, the politeness around it, and the words that only say "show" or
#: "scene". Stripped, so "请不要出现任何暴力镜头" and "please don't show any
#: violence" both leave the one term that matters - 暴力, violence - and that
#: term is then looked for in what the characters *do*, not only in what they
#: say. Longest first, so "不可以" goes before "不".
_PROHIBITION_NOISE_CJK: tuple[str, ...] = tuple(
    sorted(
        (
            "请", "千万", "绝对", "一定", "务必", "不要", "不能", "不准", "不可以", "不可", "不得",
            "不允许", "不应该", "不应", "禁止", "避免", "别", "切勿", "勿", "出现", "使用", "展示",
            "显示", "提到", "提及", "包含", "含有", "涉及", "任何", "一切", "所有", "全部", "镜头",
            "画面", "内容", "元素", "场景", "情节", "之类", "这种", "那种", "的", "了", "吧", "啊",
            "呢", "哦", "地", "得", "都", "要", "再", "在", "里", "中", "有", "太", "过于",
        ),
        key=len,
        reverse=True,
    )
)
_PROHIBITION_NOISE_LATIN = frozenset(
    {
        "please", "never", "don't", "dont", "do", "not", "must", "mustn't", "mustnt", "should",
        "shouldn't", "shouldnt", "can't", "cant", "cannot", "no", "nor", "avoid", "without", "any",
        "anything", "all", "some", "a", "an", "the", "of", "in", "on", "at", "to", "with", "into",
        "show", "showing", "shown", "include", "including", "included", "use", "using", "mention",
        "mentioning", "depict", "depicting", "feature", "featuring", "have", "having", "put",
        "there", "be", "is", "are", "was", "were", "it", "its", "this", "that", "these", "those",
        "scene", "scenes", "shot", "shots", "content", "element", "elements", "footage", "ever",
        "kind", "kinds", "sort", "sorts", "thing", "things", "stuff", "want", "i", "we", "you",
        "let's", "lets", "make", "sure", "absolutely", "definitely", "really", "too", "very", "so",
        "here", "ok", "okay", "and", "or", "but", "also", "just", "only", "at all",
    }
)
_PROHIBITION_SPLIT = re.compile(r"[,，、;；/。！!？?\n]|\s+(?:and|or|nor)\s+|(?:或者|或|和|以及|与|及)")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
#: A term that appears right after one of these is being ruled out, not shown:
#: "no blood" in a tone note is the director agreeing with the user.
_NEGATION_BEFORE_LATIN = frozenset(
    {"no", "not", "never", "without", "avoid", "avoids", "avoiding", "zero", "nor"}
)
_NEGATION_BEFORE_CJK = (
    "不要", "不能", "不准", "不可", "不得", "禁止", "避免", "没有", "无", "别", "勿", "不",
)
#: What makes a sentence an instruction not to do something, rather than a
#: sentence that merely contains a negative. `BriefEngine.prohibitions` casts
#: a wide net on purpose (it decides what survives context compression, where
#: over-collecting is free); enforcement needs the narrow one, because "别墅"
#: is a villa and "she has no umbrella" is a fact, and a term pulled out of
#: either would block an approval or forbid the wrong thing in every prompt.
_ENFORCEABLE_CJK = re.compile(
    r"不要|不能|不准|不可以|不得|不允许|不应该|禁止|避免|切勿|(?:^|[\s，,。；;：:！!？?、]|请|千万|也|都|可)勿"
    r"|(?:^|[\s，,。；;：:！!？?、]|请|千万|也|都)别(?!的|人|处|墅|名|说|管)"
)
_ENFORCEABLE_LATIN = re.compile(
    r"^\s*(?:please[,，]?\s*)?no\b|\b(?:don'?t|do not|never|must not|mustn'?t|avoid|not allowed|"
    r"should not|shouldn'?t|can'?t have|cannot have|without any|absolutely no|want no|no more|"
    r"nothing (?:with|like)|keep .{0,24}\b(?:out|away)\b|stay away from|steer clear of)\b",
    re.IGNORECASE,
)


def enforceable_prohibition(sentence: str) -> bool:
    """Whether a collected sentence is an instruction the screenplay can be held to."""

    text = str(sentence or "").strip()
    return bool(text) and bool(_ENFORCEABLE_CJK.search(text) or _ENFORCEABLE_LATIN.search(text))


def prohibited_terms(sentence: str) -> list[str]:
    """The things one prohibition sentence forbids, as searchable terms.

    Deterministic and literal: it strips the negation and the filler and keeps
    what is left, split on conjunctions. "不要暴力，也别有血腥镜头" yields
    暴力 and 血腥; "no talking heads or product close-ups" yields "talking
    heads" and "product close-ups". It does not understand synonyms - a
    prohibition on 暴力 is not a prohibition on 打斗 - which is why the same
    sentences also travel into every shot's prompt and QC checklist, where a
    model reads them whole. A sentence that is not an instruction yields
    nothing.
    """

    if not enforceable_prohibition(sentence):
        return []
    terms: list[str] = []
    for segment in _PROHIBITION_SPLIT.split(str(sentence or "")):
        piece = segment.strip()
        if not piece:
            continue
        for run in _CJK_RUN.findall(piece):
            cleaned = run
            for noise in _PROHIBITION_NOISE_CJK:
                cleaned = cleaned.replace(noise, " ")
            for term in cleaned.split():
                if len(term) >= 2 and term not in terms:
                    terms.append(term)
        # Only the ends are stripped: "avoid direct gaze into the camera" is
        # the phrase "direct gaze into the camera", searched as written,
        # rather than a word salad that matches nothing.
        words = _LATIN_WORD.findall(piece)
        while words and words[0].casefold() in _PROHIBITION_NOISE_LATIN:
            words.pop(0)
        while words and words[-1].casefold() in _PROHIBITION_NOISE_LATIN:
            words.pop()
        phrase = " ".join(words).strip()
        if len(phrase) >= 3 and phrase.casefold() not in {item.casefold() for item in terms}:
            terms.append(phrase)
    return terms


def _negated(text: str, position: int) -> bool:
    """Whether the occurrence at `position` (in normalized text) is being ruled out.

    Deliberately short-sighted in both directions: a negation within the last
    three words (Latin) or six characters (CJK) counts, anything further back
    does not. A missed negation blocks an approval the user can overrule; a
    missed violation is caught again by the prompt and the QC checklist, which
    carry the whole sentence.
    """

    before = normalize(text)[max(0, position - 16) : position]
    if not before.strip():
        return False
    if any(word in _NEGATION_BEFORE_LATIN for word in before.split()[-3:]):
        return True
    window = before[-6:]
    return any(cue in window for cue in _NEGATION_BEFORE_CJK)


def _screenplay_surfaces(screenplay: Screenplay) -> list[tuple[str, str, bool]]:
    """Every text of the screenplay with where it is, and whether it compiles to a shot."""

    return [(where, text, compiled) for where, text, compiled, _spoken in _surfaces(screenplay)]


def _surfaces(screenplay: Screenplay) -> list[tuple[str, str, bool, bool]]:
    """(where, text, compiles to a shot, is spoken on screen) for every text of the screenplay."""

    treatment = screenplay.treatment
    surfaces: list[tuple[str, str, bool, bool]] = [
        ("treatment.title", treatment.title, False, False),
        ("treatment.premise", treatment.premise, False, False),
        ("treatment.hook", treatment.hook.opening_question, False, False),
        ("treatment.hook", treatment.hook.promise, False, False),
        ("treatment.tone_direction", treatment.tone_direction, False, False),
        ("treatment.visual_direction", treatment.visual_direction, False, False),
        ("treatment.ending", treatment.ending, False, False),
        *(
            (f"invariant {index}", item, False, False)
            for index, item in enumerate(screenplay.invariant_texts, 1)
        ),
        *(
            (f"product claim {index}", claim.claim, True, True)
            for index, claim in enumerate(screenplay.product_claims, 1)
        ),
        *(
            (f"required copy {index}", text, True, True)
            for index, text in enumerate(screenplay.required_copy_texts, 1)
        ),
        *((f"scene {scene.key}", scene.description, False, False) for scene in screenplay.scenes),
    ]
    for beat in screenplay.beats:
        surfaces.append((f"beat {beat.sequence}", beat.summary, False, False))
        surfaces.append((f"beat {beat.sequence}", beat.emotional_beat, False, False))
        for shot in beat.shots:
            where = f"beat {beat.sequence} shot {shot.sequence}"
            if shot.dialogue is not None:
                surfaces.append((where, shot.dialogue.text, True, True))
            if shot.action is not None:
                surfaces.append(
                    (
                        where,
                        " ".join(
                            part
                            for part in (
                                shot.action.verb.replace("_", " "),
                                shot.action.object,
                                shot.action.target,
                                shot.action.description,
                            )
                            if part
                        ),
                        True,
                        False,
                    )
                )
            for part in (shot.start_state, shot.end_state):
                surfaces.append((where, part, True, False))
    return [item for item in surfaces if item[1] and item[1].strip()]

#: How far the screenplay's total running time may sit from the brief's
#: duration before it is reported. Pacing is a creative variable, so this is
#: generous; it exists to catch a screenplay written for a different brief.
DEFAULT_DURATION_TOLERANCE = 0.35

_TIME_ALIASES = {
    "NOON": "DAY",
    "MORNING": "DAY",
    "AFTERNOON": "DAY",
    "EVENING": "DUSK",
    "SUNSET": "DUSK",
    "GOLDEN HOUR": "DUSK",
    "MIDNIGHT": "NIGHT",
    "SUNRISE": "DAWN",
    "白天": "DAY",
    "夜": "NIGHT",
    "夜晚": "NIGHT",
    "晚上": "NIGHT",
    "黄昏": "DUSK",
    "傍晚": "DUSK",
    "清晨": "DAWN",
}
#: The compiler only ever renders DAY or NIGHT (beats._scene_time), so DAWN
#: reads as DAY and DUSK as NIGHT when comparing a brief to a scene.
_TIME_FAMILY = {"DAY": "DAY", "DAWN": "DAY", "NIGHT": "NIGHT", "DUSK": "NIGHT"}


@dataclass(frozen=True)
class BriefViolation:
    """One disagreement between the approved brief and the screenplay."""

    code: str
    #: The brief path in dispute, e.g. "setting.location" or "characters/mira".
    brief_path: str
    #: What the brief says, and what the screenplay says instead.
    brief_value: Any
    screenplay_value: Any
    severity: str
    reason: str
    #: Where in the screenplay, when the disagreement has a place.
    location: str = ""
    #: Who established the brief value: the fact that decides the severity.
    brief_source: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == BLOCKING

    def as_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "brief_path": self.brief_path,
            "brief_value": self.brief_value,
            "screenplay_value": self.screenplay_value,
            "severity": self.severity,
            "reason": self.reason,
            "location": self.location,
            "brief_source": self.brief_source,
        }


@dataclass
class BriefConformance:
    """The findings, split the way the caller has to act on them."""

    violations: list[BriefViolation] = field(default_factory=list)

    @property
    def blocking(self) -> list[BriefViolation]:
        return [item for item in self.violations if item.blocking]

    @property
    def advisory(self) -> list[BriefViolation]:
        return [item for item in self.violations if not item.blocking]

    def as_json(self) -> list[dict[str, Any]]:
        return [item.as_json() for item in self.violations]


def _text_of(screenplay: Screenplay) -> str:
    """Everything the screenplay says, for a "is this mentioned at all" check."""

    parts: list[str] = [
        screenplay.treatment.title,
        screenplay.treatment.premise,
        screenplay.treatment.hook.opening_question,
        screenplay.treatment.hook.promise,
        screenplay.treatment.audience_expectation,
        screenplay.treatment.tone_direction,
        screenplay.treatment.visual_direction,
        screenplay.treatment.ending,
        *screenplay.invariant_texts,
        *screenplay.required_copy_texts,
        *(claim.claim for claim in screenplay.product_claims),
        *(scene.location for scene in screenplay.scenes),
        *(scene.description for scene in screenplay.scenes),
    ]
    for beat in screenplay.beats:
        parts.extend([beat.summary, beat.emotional_beat])
        for shot in beat.shots:
            if shot.dialogue is not None:
                parts.append(shot.dialogue.text)
            if shot.action is not None:
                parts.extend([shot.action.object, shot.action.target, shot.action.description])
    return normalize(" \n ".join(part for part in parts if part))


class ScreenplayBriefValidator:
    """Compare an approved brief with the screenplay written from it."""

    def __init__(self, *, duration_tolerance: float = DEFAULT_DURATION_TOLERANCE) -> None:
        self.duration_tolerance = duration_tolerance

    # ----------------------------------------------------------- entry point
    def validate(
        self,
        screenplay: Screenplay,
        fields: dict[str, Any],
        *,
        format_value: str = "",
        provenance: dict[str, Any] | None = None,
        prohibitions: list[str] | None = None,
    ) -> BriefConformance:
        records = provenance or {}
        body = _text_of(screenplay)
        found = BriefConformance()
        self._check_cast(screenplay, fields, records, found)
        self._check_relationships(screenplay, fields, records, found)
        self._check_setting(screenplay, fields, records, found)
        self._check_duration(screenplay, fields, records, found)
        self._check_product(screenplay, fields, records, format_value, body, found)
        self._check_copy(screenplay, fields, records, body, found)
        self._check_hook(screenplay, fields, records, body, found)
        self._check_aspect(screenplay, fields, records, found)
        self._check_prohibitions(screenplay, prohibitions or [], found)
        return found

    # ------------------------------------------------------------- machinery
    @staticmethod
    def _severity(records: dict[str, Any], path: str) -> tuple[str, str]:
        """BLOCKING for a user fact, ADVISORY for the director's own reading."""

        record = records.get(path)
        source = str((record or {}).get("source") or "")
        if not records:
            # No provenance supplied: treat every brief value as the user's,
            # which is the conservative reading for a hand-built brief.
            return BLOCKING, source
        return (BLOCKING if is_user_established(record) else ADVISORY), source

    def _add(  # noqa: PLR0913 - one constructor for every rule
        self,
        found: BriefConformance,
        records: dict[str, Any],
        *,
        code: str,
        path: str,
        brief_value: Any,
        screenplay_value: Any,
        reason: str,
        location: str = "",
        force: str | None = None,
    ) -> None:
        severity, source = self._severity(records, path)
        found.violations.append(
            BriefViolation(
                code=code,
                brief_path=path,
                brief_value=brief_value,
                screenplay_value=screenplay_value,
                severity=force or severity,
                reason=reason,
                location=location,
                brief_source=source,
            )
        )

    # ----------------------------------------------------------------- rules
    def _check_cast(
        self,
        screenplay: Screenplay,
        fields: dict[str, Any],
        records: dict[str, Any],
        found: BriefConformance,
    ) -> None:
        """Every character the brief names must still be in the screenplay.

        A renamed protagonist is the sharpest version of this defect: the
        screenplay is structurally perfect, and the identity lock is taken on
        somebody the user never asked for.
        """

        names = {normalize_name(item.name) for item in screenplay.characters}
        for member in fields.get(CHARACTER_LIST_PATH) or []:
            if not isinstance(member, dict):
                continue
            name = str(member.get("name") or "").strip()
            if not name:
                continue
            key = normalize_name(name)
            if key in names:
                continue
            self._add(
                found,
                records,
                code="CAST_MEMBER_MISSING",
                path=f"{CHARACTER_LIST_PATH}/{key}",
                brief_value=name,
                screenplay_value=sorted(item.name for item in screenplay.characters),
                reason="the brief names this character; the screenplay does not",
            )

    def _check_relationships(
        self,
        screenplay: Screenplay,
        fields: dict[str, Any],
        records: dict[str, Any],
        found: BriefConformance,
    ) -> None:
        by_key = {normalize_name(item.name): item for item in screenplay.characters}
        for member in fields.get(CHARACTER_LIST_PATH) or []:
            if not isinstance(member, dict):
                continue
            key = normalize_name(str(member.get("name") or ""))
            character = by_key.get(key)
            if character is None:
                continue  # already reported by _check_cast
            declared = {
                normalize_name(item.with_): item.relation for item in character.relationships
            }
            for relation in member.get("relationships") or []:
                if not isinstance(relation, dict):
                    continue
                other = normalize_name(str(relation.get("with") or ""))
                wanted = str(relation.get("relation") or "").strip()
                if not other:
                    continue
                if other not in declared:
                    self._add(
                        found,
                        records,
                        code="RELATIONSHIP_MISSING",
                        path=f"{CHARACTER_LIST_PATH}/{key}",
                        brief_value={"with": relation.get("with"), "relation": wanted},
                        screenplay_value=[item.model_dump(by_alias=True) for item in character.relationships],
                        reason="the brief establishes this relationship; the screenplay drops it",
                        location=f"characters/{character.name}",
                    )
                elif wanted and normalize(declared[other]) != normalize(wanted):
                    self._add(
                        found,
                        records,
                        code="RELATIONSHIP_CHANGED",
                        path=f"{CHARACTER_LIST_PATH}/{key}",
                        brief_value=wanted,
                        screenplay_value=declared[other],
                        reason="the screenplay rewrites a relationship the brief establishes",
                        location=f"characters/{character.name}",
                    )

    def _check_setting(
        self,
        screenplay: Screenplay,
        fields: dict[str, Any],
        records: dict[str, Any],
        found: BriefConformance,
    ) -> None:
        location = str(get_path(fields, "setting.location") or "").strip()
        if location:
            wanted = normalize(location)
            locations = [scene.location for scene in screenplay.scenes]
            if not any(wanted in normalize(item) or normalize(item) in wanted for item in locations):
                self._add(
                    found,
                    records,
                    code="LOCATION_CHANGED",
                    path="setting.location",
                    brief_value=location,
                    screenplay_value=locations,
                    reason="no scene plays where the brief sets the story",
                )
        time_value = str(get_path(fields, "setting.time") or "").strip().upper()
        if time_value:
            normalized = _TIME_ALIASES.get(time_value, time_value)
            family = _TIME_FAMILY.get(normalized)
            if family is not None:
                times = [scene.time for scene in screenplay.scenes]
                if not any(_TIME_FAMILY.get(item) == family for item in times):
                    self._add(
                        found,
                        records,
                        code="TIME_CHANGED",
                        path="setting.time",
                        brief_value=time_value,
                        screenplay_value=times,
                        reason="no scene plays at the time of day the brief sets",
                    )

    def _check_duration(
        self,
        screenplay: Screenplay,
        fields: dict[str, Any],
        records: dict[str, Any],
        found: BriefConformance,
    ) -> None:
        wanted = get_path(fields, "duration_seconds")
        if not isinstance(wanted, (int, float)) or wanted <= 0:
            return
        total = sum(float(shot.duration) for beat in screenplay.beats for shot in beat.shots)
        if abs(total - float(wanted)) <= float(wanted) * self.duration_tolerance:
            return
        self._add(
            found,
            records,
            code="DURATION_OUT_OF_TOLERANCE",
            path="duration_seconds",
            brief_value=wanted,
            screenplay_value=round(total, 2),
            reason=(
                f"the shots total {total:g}s against a brief of {wanted}s, outside the "
                f"{self.duration_tolerance:.0%} tolerance"
            ),
            # Pacing is a creative variable the director owns; a mismatch is
            # worth showing, not worth blocking an approval on.
            force=ADVISORY,
        )

    def _check_product(  # noqa: PLR0913 - the rule needs the whole context
        self,
        screenplay: Screenplay,
        fields: dict[str, Any],
        records: dict[str, Any],
        format_value: str,
        body: str,
        found: BriefConformance,
    ) -> None:
        product = str(get_path(fields, "product.name") or "").strip()
        if not product:
            return
        if normalize(product) not in body:
            self._add(
                found,
                records,
                code="PRODUCT_MISSING",
                path="product.name",
                brief_value=product,
                screenplay_value=None,
                reason="the brief's product never appears in the screenplay",
                force=None if format_value in COMMERCE_FORMATS else ADVISORY,
            )
        claims = [claim.claim for claim in screenplay.product_claims]
        for point in get_path(fields, "product.selling_points") or []:
            text = str(point).strip()
            if text and normalize(text) not in body:
                # The same rule as the product itself: in a commerce piece a
                # selling point the *user* stated is a fact the director may
                # not drop (severity follows its provenance); elsewhere the
                # product is set dressing and the omission is advice.
                self._add(
                    found,
                    records,
                    code="SELLING_POINT_MISSING",
                    path="product.selling_points",
                    brief_value=text,
                    screenplay_value=claims,
                    reason="a selling point the brief establishes is nowhere in the screenplay",
                    force=None if format_value in COMMERCE_FORMATS else ADVISORY,
                )

    def _check_copy(  # noqa: PLR0913 - the rule needs the whole context
        self,
        screenplay: Screenplay,
        fields: dict[str, Any],
        records: dict[str, Any],
        body: str,
        found: BriefConformance,
    ) -> None:
        cta = str(get_path(fields, "call_to_action") or "").strip()
        if not cta:
            return
        # Only what compiles into a shot counts: placed copy, a spoken line,
        # or the staging of an action. The treatment's ending is prose about
        # the film - nothing renders it - so a call to action that lived only
        # there was approved and then never reached the screen.
        targets = [
            *screenplay.required_copy_texts,
            *(
                text
                for _where, text, compiled in _screenplay_surfaces(screenplay)
                if compiled
            ),
        ]
        if any(normalize(cta) in normalize(item) for item in targets if item):
            return
        self._add(
            found,
            records,
            code="CALL_TO_ACTION_MISSING",
            path="call_to_action",
            brief_value=cta,
            screenplay_value=list(screenplay.required_copy_texts),
            reason=(
                "the brief's call to action is neither required copy nor in any shot's "
                "dialogue or staging; the treatment alone never reaches the screen"
            ),
        )

    def _check_hook(  # noqa: PLR0913 - the rule needs the whole context
        self,
        screenplay: Screenplay,
        fields: dict[str, Any],
        records: dict[str, Any],
        body: str,
        found: BriefConformance,
    ) -> None:
        hook = str(fields.get("hook") or "").strip()
        if not hook:
            return
        treatment = screenplay.treatment
        targets = [
            treatment.hook.opening_question,
            treatment.hook.promise,
            treatment.premise,
            treatment.title,
        ]
        if any(normalize(hook) in normalize(item) for item in targets if item):
            return
        self._add(
            found,
            records,
            code="HOOK_CHANGED",
            path="hook",
            brief_value=hook,
            screenplay_value=treatment.hook.model_dump(),
            reason="the screenplay opens on a different hook than the one the brief fixes",
        )

    def _check_aspect(
        self,
        screenplay: Screenplay,
        fields: dict[str, Any],
        records: dict[str, Any],
        found: BriefConformance,
    ) -> None:
        """The screenplay may not describe a frame other than the approved one."""

        aspect = str(get_path(fields, "aspect_ratio") or "").strip()
        if not aspect:
            return
        prose = " ".join(
            part
            for part in (
                screenplay.treatment.visual_direction,
                screenplay.treatment.tone_direction,
                *(scene.description for scene in screenplay.scenes),
            )
            if part
        )
        mentioned = {
            match for match in re.findall(r"\b\d{1,2}\s*:\s*\d{1,2}\b", prose)
        }
        conflicting = sorted(
            {
                item.replace(" ", "")
                for item in mentioned
                if item.replace(" ", "") in ASPECT_RATIOS and item.replace(" ", "") != aspect
            }
        )
        if conflicting:
            self._add(
                found,
                records,
                code="ASPECT_RATIO_CHANGED",
                path="aspect_ratio",
                brief_value=aspect,
                screenplay_value=conflicting,
                reason="the screenplay describes a frame the brief did not approve",
            )

    def _check_prohibitions(
        self, screenplay: Screenplay, prohibitions: list[str], found: BriefConformance
    ) -> None:
        """What the user forbade, in the user's own sentence, back on screen."""

        if not prohibitions:
            return
        surfaces = _surfaces(screenplay)
        for sentence in prohibitions:
            needle = normalize(sentence)
            if not needle:
                continue
            terms = prohibited_terms(sentence)
            reported: set[tuple[str, str]] = set()
            for where, text, _compiled, spoken in surfaces:
                if (where, text) in reported:
                    continue
                folded = normalize(text)
                # The user's whole sentence, verbatim, put back on screen -
                # said by a character, or shown as claim or copy. Prose that
                # restates the rule ("no violence" in a tone note) is the
                # director agreeing, and is judged by its terms below.
                if spoken and needle in folded:
                    reported.add((where, text))
                    found.violations.append(
                        BriefViolation(
                            code="PROHIBITION_BREACHED",
                            brief_path="prohibitions",
                            brief_value=sentence,
                            screenplay_value=text,
                            severity=BLOCKING,
                            reason="the screenplay puts a sentence the user forbade on screen",
                            location=where,
                            brief_source="USER_STATED",
                        )
                    )
                    continue
                # The thing the sentence forbids, in what is shown or done -
                # an action's object, a staging note, a beat summary - not
                # only in what is said. A mention that is itself ruled out
                # ("no blood" in a tone note) is the director agreeing.
                hit = next(
                    (
                        term
                        for term in terms
                        if any(
                            not _negated(text, position)
                            for position in aligned_occurrences(text, term)
                        )
                    ),
                    None,
                )
                if hit is None:
                    continue
                reported.add((where, text))
                found.violations.append(
                    BriefViolation(
                        code="PROHIBITION_BREACHED",
                        brief_path="prohibitions",
                        brief_value=sentence,
                        screenplay_value=text,
                        severity=BLOCKING,
                        reason=f"the screenplay shows or does what the user forbade ({hit})",
                        location=where,
                        brief_source="USER_STATED",
                    )
                )


__all__ = [
    "ADVISORY",
    "BLOCKING",
    "DEFAULT_DURATION_TOLERANCE",
    "BriefConformance",
    "BriefViolation",
    "ScreenplayBriefValidator",
    "enforceable_prohibition",
    "prohibited_terms",
]
