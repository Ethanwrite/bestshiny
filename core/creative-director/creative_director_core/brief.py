"""Deterministic brief extraction and gap analysis.

The rules here are the floor, not the ceiling: a model (through
``ModelRoleRuntime``) may extract more from the same text, but its output is
merged through the same guarded patch path and can never overwrite something
the user already said. When no model is reachable the dialogue still works,
and the revision records which reasoner produced it.
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
    BRIEF_FIELD_SPECS,
    FORMAT_DEFAULTS,
    MAX_QUESTIONS_PER_TURN,
    FieldWeight,
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
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


@dataclass(frozen=True)
class GapReport:
    code: str
    path: str
    weight: int
    question: str
    already_asked: bool


@dataclass(frozen=True)
class BriefAnalysis:
    """What is missing, what to ask next, and whether a proposal can stand."""

    gaps: list[GapReport]
    questions: list[GapReport]
    proposable: bool
    applied_defaults: dict[str, Any] = field(default_factory=dict)

    def completeness(self) -> dict[str, Any]:
        return {
            "gaps": [
                {
                    "code": gap.code,
                    "path": gap.path,
                    "weight": gap.weight,
                    "already_asked": gap.already_asked,
                }
                for gap in self.gaps
            ],
            "applied_defaults": self.applied_defaults,
        }


class BriefEngine:
    """Rules-based extraction plus per-format gap analysis."""

    version = "creative-brief-v1"

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
    def extract(cls, text: str, current: dict[str, Any]) -> dict[str, Any]:
        """Extract a patch of brief fields this text supports.

        Only empty fields are filled: the user's earlier answers are truth and
        later, vaguer text must not erode them.
        """

        patch: dict[str, Any] = {}

        def fill(path: str, value: Any) -> None:
            if _present(value) and not _present(get_path(current, path)):
                set_path(patch, path, value)

        detected_format = cls.detect_format(text)
        if detected_format is not None:
            fill("format", detected_format.value)

        minutes = _DURATION_MINUTES.search(text)
        seconds = _DURATION_SECONDS.search(text)
        if minutes:
            fill("duration_seconds", int(minutes.group(1)) * 60)
        elif seconds:
            fill("duration_seconds", int(seconds.group(1)))

        aspect = _ASPECT.search(text)
        if aspect:
            fill("aspect_ratio", aspect.group(1))
        elif "竖屏" in text or "vertical" in text.casefold():
            fill("aspect_ratio", "9:16")
        elif "横屏" in text or "widescreen" in text.casefold():
            fill("aspect_ratio", "16:9")

        episodes = _EPISODES.search(text)
        if episodes:
            fill("episode_count", int(episodes.group(1)))

        fill("platform", cls._match(text, _PLATFORM_CUES))
        medium = cls._match(text, _MEDIUM_CUES)
        if medium:
            fill("visual_style.medium", medium)

        tones = [
            value
            for terms, value in _TONE_CUES
            if any(term.casefold() in text.casefold() for term in terms)
        ]
        if tones and not _present(get_path(current, "tone")):
            set_path(patch, "tone", tones)

        name_match = _CH_NAME_INTRO.search(text) or _EN_NAME_INTRO.search(text)
        if name_match and not _present(get_path(current, "characters")):
            set_path(
                patch,
                "characters",
                [{"name": name_match.group(1).strip(), "role": "protagonist", "look": ""}],
            )

        product = _PRODUCT_INTRO.search(text)
        if product:
            fill("product.name", product.group(1).strip(" ，。,."))
        elif not _present(get_path(current, "product.name")):
            quoted = _QUOTED.search(text)
            lowered = text.casefold()
            if quoted and any(term in lowered for term in ("产品", "品牌", "product", "brand")):
                set_path(patch, "product.name", quoted.group(1).strip())

        location = _LOCATION_INTRO.search(text)
        if location:
            candidate = location.group(1).strip()
            # "在30秒内" style matches are numbers, not places.
            if not re.fullmatch(r"[\d\s]+", candidate):
                fill("setting.location", candidate)
        fill("setting.time", cls._match(text, _TIME_CUES))

        # The first substantive user text becomes the logline candidate: the
        # core idea is whatever they opened with, until they replace it.
        stripped = text.strip()
        if len(stripped) >= 12 and not _present(get_path(current, "logline")):
            set_path(patch, "logline", stripped[:500])
        return patch

    @staticmethod
    def merge(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        merged = deepcopy(current)

        def walk(target: dict[str, Any], source: dict[str, Any]) -> None:
            for key, value in source.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    walk(target[key], value)
                elif not _present(target.get(key)):
                    target[key] = value

        walk(merged, deepcopy(patch))
        return merged

    @classmethod
    def analyze(
        cls,
        fields: dict[str, Any],
        *,
        format_value: str,
        asked_codes: set[str],
    ) -> BriefAnalysis:
        """Gap analysis: what is missing and worth one of this turn's questions.

        A gap becomes a question only when its weight is HIGH or CRITICAL for
        the selected format and it has not been asked before. Once asked, an
        unanswered non-critical gap is defaulted at proposal time instead of
        being asked again - repetition is the questionnaire smell this exists
        to avoid.
        """

        gaps: list[GapReport] = []
        for spec in BRIEF_FIELD_SPECS:
            weight = spec.weights.get(format_value, spec.default_weight)
            if format_value == CreativeFormat.UNSPECIFIED.value:
                # Until the format is known, only format and logline are worth
                # asking for; every other weight depends on the answer.
                weight = weight if spec.code in {"FORMAT", "LOGLINE"} else FieldWeight.IRRELEVANT
            if weight is FieldWeight.IRRELEVANT:
                continue
            if _present(get_path(fields, spec.path)):
                continue
            gaps.append(
                GapReport(
                    code=spec.code,
                    path=spec.path,
                    weight=int(weight),
                    question=spec.question,
                    already_asked=spec.code in asked_codes,
                )
            )

        askable = [
            gap
            for gap in gaps
            if gap.weight >= int(FieldWeight.HIGH) and not gap.already_asked
        ]
        askable.sort(key=lambda gap: (-gap.weight, gap.code))
        questions = askable[:MAX_QUESTIONS_PER_TURN]

        critical_open = [
            gap for gap in gaps if gap.weight >= int(FieldWeight.CRITICAL) and not gap.already_asked
        ]
        # Proposable once nothing CRITICAL remains unasked: an asked-but-still
        # -open critical gap means the user declined to answer, and the
        # proposal then says what was assumed rather than blocking forever.
        proposable = not critical_open and not questions

        applied_defaults: dict[str, Any] = {}
        if proposable:
            for path, value in FORMAT_DEFAULTS.get(format_value, {}).items():
                if not _present(get_path(fields, path)):
                    applied_defaults[path] = value
        return BriefAnalysis(
            gaps=gaps,
            questions=questions,
            proposable=proposable,
            applied_defaults=applied_defaults,
        )
