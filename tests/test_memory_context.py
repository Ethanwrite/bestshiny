from __future__ import annotations

import pytest
from memory_core import (
    AuthorityLevel,
    ContextAssembler,
    ContextBudget,
    EvidencePurpose,
    MemoryLayer,
    MemoryQuery,
    MultimodalContent,
    ShotMemoryInput,
)
from production_domain.models import ShotMemory


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


@pytest.mark.parametrize(
    "purpose",
    [
        EvidencePurpose.IDENTITY_VERDICT,
        EvidencePurpose.STATE_FACT_ASSERTION,
        EvidencePurpose.STATE_DELTA_APPROVAL,
        EvidencePurpose.COMMIT_AUTHORIZATION,
    ],
)
def test_embedding_content_rejects_decision_authority_purposes(purpose: EvidencePurpose):
    with pytest.raises(ValueError, match="embedding evidence is advisory"):
        MultimodalContent(text="a similarity hint", evidence_purpose=purpose)


def test_embedding_content_rejects_authoritative_label():
    with pytest.raises(ValueError, match="cannot be authoritative"):
        MultimodalContent(
            text="a similarity hint",
            authority_level=AuthorityLevel.AUTHORITATIVE,
        )


def test_current_state_is_compatibility_alias_for_advisory_retrieval_hint(container, project):
    indexed = container.memory.index(
        ShotMemoryInput(
            project_id=project.id,
            layer=MemoryLayer.TEMPORAL,
            memory_type="SHOT_STATE_HINT",
            content=MultimodalContent(
                text="Mira is at platform three with an unlit flare",
                evidence_purpose=EvidencePurpose.SUPPORTING_SIMILARITY,
            ),
            entity_ids=["mira"],
            metadata={
                "authority_level": AuthorityLevel.AUTHORITATIVE.value,
                "evidence_purpose": EvidencePurpose.COMMIT_AUTHORIZATION.value,
            },
        )
    )

    hint = container.memory.retrieval_hint(project.id)
    compatibility = container.memory.current_state(project.id)

    assert hint is not None and compatibility is not None
    assert hint.id == indexed.id == compatibility.id
    assert hint.authority_level is AuthorityLevel.ADVISORY
    assert hint.evidence_purpose is EvidencePurpose.SUPPORTING_SIMILARITY
    assert compatibility.authority_level is AuthorityLevel.ADVISORY
    assert hint.metadata["authority_level"] == AuthorityLevel.ADVISORY.value
    assert hint.metadata["evidence_purpose"] == EvidencePurpose.SUPPORTING_SIMILARITY.value


def test_search_rejects_persisted_embedding_that_claims_authority(container, project):
    with container.database.session() as session:
        session.add(
            ShotMemory(
                project_id=project.id,
                layer=MemoryLayer.TEMPORAL.value,
                memory_type="UNTRUSTED_STATE_CLAIM",
                text_content="Mira's flare is lit",
                image_urls=[],
                video_urls=[],
                entity_ids=["mira"],
                asset_version_ids=[],
                canonical=False,
                embedding=[0.0] * 512,
                embedding_dimension=512,
                embedding_provider="local_test",
                embedding_model="deterministic-token-hash-v1",
                metadata_json={
                    "evidence_purpose": EvidencePurpose.RETRIEVAL_HINT.value,
                    "authority_level": AuthorityLevel.AUTHORITATIVE.value,
                },
            )
        )

    results = container.memory.search(
        MemoryQuery(project_id=project.id, text="Mira flare", layers=[MemoryLayer.TEMPORAL])
    )

    assert results == []
