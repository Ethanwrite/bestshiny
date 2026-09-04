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
from .evidence import normalize
from .schemas import (
    ASPECT_RATIOS,
    CHARACTER_LIST_PATH,
    COMMERCE_FORMATS,
    Screenplay,
    normalize_name,
)

BLOCKING = "BLOCKING"
ADVISORY = "ADVISORY"

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
                self._add(
                    found,
                    records,
                    code="SELLING_POINT_MISSING",
                    path="product.selling_points",
                    brief_value=text,
                    screenplay_value=claims,
                    reason="a selling point the brief establishes is nowhere in the screenplay",
                    force=ADVISORY,
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
        targets = [*screenplay.required_copy_texts, screenplay.treatment.ending]
        if any(normalize(cta) in normalize(item) for item in targets if item):
            return
        self._add(
            found,
            records,
            code="CALL_TO_ACTION_MISSING",
            path="call_to_action",
            brief_value=cta,
            screenplay_value=list(screenplay.required_copy_texts),
            reason="the brief's call to action is neither required copy nor the ending",
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
        spoken = [
            (beat.sequence, shot.sequence, shot.dialogue.text)
            for beat in screenplay.beats
            for shot in beat.shots
            if shot.dialogue is not None
        ]
        for sentence in prohibitions:
            needle = normalize(sentence)
            if not needle:
                continue
            for beat_sequence, shot_sequence, text in spoken:
                if needle in normalize(text):
                    found.violations.append(
                        BriefViolation(
                            code="PROHIBITION_BREACHED",
                            brief_path="prohibitions",
                            brief_value=sentence,
                            screenplay_value=text,
                            severity=BLOCKING,
                            reason="the screenplay puts a sentence the user forbade on screen",
                            location=f"beat {beat_sequence} shot {shot_sequence}",
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
]
