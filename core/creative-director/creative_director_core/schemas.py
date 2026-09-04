"""Structured vocabulary of the creative director.

The brief is a set of typed fields, never one growing prompt string. Each
field carries a per-format value weight, and the dialogue only ever asks for
fields that are missing *and* high-value for the selected format - the
question list is computed from the gaps, so a user who supplies everything up
front is asked nothing and a fixed questionnaire cannot exist.

Since 2026-09-02 the director's model turn is a validated
``DirectorTurnResult``: a message in the user's language plus explicit brief
*operations* (SET / REPLACE / UPSERT / REMOVE / KEEP) with provenance, the
questions it considers answered or skipped, the assumptions it proposes and
its creative notes. The screenplay the model writes after brief approval is a
validated ``Screenplay``. Both schemas are strict: a malformed model reply is
rejected or degraded on record, never turned into a 500.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any, Literal

from production_domain.models import CreativeFormat
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ANCHOR_PROMPT_VERSION = "creative-anchor-v2"

#: The most questions one director turn may ask. Clarification is a dialogue,
#: not a form; anything beyond the highest-value gaps waits for the next turn
#: or is defaulted at proposal time.
MAX_QUESTIONS_PER_TURN = 3

#: The largest cast the pipeline can carry end to end. Every character that
#: appears in a beat or a shot gets its own key visual and a locked
#: CharacterIdentityVersion, so this single number bounds the brief's cast, the
#: screenplay schema, the director's prompt, anchor derivation, the compiler
#: and the UI. A screenplay over the cap is *refused* at validation - the cast
#: is never silently sliced, because a sliced character still acts on screen
#: with no canonical reference behind it.
MAX_CAST = 12

#: Locations that may each receive their own canonical SCENE key visual. Equal
#: to the screenplay's own scene cap: every scene a beat actually plays in is
#: anchored, so the frame-anchor planner can always resolve one.
MAX_SCENE_ANCHORS = 12

#: Distinct props that may receive their own key visual. Unlike the cast this
#: is a budget, not a contract: a prop beyond it is recorded as uncovered with
#: its reason rather than refusing the screenplay.
MAX_PROP_ANCHORS = 6


class FieldWeight(IntEnum):
    """How much a missing field hurts, for one format."""

    IRRELEVANT = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class StructuredActionKind(StrEnum):
    GENERATE_KEY_VISUAL = "GENERATE_KEY_VISUAL"
    CREATE_EPISODE = "CREATE_EPISODE"
    COMPILE_EPISODE = "COMPILE_EPISODE"
    OPEN_OBLIGATION = "OPEN_OBLIGATION"
    ESTABLISH_FACT = "ESTABLISH_FACT"
    LOCK_CHARACTER_IDENTITY = "LOCK_CHARACTER_IDENTITY"
    LOCK_PROJECT_STYLE = "LOCK_PROJECT_STYLE"


class BriefOperationKind(StrEnum):
    """Explicit merge semantics for one brief field.

    SET fills an empty field; REPLACE overwrites an existing value and is only
    honoured when the user said so; UPSERT updates or adds one member of a
    list (characters, selling points); REMOVE deletes a value or member on the
    user's explicit request; KEEP records that an existing fact was confirmed.
    """

    SET = "SET"
    REPLACE = "REPLACE"
    UPSERT = "UPSERT"
    REMOVE = "REMOVE"
    KEEP = "KEEP"


class ProvenanceSource(StrEnum):
    """Who established a brief value. Only the first three are user facts."""

    USER_STATED = "USER_STATED"
    USER_EDIT = "USER_EDIT"
    ASSUMPTION_ACCEPTED = "ASSUMPTION_ACCEPTED"
    MODEL_INFERRED = "MODEL_INFERRED"
    DEFAULT = "DEFAULT"


USER_ESTABLISHED_SOURCES = frozenset(
    {
        ProvenanceSource.USER_STATED.value,
        ProvenanceSource.USER_EDIT.value,
        ProvenanceSource.ASSUMPTION_ACCEPTED.value,
    }
)
ASSUMED_SOURCES = frozenset({ProvenanceSource.MODEL_INFERRED.value, ProvenanceSource.DEFAULT.value})


class QuestionStatus(StrEnum):
    UNASKED = "UNASKED"
    ASKED = "ASKED"
    ANSWERED = "ANSWERED"
    SKIPPED_BY_USER = "SKIPPED_BY_USER"
    ASSUMPTION_ACCEPTED = "ASSUMPTION_ACCEPTED"


#: Statuses that satisfy a CRITICAL field for proposal and approval.
RESOLVED_QUESTION_STATUSES = frozenset(
    {QuestionStatus.ANSWERED.value, QuestionStatus.ASSUMPTION_ACCEPTED.value}
)


class ReasonCode(StrEnum):
    """Machine-readable outcomes recorded on turns, revisions and replies."""

    MODEL_REPLY = "MODEL_REPLY"
    MODEL_OPERATIONS_APPLIED = "MODEL_OPERATIONS_APPLIED"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_BUDGET_REFUSED = "MODEL_BUDGET_REFUSED"
    MODEL_RUNTIME_NOT_CONFIGURED = "MODEL_RUNTIME_NOT_CONFIGURED"
    MODEL_CALL_ERROR = "MODEL_CALL_ERROR"
    DETERMINISTIC_FILL = "DETERMINISTIC_FILL"
    DETERMINISTIC_FALLBACK = "DETERMINISTIC_FALLBACK"
    OPERATIONS_REJECTED = "OPERATIONS_REJECTED"
    CONTEXT_COMPRESSED = "CONTEXT_COMPRESSED"
    SKILL_LOADED = "SKILL_LOADED"
    SKILL_UNAVAILABLE = "SKILL_UNAVAILABLE"
    BRIEF_NOT_PROPOSED = "BRIEF_NOT_PROPOSED"
    CRITICAL_UNANSWERED = "CRITICAL_UNANSWERED"
    ASSUMPTIONS_UNCONFIRMED = "ASSUMPTIONS_UNCONFIRMED"
    REVISION_SUPERSEDED = "REVISION_SUPERSEDED"
    DETERMINISTIC_SCREENPLAY_UNCONFIRMED = "DETERMINISTIC_SCREENPLAY_UNCONFIRMED"
    #: The screenplay disagrees with a fact the user established in the
    #: approved brief. A redraft is the remedy, never a plain retry.
    SCREENPLAY_CONTRADICTS_BRIEF = "SCREENPLAY_CONTRADICTS_BRIEF"
    #: The screenplay departs from something the director itself inferred:
    #: enrichment, recorded and shown, never a blocked approval.
    SCREENPLAY_BRIEF_ADVISORY = "SCREENPLAY_BRIEF_ADVISORY"
    REQUIRED_ANCHORS_NOT_READY = "REQUIRED_ANCHORS_NOT_READY"
    OPTIONAL_ANCHORS_NOT_TERMINAL = "OPTIONAL_ANCHORS_NOT_TERMINAL"
    ANCHOR_SKIPPED = "ANCHOR_SKIPPED"
    ANCHOR_SUPERSEDED = "ANCHOR_SUPERSEDED"
    STYLE_LOCK_INHERITED = "STYLE_LOCK_INHERITED"
    STYLE_LOCK_REQUIRES_USER = "STYLE_LOCK_REQUIRES_USER"
    LOCK_SERVICES_UNAVAILABLE = "LOCK_SERVICES_UNAVAILABLE"
    LOCK_FAILED = "LOCK_FAILED"
    BIBLE_LOCK_INCOMPLETE = "BIBLE_LOCK_INCOMPLETE"
    CHARACTER_IDENTITY_NOT_COVERED = "CHARACTER_IDENTITY_NOT_COVERED"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    #: The brief head moved while the director was thinking, so the model's
    #: operations were re-applied to the newer revision under the same
    #: provenance rules instead of overwriting it.
    BRIEF_REBASED = "BRIEF_REBASED"
    #: The caller pinned a brief revision that is no longer the head.
    BRIEF_REVISION_CHANGED = "BRIEF_REVISION_CHANGED"
    #: The session left the stage this operation was reasoned against.
    SESSION_STAGE_CHANGED = "SESSION_STAGE_CHANGED"
    #: A screenplay was written against a revision that is no longer head.
    SCREENPLAY_REVISION_CHANGED = "SCREENPLAY_REVISION_CHANGED"
    #: A screenplay was written against a stage the session has left.
    SCREENPLAY_STAGE_CHANGED = "SCREENPLAY_STAGE_CHANGED"
    #: The approved brief moved under a screenplay while it was being written.
    SCREENPLAY_BRIEF_CHANGED = "SCREENPLAY_BRIEF_CHANGED"
    #: At least one USER_STATED claim could not be found in the user's own
    #: words and was recorded as the director's inference instead.
    EVIDENCE_UNVERIFIED = "EVIDENCE_UNVERIFIED"
    #: At least one claimed skip was not honoured: the question was never
    #: asked, or the user never declined it in their own words.
    SKIP_UNVERIFIED = "SKIP_UNVERIFIED"


#: Reason codes whose failure a caller may retry without changing the input.
RETRYABLE_REASON_CODES = frozenset(
    {
        ReasonCode.MODEL_UNAVAILABLE.value,
        ReasonCode.MODEL_BUDGET_REFUSED.value,
        ReasonCode.MODEL_OUTPUT_INVALID.value,
        ReasonCode.MODEL_CALL_ERROR.value,
        ReasonCode.LOCK_FAILED.value,
    }
)


@dataclass(frozen=True)
class BriefFieldSpec:
    """One brief field: where it lives, what to ask, and who needs it."""

    code: str
    #: Dotted path into the brief fields JSON, e.g. "setting.location".
    path: str
    question: str
    #: Per-format weight; formats absent here fall back to `default_weight`.
    weights: dict[str, FieldWeight]
    default_weight: FieldWeight = FieldWeight.MEDIUM


def _w(weight: FieldWeight, *formats: CreativeFormat) -> dict[str, FieldWeight]:
    return {value.value: weight for value in formats}


_ALL = tuple(value for value in CreativeFormat if value is not CreativeFormat.UNSPECIFIED)
_COMMERCE = (CreativeFormat.ADVERTISEMENT, CreativeFormat.PRODUCT_SHOWCASE)
COMMERCE_FORMATS = frozenset(value.value for value in _COMMERCE)

BRIEF_FIELD_SPECS: tuple[BriefFieldSpec, ...] = (
    BriefFieldSpec(
        code="FORMAT",
        path="format",
        question=(
            "What kind of piece is this - short drama, advertisement, product showcase, "
            "social short, music visual, fashion, beauty, or concept film?"
        ),
        weights=_w(FieldWeight.CRITICAL, *_ALL),
        default_weight=FieldWeight.CRITICAL,
    ),
    BriefFieldSpec(
        code="LOGLINE",
        path="logline",
        question="In one or two sentences, what is the core idea or story?",
        weights=_w(FieldWeight.CRITICAL, *_ALL),
        default_weight=FieldWeight.CRITICAL,
    ),
    BriefFieldSpec(
        code="DURATION",
        path="duration_seconds",
        question="Roughly how long should it run, in seconds?",
        weights=_w(FieldWeight.HIGH, *_ALL),
        default_weight=FieldWeight.HIGH,
    ),
    BriefFieldSpec(
        code="PLATFORM",
        path="platform",
        question="Where will it be published (Douyin/TikTok, Instagram, YouTube, in-store...)?",
        weights={
            **_w(FieldWeight.HIGH, CreativeFormat.SOCIAL_SHORT, *_COMMERCE),
            **_w(FieldWeight.MEDIUM, CreativeFormat.SHORT_DRAMA, CreativeFormat.MUSIC_VISUAL),
        },
        default_weight=FieldWeight.LOW,
    ),
    BriefFieldSpec(
        code="VISUAL_STYLE",
        path="visual_style.medium",
        question=(
            "What should it look like - live-action photographic, anime, 3D render, "
            "film noir, documentary...? Any reference works?"
        ),
        weights=_w(FieldWeight.HIGH, *_ALL),
        default_weight=FieldWeight.HIGH,
    ),
    BriefFieldSpec(
        code="TONE",
        path="tone",
        question="What is the emotional tone (warm, suspenseful, funny, epic, minimal...)?",
        weights=_w(FieldWeight.MEDIUM, *_ALL),
        default_weight=FieldWeight.MEDIUM,
    ),
    BriefFieldSpec(
        code="PROTAGONIST",
        path="characters",
        question="Who is on screen - name and one line about who they are?",
        weights={
            **_w(FieldWeight.CRITICAL, CreativeFormat.SHORT_DRAMA),
            **_w(
                FieldWeight.HIGH,
                CreativeFormat.FASHION_LOOKBOOK,
                CreativeFormat.BEAUTY_TUTORIAL,
                CreativeFormat.MUSIC_VISUAL,
            ),
            **_w(FieldWeight.MEDIUM, CreativeFormat.ADVERTISEMENT, CreativeFormat.SOCIAL_SHORT),
        },
        default_weight=FieldWeight.LOW,
    ),
    BriefFieldSpec(
        code="SETTING",
        path="setting.location",
        question="Where does it take place (one main location is enough)?",
        weights={
            **_w(FieldWeight.HIGH, CreativeFormat.SHORT_DRAMA, CreativeFormat.CONCEPT_FILM),
            **_w(FieldWeight.MEDIUM, *_COMMERCE, CreativeFormat.MUSIC_VISUAL),
        },
        default_weight=FieldWeight.MEDIUM,
    ),
    BriefFieldSpec(
        code="PRODUCT",
        path="product.name",
        question="What product or brand is this for, and what one thing must land?",
        weights={
            **_w(FieldWeight.CRITICAL, *_COMMERCE),
            **_w(FieldWeight.MEDIUM, CreativeFormat.BEAUTY_TUTORIAL, CreativeFormat.FASHION_LOOKBOOK),
        },
        default_weight=FieldWeight.IRRELEVANT,
    ),
    BriefFieldSpec(
        code="CTA",
        path="call_to_action",
        question="What should the viewer do at the end (buy, follow, visit...)?",
        weights=_w(FieldWeight.HIGH, CreativeFormat.ADVERTISEMENT),
        default_weight=FieldWeight.IRRELEVANT,
    ),
    BriefFieldSpec(
        code="MUSIC",
        path="music.mood",
        question="What is the music like - genre or mood, and any beat to cut on?",
        weights=_w(FieldWeight.CRITICAL, CreativeFormat.MUSIC_VISUAL),
        default_weight=FieldWeight.IRRELEVANT,
    ),
    BriefFieldSpec(
        code="AUDIENCE",
        path="audience",
        question="Who is this for?",
        weights=_w(FieldWeight.MEDIUM, *_COMMERCE, CreativeFormat.SOCIAL_SHORT),
        default_weight=FieldWeight.LOW,
    ),
)

SPECS_BY_CODE: dict[str, BriefFieldSpec] = {spec.code: spec for spec in BRIEF_FIELD_SPECS}
SPECS_BY_PATH: dict[str, BriefFieldSpec] = {spec.path: spec for spec in BRIEF_FIELD_SPECS}

#: Values a proposal may assume when a HIGH (never CRITICAL) gap stays open.
#: Every applied default is recorded in the revision's provenance as DEFAULT,
#: shown to the user as an assumption, and must be accepted before approval -
#: an assumed value is visible rather than indistinguishable from an answer.
FORMAT_DEFAULTS: dict[str, dict[str, object]] = {
    CreativeFormat.SHORT_DRAMA.value: {
        "duration_seconds": 60,
        "aspect_ratio": "9:16",
        "visual_style.medium": "cinematic live-action",
    },
    CreativeFormat.ADVERTISEMENT.value: {
        "duration_seconds": 30,
        "aspect_ratio": "9:16",
        "visual_style.medium": "polished commercial live-action",
        "call_to_action": "learn more",
    },
    CreativeFormat.PRODUCT_SHOWCASE.value: {
        "duration_seconds": 30,
        "aspect_ratio": "1:1",
        "visual_style.medium": "studio product photography",
    },
    CreativeFormat.SOCIAL_SHORT.value: {
        "duration_seconds": 15,
        "aspect_ratio": "9:16",
        "visual_style.medium": "handheld ugc realism",
    },
    CreativeFormat.MUSIC_VISUAL.value: {
        "duration_seconds": 30,
        "aspect_ratio": "9:16",
        "visual_style.medium": "stylized music video",
    },
    CreativeFormat.FASHION_LOOKBOOK.value: {
        "duration_seconds": 20,
        "aspect_ratio": "9:16",
        "visual_style.medium": "editorial fashion film",
    },
    CreativeFormat.BEAUTY_TUTORIAL.value: {
        "duration_seconds": 45,
        "aspect_ratio": "9:16",
        "visual_style.medium": "soft-light beauty macro",
    },
    CreativeFormat.CONCEPT_FILM.value: {
        "duration_seconds": 45,
        "aspect_ratio": "16:9",
        "visual_style.medium": "atmospheric concept film",
    },
}

#: Every brief path a model or an editor may touch, with its value kind.
#: Anything else is dropped on validation: a model invents no fields.
SCALAR_STRING_PATHS = frozenset(
    {
        "logline",
        "platform",
        "aspect_ratio",
        "hook",
        "call_to_action",
        "audience",
        "visual_style.medium",
        "visual_style.palette",
        "setting.location",
        "setting.time",
        "product.name",
        "music.mood",
        "music.reference",
    }
)
INTEGER_PATHS = frozenset({"duration_seconds", "episode_count"})
STRING_LIST_PATHS = frozenset({"tone", "product.selling_points"})
CHARACTER_LIST_PATH = "characters"
NESTED_OBJECT_PATHS = frozenset({"visual_style", "setting", "product", "music"})
ALLOWED_BRIEF_PATHS = (
    SCALAR_STRING_PATHS
    | INTEGER_PATHS
    | STRING_LIST_PATHS
    | {CHARACTER_LIST_PATH, "format"}
    | NESTED_OBJECT_PATHS
)
ASPECT_RATIOS = frozenset({"9:16", "16:9", "1:1", "4:3", "3:4", "21:9", "3:2", "2:3"})


# ---------------------------------------------------------------- turn result
class BriefOperation(BaseModel):
    """One explicit change to the brief, as the model (or the editor) states it."""

    model_config = ConfigDict(extra="ignore")

    op: BriefOperationKind
    path: str = Field(min_length=1, max_length=80)
    value: Any = None
    #: The user's own words that justify this operation. Verbatim, because the
    #: server checks it against the user's messages before honouring a
    #: USER_STATED claim; a paraphrase is not a quote.
    evidence: str = Field(default="", max_length=400)
    #: Which user turn the quote comes from, when the model names one. Naming a
    #: turn that is not on record fails the proof; naming none searches every
    #: user turn in the session.
    evidence_turn_id: str | None = Field(default=None, max_length=36)
    #: USER_STATED: the user said it; INFERRED: the director's reading. Only
    #: USER_STATED may replace or remove something the user established - and
    #: the model's word for it is a claim, not the finding: the service
    #: verifies the quote and demotes an unprovable claim to INFERRED.
    confidence: Literal["USER_STATED", "INFERRED"] = "INFERRED"

    @field_validator("path")
    @classmethod
    def _normalize_path(cls, value: str) -> str:
        return value.strip()


class UnresolvedQuestion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str = Field(min_length=1, max_length=40)
    question: str = Field(default="", max_length=400)


class SkippedQuestionClaim(BaseModel):
    """The model's claim that the user declined one question, with its proof.

    A skip permanently silences a gap and can make a brief proposable, so it is
    honoured only for a question that was actually asked *and* whose decline
    the user's own words support.
    """

    model_config = ConfigDict(extra="ignore")

    code: str = Field(min_length=1, max_length=40)
    evidence: str = Field(default="", max_length=400)
    evidence_turn_id: str | None = Field(default=None, max_length=36)

    @field_validator("code")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()[:40]


class DirectorAssumption(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = Field(min_length=1, max_length=80)
    value: Any = None
    rationale: str = Field(default="", max_length=400)


class DirectorTurnResult(BaseModel):
    """The validated shape of one DIRECTOR model turn.

    ``assistant_message`` is what the user reads. Everything else is
    structure the service applies under its own rules; nothing here is
    trusted to overwrite a user fact on the model's say-so.
    """

    model_config = ConfigDict(extra="ignore")

    assistant_message: str = Field(min_length=1, max_length=4000)
    brief_operations: list[BriefOperation] = Field(default_factory=list, max_length=40)
    answered_question_codes: list[str] = Field(default_factory=list, max_length=20)
    skipped_question_codes: list[str] = Field(default_factory=list, max_length=20)
    #: The same skips with the user's words behind them. A bare code in
    #: ``skipped_question_codes`` carries no proof and is recorded but not
    #: honoured; an entry here is honoured when its quote verifies.
    skipped_questions: list[SkippedQuestionClaim] = Field(default_factory=list, max_length=20)
    unresolved_questions: list[UnresolvedQuestion] = Field(default_factory=list, max_length=10)
    assumptions: list[DirectorAssumption] = Field(default_factory=list, max_length=20)
    creative_notes: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("answered_question_codes", "skipped_question_codes")
    @classmethod
    def _upper_codes(cls, value: list[str]) -> list[str]:
        return [str(code).strip().upper()[:40] for code in value if str(code).strip()]

    @field_validator("creative_notes")
    @classmethod
    def _trim_notes(cls, value: list[str]) -> list[str]:
        return [str(note).strip()[:600] for note in value if str(note).strip()]


# ------------------------------------------------------------------ screenplay
#: The one-action vocabulary the narrative compiler parses. A shot names
#: exactly one of these (or is a dialogue shot); the renderer turns it into a
#: line the compiler maps to the same canonical action.
ACTION_VERBS: tuple[str, ...] = (
    "enter",
    "exit",
    "walk",
    "look",
    "pick_up",
    "raise",
    "place",
    "turn",
    "stop",
    "sit",
    "stand",
    "open",
    "close",
    "push",
    "pull",
)
SHOT_TYPES: tuple[str, ...] = (
    "WIDE",
    "MEDIUM",
    "CLOSE",
    "CLOSE_UP",
    "EXTREME_CLOSE_UP",
    "INSERT",
    "OVER_SHOULDER",
    "TWO_SHOT",
    "DIALOGUE",
)
TIMES_OF_DAY: tuple[str, ...] = ("DAY", "NIGHT", "DUSK", "DAWN")


def _clean_text(value: Any, limit: int) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text[:limit]


class ShotAction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    actor: str = Field(min_length=1, max_length=60)
    verb: str = Field(min_length=1, max_length=20)
    object: str = Field(default="", max_length=60)
    target: str = Field(default="", max_length=60)
    #: The director's staging note for the prompt compiler; never parsed.
    description: str = Field(default="", max_length=400)

    @field_validator("verb")
    @classmethod
    def _known_verb(cls, value: str) -> str:
        verb = value.strip().lower().replace(" ", "_").replace("-", "_")
        aliases = {"picks_up": "pick_up", "pickup": "pick_up", "leave": "exit", "leaves": "exit"}
        verb = aliases.get(verb, verb)
        if verb not in ACTION_VERBS:
            raise ValueError(f"unknown action verb {value!r}; use one of {', '.join(ACTION_VERBS)}")
        return verb

    @field_validator("actor", "object", "target")
    @classmethod
    def _clean_names(cls, value: str) -> str:
        return _clean_text(value, 60)

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str) -> str:
        return _clean_text(value, 400)


class ShotDialogue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    speaker: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=400)

    @field_validator("speaker", "text")
    @classmethod
    def _one_line(cls, value: str) -> str:
        cleaned = _clean_text(value, 400)
        if not cleaned:
            raise ValueError("dialogue must not be empty")
        return cleaned


class ScreenplayShot(BaseModel):
    """One planned shot: exactly one primary visible action or one line."""

    model_config = ConfigDict(extra="ignore")

    sequence: int = Field(ge=1, le=200)
    shot_type: str = Field(default="MEDIUM", max_length=40)
    duration: float = Field(default=5.0, ge=1.0, le=15.0)
    action: ShotAction | None = None
    dialogue: ShotDialogue | None = None
    start_state: str = Field(default="", max_length=400)
    end_state: str = Field(default="", max_length=400)
    gaze_target: str = Field(default="", max_length=120)
    continuity_obligations: list[str] = Field(default_factory=list, max_length=8)
    anchors: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("shot_type")
    @classmethod
    def _shot_type(cls, value: str) -> str:
        normalized = value.strip().upper().replace(" ", "_").replace("-", "_") or "MEDIUM"
        aliases = {"CLOSEUP": "CLOSE_UP", "ECU": "EXTREME_CLOSE_UP", "CU": "CLOSE_UP"}
        normalized = aliases.get(normalized, normalized)
        if normalized not in SHOT_TYPES:
            raise ValueError(f"unknown shot type {value!r}")
        return normalized

    @field_validator("start_state", "end_state", "gaze_target")
    @classmethod
    def _clean_states(cls, value: str) -> str:
        return _clean_text(value, 400)

    @field_validator("continuity_obligations", "anchors")
    @classmethod
    def _clean_lists(cls, value: list[str]) -> list[str]:
        return [_clean_text(item, 200) for item in value if _clean_text(item, 200)]

    @model_validator(mode="after")
    def _exactly_one_primary(self) -> ScreenplayShot:
        if (self.action is None) == (self.dialogue is None):
            raise ValueError(
                f"shot {self.sequence} must carry exactly one primary element: an action or a line"
            )
        if self.dialogue is not None:
            self.shot_type = "DIALOGUE"
        elif self.shot_type == "DIALOGUE":
            self.shot_type = "MEDIUM"
        return self


class ScreenplayBeat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sequence: int = Field(ge=1, le=60)
    intent: str = Field(min_length=1, max_length=40)
    summary: str = Field(default="", max_length=600)
    scene_key: str = Field(min_length=1, max_length=60)
    characters: list[str] = Field(default_factory=list, max_length=8)
    emotional_beat: str = Field(default="", max_length=300)
    shots: list[ScreenplayShot] = Field(min_length=1, max_length=12)

    @field_validator("intent")
    @classmethod
    def _intent(cls, value: str) -> str:
        cleaned = "_".join(_clean_text(value, 40).upper().replace("-", " ").split())
        return cleaned or "BEAT"

    @field_validator("summary", "emotional_beat")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _clean_text(value, 600)


class ScreenplayScene(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str = Field(min_length=1, max_length=60)
    location: str = Field(min_length=1, max_length=80)
    time: str = Field(default="DAY", max_length=20)
    interior: bool | None = None
    description: str = Field(default="", max_length=600)

    @field_validator("time")
    @classmethod
    def _time(cls, value: str) -> str:
        normalized = value.strip().upper()
        aliases = {
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
        normalized = aliases.get(normalized, normalized)
        if normalized not in TIMES_OF_DAY:
            raise ValueError(f"unknown time of day {value!r}")
        return normalized

    @field_validator("location", "description")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _clean_text(value, 600)


class CharacterRelationship(BaseModel):
    model_config = ConfigDict(extra="ignore")

    with_: str = Field(alias="with", min_length=1, max_length=60)
    relation: str = Field(default="", max_length=120)


class ScreenplayCharacter(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str = Field(min_length=1, max_length=60)
    role: str = Field(default="", max_length=120)
    look: str = Field(default="", max_length=400)
    wants: str = Field(default="", max_length=300)
    relationships: list[CharacterRelationship] = Field(default_factory=list, max_length=8)

    @field_validator("name", "role", "look", "wants")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _clean_text(value, 400)


class HookIntent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    opening_question: str = Field(default="", max_length=400)
    promise: str = Field(default="", max_length=400)
    audience_feeling: str = Field(default="", max_length=200)


class Treatment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(default="", max_length=120)
    premise: str = Field(min_length=1, max_length=1200)
    hook: HookIntent = Field(default_factory=HookIntent)
    audience_expectation: str = Field(default="", max_length=600)
    tone_direction: str = Field(default="", max_length=400)
    visual_direction: str = Field(default="", max_length=600)
    ending: str = Field(default="", max_length=600)


class ProductClaim(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claim: str = Field(min_length=1, max_length=300)
    must_preserve: bool = True


class ScreenplayObligation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str = Field(min_length=1, max_length=80)
    promise: str = Field(min_length=1, max_length=400)
    category: str = Field(default="GENERIC", max_length=40)

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        cleaned = "".join(ch if ch.isalnum() or ch in "_-." else "_" for ch in value.strip())
        return cleaned[:80] or "obligation"

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        return _clean_text(value, 40).upper().replace(" ", "_") or "GENERIC"


class Screenplay(BaseModel):
    """The structured screenplay the DIRECTOR model writes; strictly validated."""

    model_config = ConfigDict(extra="ignore")

    treatment: Treatment
    invariants: list[str] = Field(default_factory=list, max_length=40)
    variables: list[str] = Field(default_factory=list, max_length=40)
    characters: list[ScreenplayCharacter] = Field(min_length=1, max_length=MAX_CAST)
    scenes: list[ScreenplayScene] = Field(min_length=1, max_length=MAX_SCENE_ANCHORS)
    beats: list[ScreenplayBeat] = Field(min_length=1, max_length=40)
    product_claims: list[ProductClaim] = Field(default_factory=list, max_length=20)
    required_copy: list[str] = Field(default_factory=list, max_length=20)
    obligations: list[ScreenplayObligation] = Field(default_factory=list, max_length=20)
    unresolved: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("invariants", "variables", "required_copy", "unresolved")
    @classmethod
    def _clean_lists(cls, value: list[str]) -> list[str]:
        return [_clean_text(item, 400) for item in value if _clean_text(item, 400)]

    @property
    def required_copy_texts(self) -> list[str]:
        """The wording of every required copy line, however it is declared."""

        return [str(item) for item in self.required_copy]

    @model_validator(mode="after")
    def _cross_references(self) -> Screenplay:
        scene_keys = {scene.key for scene in self.scenes}
        if len(scene_keys) != len(self.scenes):
            raise ValueError("scene keys must be unique")
        names = {_normalize_name(character.name) for character in self.characters}
        if len(names) != len(self.characters):
            raise ValueError("character names must be unique")
        expected_sequence = 1
        for beat in self.beats:
            if beat.scene_key not in scene_keys:
                raise ValueError(f"beat {beat.sequence} references unknown scene {beat.scene_key!r}")
            if beat.sequence != expected_sequence:
                raise ValueError(f"beats must be numbered consecutively from 1; got {beat.sequence}")
            expected_sequence += 1
            for shot in beat.shots:
                speaker = shot.dialogue.speaker if shot.dialogue else shot.action.actor  # type: ignore[union-attr]
                if _normalize_name(speaker) not in names:
                    raise ValueError(
                        f"beat {beat.sequence} shot {shot.sequence} uses unknown character {speaker!r}"
                    )
            for name in beat.characters:
                if _normalize_name(name) not in names:
                    raise ValueError(f"beat {beat.sequence} lists unknown character {name!r}")
        return self


def _normalize_name(value: str) -> str:
    return " ".join(str(value).casefold().split())


def normalize_name(value: str) -> str:
    """Stable identity for list members (characters, props): case- and space-folded."""

    return _normalize_name(value)
