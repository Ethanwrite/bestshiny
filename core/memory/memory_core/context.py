from __future__ import annotations

import json
from typing import Any

from .schemas import ContextBudget, GenerationContext, RetrievedMemory


class ContextAssembler:
    """Build bounded context in immutable priority order: L0, L1, shot, L2, world."""

    version = "context-assembler-v1"

    def __init__(self, budget: ContextBudget | None = None):
        self.budget = budget or ContextBudget()

    def assemble(
        self,
        *,
        canonical_assets: list[dict[str, Any]],
        temporal_state: dict[str, Any],
        shot_requirement: dict[str, Any],
        memories: list[RetrievedMemory],
        world_rules: list[str] | None = None,
        previous_final_frame_asset_id: str | None = None,
    ) -> GenerationContext:
        world_rules = world_rules or []
        omitted: list[str] = []
        sections: list[str] = []
        character_count = 0
        effective_character_limit = min(
            self.budget.max_characters,
            self.budget.max_tokens * 4,
        )

        def add(
            label: str,
            value: Any,
            *,
            mandatory: bool = False,
            reserve_labels: tuple[str, ...] = (),
        ) -> bool:
            nonlocal character_count
            rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            prefix = f"{label}: "
            piece = f"{prefix}{rendered}"
            remaining = max(0, effective_character_limit - character_count)
            if not mandatory and len(piece) > remaining:
                omitted.append(label)
                return False
            if mandatory and len(piece) > remaining:
                reserved = sum(len(item) + 2 + 32 for item in reserve_labels)
                usable = max(0, remaining - reserved)
                if usable <= len(prefix):
                    omitted.append(label)
                    return False
                piece = f"{prefix}{rendered[: usable - len(prefix)]}"
            sections.append(piece)
            character_count += len(piece)
            return True

        # Canonical truth is always included before any historical similarity.
        add(
            "CANONICAL_ASSETS",
            canonical_assets,
            mandatory=True,
            reserve_labels=("CURRENT_TEMPORAL_STATE", "CURRENT_SHOT_REQUIREMENT"),
        )
        add(
            "CURRENT_TEMPORAL_STATE",
            temporal_state,
            mandatory=True,
            reserve_labels=("CURRENT_SHOT_REQUIREMENT",),
        )
        add("CURRENT_SHOT_REQUIREMENT", shot_requirement, mandatory=True)

        accepted_memories: list[RetrievedMemory] = []
        for memory in sorted(memories, key=lambda item: (-item.score, item.id)):
            if add(f"EPISODIC_MEMORY[{memory.id}]", memory.text):
                accepted_memories.append(memory)
        add("WORLD_RULES", world_rules)

        canonical_ids: list[str] = []
        images: list[str] = []
        videos: list[str] = []
        for asset in canonical_assets:
            asset_id = asset.get("version_id") or asset.get("id")
            if asset_id:
                canonical_ids.append(str(asset_id))
            images.extend(str(item) for item in asset.get("image_urls", []) if item)
            videos.extend(str(item) for item in asset.get("video_urls", []) if item)
        if previous_final_frame_asset_id:
            images.insert(0, previous_final_frame_asset_id)
        for memory in accepted_memories:
            images.extend(memory.image_urls)
            videos.extend(memory.video_urls)
        images = list(dict.fromkeys(images))[: self.budget.max_images]
        videos = list(dict.fromkeys(videos))[: self.budget.max_videos]

        return GenerationContext(
            canonical_assets=canonical_assets,
            temporal_state=temporal_state,
            shot_requirement=shot_requirement,
            episodic_memories=accepted_memories,
            world_rules=world_rules,
            canonical_asset_ids=list(dict.fromkeys(canonical_ids)),
            reference_images=images,
            reference_videos=videos,
            previous_final_frame_asset_id=previous_final_frame_asset_id,
            assembled_text="\n".join(sections),
            omitted=omitted,
            budget_used={
                "characters": character_count,
                "tokens_estimate": (character_count + 3) // 4,
                "images": len(images),
                "videos": len(videos),
            },
        )
