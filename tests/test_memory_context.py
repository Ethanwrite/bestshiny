from __future__ import annotations

from memory_core import (
    ContextAssembler,
    ContextBudget,
    MemoryLayer,
    MemoryQuery,
    MultimodalContent,
    ShotMemoryInput,
)


def test_memory_filters_entities_before_vector_ranking(container, project):
    lin = container.memory.index(
        ShotMemoryInput(
            project_id=project.id,
            layer=MemoryLayer.CANONICAL,
            memory_type="CHARACTER_ASSET",
            content=MultimodalContent(text="Lin Jin short black hair blue jacket profile"),
            entity_ids=["lin"],
        )
    )
    container.memory.index(
        ShotMemoryInput(
            project_id=project.id,
            layer=MemoryLayer.EPISODIC,
            memory_type="CHARACTER_HISTORY",
            content=MultimodalContent(text="Lin Jin short black hair blue jacket profile"),
            entity_ids=["zhao"],
        )
    )

    results = container.memory.search(
        MemoryQuery(
            project_id=project.id,
            text="black hair blue jacket profile",
            entity_ids=["lin"],
            top_k=5,
        )
    )

    assert [item.id for item in results] == [lin.id]
    assert results[0].score_components["canonical_priority"] == 1.0


def test_context_priority_keeps_canonical_truth_ahead_of_episodic_memory(container, project):
    episodic = container.memory.index(
        ShotMemoryInput(
            project_id=project.id,
            layer=MemoryLayer.EPISODIC,
            memory_type="SHOT_HISTORY",
            content=MultimodalContent(text="old generated shot " * 100),
            entity_ids=["lin"],
        )
    )
    retrieved = container.memory.search(
        MemoryQuery(project_id=project.id, text="old generated shot", entity_ids=["lin"])
    )
    context = ContextAssembler(
        ContextBudget(max_characters=500, max_tokens=125, max_images=2, max_videos=1)
    ).assemble(
        canonical_assets=[
            {
                "id": "lin",
                "version_id": "lin-v2",
                "canonical_metadata": {"hair": "short black", "wardrobe": "blue"},
                "image_urls": ["canonical-front", "canonical-profile"],
            }
        ],
        temporal_state={"position": "screen-left", "wardrobe": "blue"},
        shot_requirement={"action": "turn once"},
        memories=retrieved,
        previous_final_frame_asset_id="previous-frame",
    )

    assert context.assembled_text.startswith("CANONICAL_ASSETS")
    assert context.canonical_asset_ids == ["lin-v2"]
    assert context.reference_images[0] == "previous-frame"
    assert "canonical-front" in context.reference_images
    assert episodic.id not in [item.id for item in context.episodic_memories]
    assert any(value.startswith("EPISODIC_MEMORY") for value in context.omitted)


def test_feature_flag_project_override_does_not_change_global_default(container, project):
    assert container.feature_flags.enabled("voyage_memory") is False
    container.feature_flags.set("voyage_memory", True, project_id=project.id)
    assert container.feature_flags.enabled("voyage_memory", project_id=project.id) is True
    assert container.feature_flags.enabled("voyage_memory") is False


def test_context_budget_applies_to_mandatory_sections_too():
    budget = ContextBudget(max_characters=500, max_tokens=100, max_images=1, max_videos=0)
    context = ContextAssembler(budget).assemble(
        canonical_assets=[{"identity": "canonical " * 200}],
        temporal_state={"state": "temporal " * 100},
        shot_requirement={"action": "current shot " * 100},
        memories=[],
    )
    assert context.budget_used["characters"] <= 400
    assert context.budget_used["tokens_estimate"] <= 100
    assert "CANONICAL_ASSETS" in context.assembled_text
    assert "CURRENT_TEMPORAL_STATE" in context.assembled_text
    assert "CURRENT_SHOT_REQUIREMENT" in context.assembled_text
