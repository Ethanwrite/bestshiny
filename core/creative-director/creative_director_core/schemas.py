"""Structured vocabulary of the creative director.

The brief is a set of typed fields, never one growing prompt string. Each
field carries a per-format value weight, and the dialogue only ever asks for
fields that are missing *and* high-value for the selected format - the
question list is computed from the gaps, so a user who supplies everything up
front is asked nothing and a fixed questionnaire cannot exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

from production_domain.models import CreativeFormat

ANCHOR_PROMPT_VERSION = "creative-anchor-v1"

#: The most questions one director turn may ask. Clarification is a dialogue,
#: not a form; anything beyond the highest-value gaps waits for the next turn
#: or is defaulted at proposal time.
MAX_QUESTIONS_PER_TURN = 3


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

#: Values a proposal may assume when a HIGH (never CRITICAL) gap stays open.
#: Every applied default is recorded in the revision's completeness report, so
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
