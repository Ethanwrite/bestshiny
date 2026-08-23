from __future__ import annotations

from datetime import timedelta

import pytest
from memory_core import (
    AuthorityLevel,
    ContextAssembler,
    ContextBudget,
    EpisodeScope,
    EvidencePurpose,
    MemoryLayer,
    MemoryQuery,
    MultimodalContent,
    ShotMemoryInput,
)
from production_domain.models import Episode, Scene, ShotMemory


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


# --- Episode-scoped retrieval -----------------------------------------------
#
# A 60-episode series is the case these guard. Episodic recall used to be
# narrowed to the current *scene*, so the layer whose whole purpose is "what
# happened before" could not see anything before the shot being planned.


def _episode_with_scene(container, project_id: str, number: int) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(project_id=project_id, title=f"Episode {number}", episode_number=number)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description=f"scene of episode {number}")
        session.add(scene)
        session.flush()
        return episode.id, scene.id


def test_episodic_recall_is_no_longer_fenced_off_by_the_current_scene(container, project):
    """L2 exists to recall earlier work; a scene fence made that impossible."""

    _episode_one, scene_one = _episode_with_scene(container, project.id, 1)
    _episode_two, scene_two = _episode_with_scene(container, project.id, 2)
    earlier = container.memory.index(
        ShotMemoryInput(
            project_id=project.id,
            layer=MemoryLayer.EPISODIC,
            memory_type="SHOT_HISTORY",
            content=MultimodalContent(text="Lin Jin promised to return the letter"),
            entity_ids=["lin"],
            scene_id=scene_one,
        )
    )

    results = container.memory.search(
        MemoryQuery(
            project_id=project.id,
            text="Lin Jin promised to return the letter",
            entity_ids=["lin"],
            scene_id=scene_two,
            top_k=5,
        )
    )

    assert earlier.id in [item.id for item in results]


def test_temporal_state_is_still_fenced_to_the_current_scene(container, project):
    """L1 is current state. Inheriting another scene's would be wrong."""

    _episode_one, scene_one = _episode_with_scene(container, project.id, 3)
    _episode_two, scene_two = _episode_with_scene(container, project.id, 4)
    container.memory.index(
        ShotMemoryInput(
            project_id=project.id,
            layer=MemoryLayer.TEMPORAL,
            memory_type="SCENE_STATE",
            content=MultimodalContent(text="Lin Jin is holding the letter"),
            entity_ids=["lin"],
            scene_id=scene_one,
        )
    )

    results = container.memory.search(
        MemoryQuery(
            project_id=project.id,
            text="Lin Jin is holding the letter",
            entity_ids=["lin"],
            scene_id=scene_two,
            top_k=5,
        )
    )

    assert results == []


def test_episode_scope_confines_retrieval_to_the_current_episode(container, project):
    episode_one, scene_one = _episode_with_scene(container, project.id, 5)
    episode_two, scene_two = _episode_with_scene(container, project.id, 6)
    for scene_id in (scene_one, scene_two):
        container.memory.index(
            ShotMemoryInput(
                project_id=project.id,
                layer=MemoryLayer.EPISODIC,
                memory_type="SHOT_HISTORY",
                content=MultimodalContent(text="the lantern-lit alley after rain"),
                entity_ids=["lin"],
                scene_id=scene_id,
            )
        )

    scoped = container.memory.search(
        MemoryQuery(
            project_id=project.id,
            text="the lantern-lit alley after rain",
            entity_ids=["lin"],
            episode_id=episode_two,
            episode_scope=EpisodeScope.EPISODE,
            top_k=10,
        )
    )

    assert scoped, "episode scope must not empty the result set"
    assert {item.episode_id for item in scoped} == {episode_two}
    assert episode_one not in {item.episode_id for item in scoped}


def test_series_scope_reaches_earlier_episodes_but_ranks_the_current_one_first(container, project):
    episode_one, scene_one = _episode_with_scene(container, project.id, 7)
    episode_two, scene_two = _episode_with_scene(container, project.id, 8)
    for scene_id in (scene_one, scene_two):
        container.memory.index(
            ShotMemoryInput(
                project_id=project.id,
                layer=MemoryLayer.EPISODIC,
                memory_type="SHOT_HISTORY",
                content=MultimodalContent(text="the lantern-lit alley after rain"),
                entity_ids=["lin"],
                scene_id=scene_id,
            )
        )

    series = container.memory.search(
        MemoryQuery(
            project_id=project.id,
            text="the lantern-lit alley after rain",
            entity_ids=["lin"],
            episode_id=episode_two,
            episode_scope=EpisodeScope.SERIES,
            top_k=10,
        )
    )

    episodes = [item.episode_id for item in series]
    assert set(episodes) == {episode_one, episode_two}
    # Identical text, so only the episode signal can separate them.
    assert episodes[0] == episode_two
    assert series[0].score_components["episode_match"] == 1.0
    assert series[-1].score_components["episode_match"] == 0.0


def test_series_scope_without_an_episode_is_rejected_rather_than_silently_unscoped():
    with pytest.raises(ValueError, match="episode_scope=SERIES requires episode_id"):
        MemoryQuery(project_id="project", episode_scope=EpisodeScope.SERIES)


def test_recency_half_life_is_configurable_per_query(container, project):
    """A series that ran for a year is poorly served by a fixed 30-day decay."""

    memory = container.memory.index(
        ShotMemoryInput(
            project_id=project.id,
            layer=MemoryLayer.EPISODIC,
            memory_type="SHOT_HISTORY",
            content=MultimodalContent(text="the lantern-lit alley after rain"),
            entity_ids=["lin"],
        )
    )
    with container.database.session() as session:
        row = session.get(ShotMemory, memory.id)
        row.created_at = row.created_at - timedelta(days=180)

    def recency(half_life: float) -> float:
        results = container.memory.search(
            MemoryQuery(
                project_id=project.id,
                text="the lantern-lit alley after rain",
                entity_ids=["lin"],
                recency_half_life_days=half_life,
                top_k=5,
            )
        )
        return results[0].score_components["temporal_relevance"]

    assert recency(365.0) > recency(30.0)
