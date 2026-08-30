from __future__ import annotations

import re

from .schemas import (
    ImagePromptCorrectRequest,
    ImagePromptCorrectResult,
    PromptChange,
)


class ImagePromptCorrector:
    """Deterministic corrector with source-language and edit-scope preservation."""

    version = "image-prompt-corrector-v2"

    task_terms = {
        "commercial": ("广告", "campaign", "广告图", "banner", "电商", "ecommerce", "海报"),
        "product": (
            "产品",
            "product",
            "香水",
            "perfume",
            "瓶",
            "bottle",
            "包装",
            "packaging",
            "食品",
            "food",
        ),
        "beauty_fashion": (
            "美妆",
            "beauty",
            "妆",
            "makeup",
            "时尚",
            "fashion",
            "口红",
            "lipstick",
        ),
        "portrait": (
            "人物",
            "portrait",
            "女生",
            "女孩",
            "女人",
            "woman",
            "girl",
            "男人",
            "man",
            "男生",
        ),
        "scene_concept": (
            "场景",
            "scene",
            "室内",
            "interior",
            "建筑",
            "architecture",
            "街道",
            "landscape",
            "环境",
        ),
    }

    editable_terms = {
        "hairstyle": ("发型", "hairstyle", "hair style"),
        "makeup": ("妆", "makeup"),
        "wardrobe": ("服装", "衣服", "wardrobe", "outfit", "clothes"),
        "pose": ("姿势", "pose"),
        "lighting": ("灯光", "光线", "lighting", "relight"),
        "background": ("背景", "background"),
        "camera_angle": ("角度", "机位", "camera angle", "viewpoint"),
        "expression": ("表情", "expression"),
    }

    identity_invariants = [
        "facial structure and proportions",
        "eye shape and spacing",
        "nose, lip and jaw geometry",
        "skin tone and hairline",
        "recognizable asymmetry and signature facial characteristics",
    ]

    identity_invariants_zh = [
        "面部结构和比例",
        "眼型和眼距",
        "鼻子、嘴唇和下颌的形状",
        "肤色和发际线",
        "可识别的不对称特征和个人标志性面部特征",
    ]

    vague_terms = (
        "beautiful",
        "high quality",
        "cinematic",
        "premium",
        "8k",
        "masterpiece",
        "best quality",
        "高级",
        "高质量",
        "电影感",
    )

    vague_phrases_zh = ("高级一点", "更高级", "高级感", "高级", "高质量", "电影感")

    verbatim_constraint_markers_zh = (
        "必须",
        "不要",
        "不得",
        "不能",
        "不应",
        "不许",
        "保持",
        "不变",
        "看向",
        "望向",
        "朝向",
        "左侧",
        "右侧",
        "左手",
        "右手",
        "文字",
        "标签",
        "标志",
        "数量",
    )

    enhancement_by_task = {
        "portrait": (
            "balanced portrait composition with clear subject-background separation; a motivated, "
            "soft directional key that keeps facial planes readable; controlled highlight rolloff; "
            "authentic fine skin texture and natural microdetail"
        ),
        "beauty_fashion": (
            "beauty-editorial visual hierarchy; controlled T-zone and cheekbone highlights; clean "
            "catchlights, under-eye fill and hair separation; retained pores and natural skin microtexture"
        ),
        "product": (
            "product-forward composition with an unobstructed silhouette and label; shaped reflection "
            "gradients, controlled specular highlights, accurate material response, surface microdetail, "
            "and deliberate negative space"
        ),
        "commercial": (
            "clear commercial hero hierarchy with exact product and brand details preserved; intentional "
            "copy-safe negative space, controlled contrast, accurate material response, and a coherent "
            "brand-appropriate palette"
        ),
        "scene_concept": (
            "clear foreground, midground and background hierarchy; motivated directional light, scale "
            "cues, atmospheric depth, controlled contrast, and a restrained coherent color palette"
        ),
        "reference_character_regeneration": (
            "preserve the canonical identity and every unrequested visual variable; make only the explicit "
            "edit while retaining the original pose, expression, wardrobe, camera, lighting and background"
        ),
    }

    enhancement_by_task_zh = {
        "portrait": (
            "使用清晰的人物主体层级和自然的背景分离；用有明确来向的柔和主光保留面部轮廓，"
            "控制高光过渡，保留真实的皮肤和发丝细节"
        ),
        "beauty_fashion": (
            "建立清晰的美妆时尚视觉层级；控制额头、鼻梁和颧骨高光，使用干净的眼神光、眼下补光"
            "和发丝轮廓光，保留真实毛孔与自然皮肤质感"
        ),
        "product": (
            "采用以产品为重点的构图，确保产品轮廓、颜色、材质、标签和包装文字清楚且无遮挡；"
            "使用可控的反射渐变和高光，呈现准确材质反应和表面细节，并安排适量留白"
        ),
        "commercial": (
            "建立清晰的商业主视觉层级，完整保留产品与品牌细节；安排可用于文案的留白，控制对比度和材质反应，"
            "并使用符合品牌气质的统一色彩"
        ),
        "scene_concept": (
            "建立清楚的前景、中景和背景层次；让光线来向符合场景，通过尺度参照、空气透视和可控对比增强空间感，"
            "同时保持克制、统一的色彩"
        ),
        "reference_character_regeneration": (
            "只执行原文明确要求的修改；保留正式人物参考的身份特征，所有未要求修改的姿势、表情、服装、机位、"
            "光线和背景都保持不变"
        ),
    }

    def detect_type(self, prompt: str, request: ImagePromptCorrectRequest) -> str:
        if request.reference_assets:
            return "reference_character_regeneration"
        if request.task_type != "auto":
            return request.task_type
        lowered = prompt.casefold()
        scores = {
            task: sum(term.casefold() in lowered for term in terms) for task, terms in self.task_terms.items()
        }
        best = max(scores, key=scores.get)
        return best if scores[best] else "scene_concept"

    def _explicit_editables(self, prompt: str) -> list[str]:
        lowered = prompt.casefold()
        change_markers = ("修改", "改变", "换", "change", "replace", "edit", "make the")
        if not any(marker in lowered for marker in change_markers):
            return []
        return [
            variable
            for variable, terms in self.editable_terms.items()
            if any(term.casefold() in lowered for term in terms)
        ]

    @staticmethod
    def _is_chinese(prompt: str) -> bool:
        """Use the brief's own language instead of partially translating it."""

        return bool(re.search(r"[\u3400-\u9fff]", prompt))

    @staticmethod
    def _normalize_english_subject(prompt: str) -> str:
        """Normalize prose spacing while keeping text inside quotes byte-for-byte intact."""

        quoted_parts = re.split(r'("[^"\n]*"|\'[^\'\n]*\')', prompt)
        normalized = "".join(
            part if index % 2 else re.sub(r"\s+", " ", part) for index, part in enumerate(quoted_parts)
        ).strip()
        # Repeated terminal exclamation marks add no visual fact and produce awkward `!!;` output.
        return re.sub(r"\s*!{2,}\s*$", "", normalized).rstrip()

    def _remove_vague_quality_terms(self, prompt: str, *, is_chinese: bool) -> str:
        """Remove only known vague quality fillers; never rewrite factual clauses."""

        original = prompt.strip()
        value = original
        terms = self.vague_phrases_zh if is_chinese else self.vague_terms
        for term in sorted(terms, key=len, reverse=True):
            value = re.sub(re.escape(term), "", value, flags=re.IGNORECASE)

        if value == original:
            return original if is_chinese else self._normalize_english_subject(original)

        # Removing a filler may leave adjacent separators. This is deliberately
        # conservative: wording, quoted text, spatial relations and negations stay untouched.
        value = re.sub(r"([,，;；])\s*(?:[,，;；]\s*)+", r"\1", value)
        value = re.sub(r"^[\s,，;；]+|[\s,，;；]+$", "", value)
        value = re.sub(r"\s+([,，。.;；])", r"\1", value)
        value = re.sub(r"[，,]\s*([。.;；])", r"\1", value)
        value = re.sub(r"[ \t]{2,}", " ", value).strip()
        if not is_chinese:
            value = self._normalize_english_subject(value)
        return value or original

    def _verbatim_chinese_constraints(self, prompt: str) -> list[str]:
        clauses = [item.strip() for item in re.split(r"[，,。；;]", prompt) if item.strip()]
        return [
            clause
            for clause in clauses
            if any(marker in clause for marker in self.verbatim_constraint_markers_zh)
            or bool(re.search(r"[\"“”'‘’][^\"“”'‘’]+[\"“”'‘’]", clause))
        ]

    def correct(self, request: ImagePromptCorrectRequest) -> ImagePromptCorrectResult:
        original = request.prompt.strip()
        detected = self.detect_type(original, request)
        identity_mode = detected == "reference_character_regeneration"
        editable = self._explicit_editables(original) if identity_mode else []
        is_chinese = self._is_chinese(original)
        subject = self._remove_vague_quality_terms(original, is_chinese=is_chinese)

        if is_chinese:
            enhancement = self.enhancement_by_task_zh[detected]
            corrected = (
                f"画面主体：{subject}\n"
                f"画面优化：{enhancement}。\n"
                "保持要求：原文中的人物特征、动作、物品、数量、颜色、持物手、精确文字、环境、时间、空间方位、"
                "视线方向和否定要求必须逐字遵守，不得增删或改写。"
            )
            preserved = [
                "原文中的人物、动作、物品、空间关系、凝视方向和否定要求",
                f"原文事实描述原样保留：{subject}",
            ]
            verbatim_spans = self._verbatim_chinese_constraints(subject)
            preserved.extend(f"逐字保留：{constraint}" for constraint in verbatim_spans)
            if detected in {"product", "commercial"}:
                preserved.append("产品造型、材质、颜色、标志、标签和包装文字")
            if identity_mode:
                preserved.extend(self.identity_invariants_zh)
                preserved.extend(
                    f"未要求修改 {variable}，因此保持不变"
                    for variable in self.editable_terms
                    if variable not in editable
                )
            changes = [
                PromptChange(
                    category="visual_specificity",
                    description="将模糊的质量词转换为可执行的构图、光线、材质、细节和层级要求。",
                ),
                PromptChange(
                    category="intent_preservation",
                    description="原样保留原文的人物、动作、物品、关系、文字、凝视和否定要求。",
                ),
            ]
        else:
            enhancement = self.enhancement_by_task[detected]
            corrected = f"{subject}; {enhancement}."
            preserved = ["subject, action, requested attributes, and core environment"]
            explicit_invariants = re.findall(
                r"[^,.;]*(?:preserve|unchanged|must|do not)[^,.;]*", original, re.IGNORECASE
            )
            verbatim_spans = [item.strip() for item in explicit_invariants if item.strip()]
            preserved.extend(f"explicit invariant: {item}" for item in verbatim_spans)
            if detected in {"product", "commercial"}:
                preserved.append("product geometry, material, color, logo, label, and packaging text")
            if identity_mode:
                preserved.extend(self.identity_invariants)
                preserved.extend(
                    f"{variable} remains unchanged because it was not requested"
                    for variable in self.editable_terms
                    if variable not in editable
                )
            changes = [
                PromptChange(
                    category="visual_specificity",
                    description=(
                        "Converted vague quality language into observable composition, lighting, "
                        "material, texture, and hierarchy controls."
                    ),
                ),
                PromptChange(
                    category="intent_preservation",
                    description="Kept the original subject and requested action as the controlling facts.",
                ),
            ]
        if identity_mode:
            changes.append(
                PromptChange(
                    category="identity_preservation",
                    description=(
                        "锁定人物身份特征和明确修改范围之外的所有变量。"
                        if is_chinese
                        else "Locked identity invariants and all variables outside the explicitly "
                        "requested edit scope."
                    ),
                )
            )
        return ImagePromptCorrectResult(
            original_prompt=original,
            corrected_prompt=corrected,
            detected_type=detected,
            identity_preservation_mode=identity_mode,
            preserved_constraints=preserved,
            verbatim_spans=verbatim_spans,
            editable_variables=editable,
            changes=changes,
            corrector_version=self.version,
        )
