"""Two-stage retrieval: explicit dependencies are forced, similarity supplements.

The regression that matters most here: an explicitly dependent shot must enter
the context even when unrelated material scores higher on similarity, and a
missing dependency must move the shot to review rather than silently degrading
to similarity-only retrieval.
"""

from __future__ import annotations

import pytest
from memory_core import (
    ContextAssembler,
    ContextBudget,
    ContextSegmentSource,
    DependencySegment,
    DependencySegmentOmitted,
    MemoryLayer,
    RetrievedMemory,
)
from narrative_ledger_core import (
    NarrativeLedgerService,
    ShotDependencyService,
    ShotDependencyUnresolved,
)
from production_domain.models import (
    DecisionRecord,
    Episode,
    GenerationCandidate,
    GenerationJob,
    PromptCompilation,
    Shot,
    ShotDependencyType,
    ShotStatus,
)
from sqlalchemy import select

SCRIPT = """INT. KITCHEN - DAY
LinJin picks up the phone.
LinJin turns toward the door.
INT. HALLWAY - NIGHT
LinJin walks toward the door.
"""


@pytest.fixture
def dependencies(container):  # type: ignore[no-untyped-def]
    return ShotDependencyService(container.database)


@pytest.fixture
def ledger(container):  # type: ignore[no-untyped-def]
    return NarrativeLedgerService(container.database)


def _compile(container, project, script: str = SCRIPT, *, episode_number: int = 1):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title=f"Episode {episode_number}",
            episode_number=episode_number,
            script_source=script,
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    return episode_id, container.narrative.compile_episode(episode_id)


def _memory(identifier: str, text: str, score: float) -> RetrievedMemory:
    return RetrievedMemory(
        id=identifier,
        project_id="project",
        layer=MemoryLayer.EPISODIC,
        memory_type="beat",
        text=text,
        image_urls=[],
        video_urls=[],
        entity_ids=[],
        scene_id=None,
        shot_id=None,
        asset_version_ids=[],
        canonical=False,
        score=score,
        score_components={},
        metadata={},
    )


def _segment(key: str, text: str) -> DependencySegment:
    return DependencySegment(
        key=key,
        source_reason=ContextSegmentSource.EXPLICIT_DEPENDENCY,
        text=text,
        dependency_type=ShotDependencyType.FORESHADOWING.value,
    )


def test_explicit_dependency_outranks_higher_similarity(  # type: ignore[no-untyped-def]
) -> None:
    """The regression: high-scoring unrelated memories cannot displace a dependency."""

    assembler = ContextAssembler(ContextBudget(max_characters=900, max_tokens=300))
    noise = [
        _memory(f"noise-{index}", "an unrelated but very similar beat " * 12, 0.99)
        for index in range(4)
    ]
    context = assembler.assemble(
        canonical_assets=[{"id": "asset", "type": "CHARACTER", "name": "LinJin"}],
        temporal_state={"scene": {"location": "hallway"}},
        shot_requirement={"action": "LinJin opens the envelope"},
        memories=noise,
        dependency_segments=[_segment("dep-1", "the envelope planted in episode 1 pays off")],
    )

    assert "EXPLICIT_DEPENDENCY[dep-1]" in context.assembled_text
    assert "the envelope planted in episode 1" in context.assembled_text
    provenance = {item["label"]: item["source_reason"] for item in context.segment_provenance}
    assert provenance["EXPLICIT_DEPENDENCY[dep-1]"] == "EXPLICIT_DEPENDENCY"
    # The budget was too small for all the noise: similarity was shed, the
    # dependency was not.
    assert any(label.startswith("EPISODIC_MEMORY") for label in context.omitted)
    assert not any(label.startswith("EXPLICIT_DEPENDENCY") for label in context.omitted)
    for memory in context.episodic_memories:
        assert provenance[f"EPISODIC_MEMORY[{memory.id}]"] == "SIMILARITY"


def test_forced_segment_that_cannot_fit_raises_instead_of_dropping() -> None:
    assembler = ContextAssembler(ContextBudget(max_characters=500, max_tokens=125))
    with pytest.raises(DependencySegmentOmitted, match="cannot fit"):
        assembler.assemble(
            canonical_assets=[{"id": "a", "padding": "x" * 600}],
            temporal_state={"padding": "y" * 400},
            shot_requirement={"action": "z" * 300},
            memories=[],
            dependency_segments=[_segment("dep-1", "owed material")],
        )


def test_prepare_autopilot_forces_dependency_and_obligation_context(  # type: ignore[no-untyped-def]
    container, project, dependencies, ledger
):
    _, result = _compile(container, project)
    first, _, third = result.shot_ids
    dependencies.declare(
        project.id,
        target_shot_id=third,
        dependency_type=ShotDependencyType.FORESHADOWING.value,
        source_shot_id=first,
        summary="the kitchen phone pays off in the hallway",
    )
    ledger.open_obligation(
        project.id,
        obligation_key="reveal_caller",
        promise="Reveal who was on the phone.",
        episode=1,
    )

    prepared = container.visual_runtime.prepare_autopilot(
        third,
        idempotency_key="dependency-context-forced",
        allowed_providers=["google_flow"],
    )

    text = prepared.context.assembled_text
    assert "EXPLICIT_DEPENDENCY[" in text
    assert "the kitchen phone pays off in the hallway" in text
    assert "OPEN_OBLIGATION[obligation-1]" in text
    assert "Reveal who was on the phone." in text
    reasons = {item["source_reason"] for item in prepared.context.segment_provenance}
    assert {"EXPLICIT_DEPENDENCY", "OPEN_OBLIGATION"}.issubset(reasons)
    assert prepared.request.metadata["context_provenance"] == (
        prepared.context.segment_provenance
    )

    # The same resolved dependency reached the compiler contract: it is a
    # continuity assertion now, not merely retrieval context.
    with container.database.session() as session:
        compilation = session.scalar(
            select(PromptCompilation)
            .where(PromptCompilation.shot_id == third)
            .order_by(PromptCompilation.created_at.desc())
        )
        assertions = compilation.diff_json["prompt_compiler_output"]["continuity_assertions"]
        assert any("the kitchen phone pays off in the hallway" in item for item in assertions)
        assert any("Reveal who was on the phone." in item for item in assertions)
        input_facts = compilation.diff_json["prompt_compiler_input"]["continuity_context"]["facts"]
        fact_reasons = {
            item.get("source_reason") for item in input_facts if isinstance(item, dict)
        }
        assert "EXPLICIT_DEPENDENCY" in fact_reasons
        assert "OPEN_OBLIGATION" in fact_reasons


def test_series_facts_reach_the_compiler_for_disclosed_holders(  # type: ignore[no-untyped-def]
    container, project, ledger
):
    _, result = _compile(container, project)
    third = result.shot_ids[2]
    ledger.establish_fact(
        project.id,
        fact_key="phone_is_bugged",
        summary="The phone is bugged.",
        episode=1,
    )

    compilation = container.prompts.compile(third)
    facts = compilation.input.continuity_context.facts
    series_entries = [
        item
        for item in facts
        if isinstance(item, dict) and item.get("source_reason") == "SERIES_FACT"
    ]
    assert any(item.get("value") == "The phone is bugged." for item in series_entries)
    assert any("The phone is bugged." in item for item in compilation.output.continuity_assertions)


def test_missing_dependency_enters_review_and_never_degrades_to_similarity(  # type: ignore[no-untyped-def]
    container, project, dependencies, ledger
):
    _, result = _compile(container, project)
    _, second, third = result.shot_ids
    ledger.open_obligation(
        project.id, obligation_key="paid_elsewhere", promise="A promise.", episode=1
    )
    dependencies.declare(
        project.id,
        target_shot_id=third,
        dependency_type=ShotDependencyType.OBLIGATION_FULFILLMENT.value,
        obligation_key="paid_elsewhere",
    )
    ledger.settle_obligation(project.id, obligation_key="paid_elsewhere", episode=1, shot_id=second)

    with pytest.raises(ShotDependencyUnresolved, match="review required"):
        container.candidates.create_candidate(
            third,
            idempotency_key="dependency-review-required",
        )

    with container.database.session() as session:
        shot = session.get(Shot, third)
        assert shot.status == ShotStatus.USER_REVIEW_REQUIRED.value
        record = session.scalar(
            select(DecisionRecord).where(
                DecisionRecord.shot_id == third,
                DecisionRecord.decision_type == "SHOT_DEPENDENCY_RESOLUTION",
            )
        )
        assert record is not None
        assert record.selected_action == "REVIEW_REQUIRED"
        assert any(
            code.startswith("DEPENDENCY_OBLIGATION_ALREADY_SETTLED")
            for code in record.reason_codes
        )
        # Refused before anything was created: no job, no candidate, nothing
        # generated from similarity-only context.
        assert session.scalar(select(GenerationJob.id).where(GenerationJob.shot_id == third)) is None
        assert (
            session.scalar(
                select(GenerationCandidate.id).where(GenerationCandidate.shot_id == third)
            )
            is None
        )
