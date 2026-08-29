"""Explicit shot dependencies: declared, validated, resolved — never guessed."""

from __future__ import annotations

import pytest
from narrative_ledger_core import (
    NarrativeLedgerService,
    ShotDependencyError,
    ShotDependencyService,
    ShotDependencyUnresolved,
)
from production_domain.models import (
    Episode,
    Project,
    ShotDependency,
    ShotDependencyOrigin,
    ShotDependencyType,
)
from sqlalchemy import select

SCRIPT = """INT. KITCHEN - DAY
LinJin picks up the phone.
LinJin turns toward the door.
INT. HALLWAY - NIGHT
LinJin walks toward the door.
"""


@pytest.fixture
def service(container):  # type: ignore[no-untyped-def]
    return ShotDependencyService(container.database)


@pytest.fixture
def ledger(container):  # type: ignore[no-untyped-def]
    return NarrativeLedgerService(container.database)


def _compile(container, project, script: str, *, episode_number: int = 1):  # type: ignore[no-untyped-def]
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


def test_script_compilation_writes_state_inheritance_dependencies(container, project, service):  # type: ignore[no-untyped-def]
    """Continuous pairs get a structural dependency; scene cuts do not."""

    _, result = _compile(container, project, SCRIPT)
    first, second, third = result.shot_ids

    second_rows = service.list_for(second)
    assert [row.dependency_type for row in second_rows] == [
        ShotDependencyType.STATE_INHERITANCE.value
    ]
    assert second_rows[0].source_shot_id == first
    assert second_rows[0].origin == ShotDependencyOrigin.SCRIPT_COMPILER.value
    assert "picks up the phone" in second_rows[0].summary

    assert service.list_for(third) == []
    assert service.list_for(first) == []


def test_declare_is_idempotent_on_the_natural_key(container, project, service):  # type: ignore[no-untyped-def]
    _, result = _compile(container, project, SCRIPT)
    first, _, third = result.shot_ids

    declared = service.declare(
        project.id,
        target_shot_id=third,
        dependency_type=ShotDependencyType.FORESHADOWING.value,
        source_shot_id=first,
        summary="the phone picked up in the kitchen pays off here",
    )
    redeclared = service.declare(
        project.id,
        target_shot_id=third,
        dependency_type=ShotDependencyType.FORESHADOWING.value,
        source_shot_id=first,
        summary="a different wording changes nothing",
    )
    assert redeclared.id == declared.id
    assert redeclared.summary == "the phone picked up in the kitchen pays off here"
    assert len(service.list_for(third)) == 1


def test_a_dependency_may_only_point_backward(container, project, service):  # type: ignore[no-untyped-def]
    _, result = _compile(container, project, SCRIPT)
    first, _, third = result.shot_ids

    with pytest.raises(ShotDependencyError, match="earlier"):
        service.declare(
            project.id,
            target_shot_id=first,
            dependency_type=ShotDependencyType.FORESHADOWING.value,
            source_shot_id=third,
        )


def test_type_specific_referents_are_required(container, project, service):  # type: ignore[no-untyped-def]
    _, result = _compile(container, project, SCRIPT)
    first, _, third = result.shot_ids

    with pytest.raises(ShotDependencyError, match="fact_key"):
        service.declare(
            project.id,
            target_shot_id=third,
            dependency_type=ShotDependencyType.FACT_REVELATION.value,
            source_shot_id=first,
        )
    with pytest.raises(ShotDependencyError, match="obligation_key"):
        service.declare(
            project.id,
            target_shot_id=third,
            dependency_type=ShotDependencyType.OBLIGATION_FULFILLMENT.value,
            source_shot_id=first,
        )
    with pytest.raises(ShotDependencyError, match="source_shot_id"):
        service.declare(
            project.id,
            target_shot_id=third,
            dependency_type=ShotDependencyType.STATE_INHERITANCE.value,
            fact_key="anything",
        )
    with pytest.raises(ShotDependencyError, match="unknown dependency type"):
        service.declare(
            project.id,
            target_shot_id=third,
            dependency_type="SIMILARITY",
            source_shot_id=first,
        )
    with pytest.raises(ShotDependencyError, match="referent"):
        service.declare(
            project.id,
            target_shot_id=third,
            dependency_type=ShotDependencyType.FORESHADOWING.value,
        )


def test_fact_and_obligation_referents_must_exist_to_declare(  # type: ignore[no-untyped-def]
    container, project, service, ledger
):
    _, result = _compile(container, project, SCRIPT)
    third = result.shot_ids[2]

    with pytest.raises(LookupError, match="not established"):
        service.declare(
            project.id,
            target_shot_id=third,
            dependency_type=ShotDependencyType.FACT_REVELATION.value,
            fact_key="phone_is_bugged",
        )
    ledger.establish_fact(
        project.id, fact_key="phone_is_bugged", summary="The phone is bugged.", episode=1
    )
    declared = service.declare(
        project.id,
        target_shot_id=third,
        dependency_type=ShotDependencyType.FACT_REVELATION.value,
        fact_key="phone_is_bugged",
    )
    assert declared.fact_key == "phone_is_bugged"


def test_cross_project_referents_are_refused(container, project, service):  # type: ignore[no-untyped-def]
    _, result = _compile(container, project, SCRIPT)
    third = result.shot_ids[2]
    with container.database.session() as session:
        other = Project(title="Another Series")
        session.add(other)
        session.flush()
        other_id = other.id
    _, other_result = _compile(
        container, type("P", (), {"id": other_id})(), SCRIPT, episode_number=1
    )

    with pytest.raises(ShotDependencyError, match="different project"):
        service.declare(
            project.id,
            target_shot_id=third,
            dependency_type=ShotDependencyType.FORESHADOWING.value,
            source_shot_id=other_result.shot_ids[0],
        )


def test_resolution_carries_payloads_with_explicit_provenance(  # type: ignore[no-untyped-def]
    container, project, service, ledger
):
    _, result = _compile(container, project, SCRIPT)
    first, _, third = result.shot_ids
    ledger.establish_fact(
        project.id, fact_key="phone_is_bugged", summary="The phone is bugged.", episode=1
    )
    ledger.open_obligation(
        project.id,
        obligation_key="who_bugged_it",
        promise="Reveal who bugged the phone.",
        episode=1,
    )
    service.declare(
        project.id,
        target_shot_id=third,
        dependency_type=ShotDependencyType.FORESHADOWING.value,
        source_shot_id=first,
        summary="the kitchen phone pays off",
        # FORESHADOWING quotes produced canon, so an uncommitted source is
        # only usable through this declared, audited override.
        metadata={"allow_uncommitted_source": True},
    )
    service.declare(
        project.id,
        target_shot_id=third,
        dependency_type=ShotDependencyType.FACT_REVELATION.value,
        fact_key="phone_is_bugged",
    )
    service.declare(
        project.id,
        target_shot_id=third,
        dependency_type=ShotDependencyType.OBLIGATION_FULFILLMENT.value,
        obligation_key="who_bugged_it",
    )

    contexts = service.resolve_for_generation(third)
    assert [item.source_reason for item in contexts] == ["EXPLICIT_DEPENDENCY"] * 3
    by_type = {item.dependency_type: item for item in contexts}
    foreshadow_source = by_type["FORESHADOWING"].payload["source_shot"]
    assert "picks up the phone" in foreshadow_source["prompt"]
    assert foreshadow_source["committed"] is False
    assert foreshadow_source["uncommitted_source_allowed_by"] == "DEPENDENCY_METADATA"
    assert by_type["FACT_REVELATION"].payload["fact"]["summary"] == "The phone is bugged."
    assert by_type["OBLIGATION_FULFILLMENT"].payload["obligation"]["status"] == "OPEN"


def test_unresolvable_dependencies_refuse_with_reason_codes(  # type: ignore[no-untyped-def]
    container, project, service, ledger
):
    """A future fact, a settled obligation — each names itself in the refusal."""

    _, result = _compile(container, project, SCRIPT)
    first, second, third = result.shot_ids
    ledger.establish_fact(
        project.id, fact_key="from_the_future", summary="Established later.", episode=2
    )
    ledger.open_obligation(
        project.id, obligation_key="paid_elsewhere", promise="A promise.", episode=1
    )
    service.declare(
        project.id,
        target_shot_id=third,
        dependency_type=ShotDependencyType.FACT_REVELATION.value,
        fact_key="from_the_future",
    )
    service.declare(
        project.id,
        target_shot_id=third,
        dependency_type=ShotDependencyType.OBLIGATION_FULFILLMENT.value,
        obligation_key="paid_elsewhere",
    )
    ledger.settle_obligation(
        project.id, obligation_key="paid_elsewhere", episode=1, shot_id=second
    )

    with pytest.raises(ShotDependencyUnresolved) as excinfo:
        service.resolve_for_generation(third)
    codes = excinfo.value.reason_codes
    assert any(code.startswith("DEPENDENCY_FACT_FROM_FUTURE") for code in codes)
    assert any(code.startswith("DEPENDENCY_OBLIGATION_ALREADY_SETTLED") for code in codes)


def test_manual_removal_withdraws_a_dependency(container, project, service):  # type: ignore[no-untyped-def]
    _, result = _compile(container, project, SCRIPT)
    first, _, third = result.shot_ids
    declared = service.declare(
        project.id,
        target_shot_id=third,
        dependency_type=ShotDependencyType.FORESHADOWING.value,
        source_shot_id=first,
    )
    service.remove(project.id, dependency_id=declared.id)
    assert service.list_for(third) == []
    with pytest.raises(LookupError):
        service.remove(project.id, dependency_id=declared.id)


def test_recompile_refuses_while_later_shots_depend_on_this_episode(  # type: ignore[no-untyped-def]
    container, project, service
):
    """An explicit dependency is a contract; deleting its source is loud."""

    _, first_result = _compile(container, project, SCRIPT, episode_number=1)
    _, second_result = _compile(container, project, SCRIPT, episode_number=2)
    declared = service.declare(
        project.id,
        target_shot_id=second_result.shot_ids[0],
        dependency_type=ShotDependencyType.FORESHADOWING.value,
        source_shot_id=first_result.shot_ids[0],
    )

    with container.database.session() as session:
        episode = session.scalar(
            select(Episode).where(
                Episode.project_id == project.id, Episode.episode_number == 1
            )
        )
        episode.script_source = SCRIPT + "\nLinJin stops."
        first_episode_id = episode.id

    with pytest.raises(RuntimeError, match="explicit"):
        container.narrative.compile_episode(first_episode_id)

    service.remove(project.id, dependency_id=declared.id)
    recompiled = container.narrative.compile_episode(first_episode_id)
    assert len(recompiled.shot_ids) == 4
    with container.database.session() as session:
        stale = session.scalar(
            select(ShotDependency).where(ShotDependency.id == declared.id)
        )
        assert stale is None
