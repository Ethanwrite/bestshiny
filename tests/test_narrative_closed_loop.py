"""The narrative ledger production closed loop.

Complete narrative positions (episode, scene sequence, shot sequence), the
commit-time ledger writes, idempotent replays, the narrative context fence,
and the same-episode future-information leak these exist to prevent.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
from narrative_ledger_core import (
    LedgerWriteConflict,
    NarrativeLedgerService,
    SettlementConflict,
    ShotDependencyService,
    ShotDependencyUnresolved,
    ShotNarrativeEffectService,
)
from production_domain.models import (
    Episode,
    JobStatus,
    NarrativeDisclosure,
    NarrativeFact,
    NarrativeObligation,
    Shot,
    ShotDependencyType,
    ShotNarrativeEffect,
    utcnow,
)
from sqlalchemy import select

DIRECTIVE_SCRIPT = """INT. KITCHEN - DAY
[ESTABLISH letter_forged: The letter is a forgery]
LinJin picks up the phone
[FORESHADOW mantel_gun: The gun on the mantel must fire]
LinJin turns toward the door
INT. HALLWAY - NIGHT
[DISCLOSE letter_forged -> ZhaoKai]
[PAYOFF mantel_gun]
ZhaoKai walks toward the door
"""


@pytest.fixture
def ledger(container):  # type: ignore[no-untyped-def]
    return NarrativeLedgerService(container.database)


@pytest.fixture
def effects(container):  # type: ignore[no-untyped-def]
    return ShotNarrativeEffectService(container.database)


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


# --------------------------------------------------------------------- slices


def test_same_episode_later_disclosure_does_not_leak_backward(ledger, project) -> None:  # type: ignore[no-untyped-def]
    """A fact disclosed in scene 2 shot 2 is invisible to scene 1 shot 1."""

    ledger.establish_fact(
        project.id,
        fact_key="mole_identified",
        summary="The mole is the archivist.",
        episode=1,
        scene_sequence=2,
        shot_sequence=2,
    )
    early = ledger.series_context(project.id, episode=1, scene_sequence=1, shot_sequence=1)
    assert early.known_facts == {}
    at_disclosure = ledger.series_context(
        project.id, episode=1, scene_sequence=2, shot_sequence=2
    )
    assert at_disclosure.known_facts["AUDIENCE"] == ["The mole is the archivist."]
    # Episode-granular calls keep the historical whole-episode reading.
    assert ledger.series_context(project.id, episode=1).known_facts["AUDIENCE"] == [
        "The mole is the archivist."
    ]
    assert not ledger.may_know(
        project.id,
        holder_key="AUDIENCE",
        fact_key="mole_identified",
        episode=1,
        scene_sequence=1,
        shot_sequence=1,
    )
    assert ledger.may_know(
        project.id,
        holder_key="AUDIENCE",
        fact_key="mole_identified",
        episode=1,
        scene_sequence=2,
        shot_sequence=2,
    )


def test_settlement_position_keeps_history_readable(ledger, project) -> None:  # type: ignore[no-untyped-def]
    """Regenerating a historical shot reads the ledger as it stood then."""

    ledger.open_obligation(
        project.id,
        obligation_key="who_sent_the_letter",
        promise="Reveal the sender.",
        episode=1,
        scene_sequence=1,
        shot_sequence=1,
    )
    ledger.settle_obligation(
        project.id,
        obligation_key="who_sent_the_letter",
        episode=2,
        scene_sequence=3,
        shot_sequence=4,
        reason="the sender confesses",
    )
    # The historical slice, between opening and settlement: still open.
    historical = ledger.series_context(project.id, episode=1, scene_sequence=5, shot_sequence=5)
    assert historical.open_obligations == ["Reveal the sender."]
    # Just before the settling shot in episode 2: still open.
    before_settle = ledger.series_context(
        project.id, episode=2, scene_sequence=3, shot_sequence=3
    )
    assert before_settle.open_obligations == ["Reveal the sender."]
    # At the settling shot and after: settled.
    assert (
        ledger.series_context(project.id, episode=2, scene_sequence=3, shot_sequence=4)
    ).open_obligations == []
    assert ledger.series_context(project.id, episode=2).open_obligations == []


def test_open_obligation_from_a_later_shot_is_invisible_earlier(ledger, project) -> None:  # type: ignore[no-untyped-def]
    ledger.open_obligation(
        project.id,
        obligation_key="late_promise",
        promise="A promise made late in the episode.",
        episode=1,
        scene_sequence=4,
        shot_sequence=2,
    )
    assert (
        ledger.series_context(project.id, episode=1, scene_sequence=1, shot_sequence=1)
    ).open_obligations == []
    assert (
        ledger.series_context(project.id, episode=1, scene_sequence=4, shot_sequence=2)
    ).open_obligations == ["A promise made late in the episode."]


# ------------------------------------------------------------------ idempotency


def test_establish_fact_replay_is_idempotent_and_conflict_raises(ledger, project) -> None:  # type: ignore[no-untyped-def]
    first = ledger.establish_fact(
        project.id, fact_key="k", summary="It happened.", episode=1, scene_sequence=1, shot_sequence=1
    )
    replay = ledger.establish_fact(
        project.id, fact_key="k", summary="It happened.", episode=1, scene_sequence=1, shot_sequence=1
    )
    assert replay == first
    with pytest.raises(LedgerWriteConflict, match="different content or position"):
        ledger.establish_fact(
            project.id,
            fact_key="k",
            summary="A different claim.",
            episode=1,
            scene_sequence=1,
            shot_sequence=1,
        )
    with pytest.raises(LedgerWriteConflict):
        ledger.establish_fact(
            project.id, fact_key="k", summary="It happened.", episode=2
        )


def test_settle_replay_is_noop_and_elsewhere_conflicts(ledger, project) -> None:  # type: ignore[no-untyped-def]
    ledger.open_obligation(
        project.id, obligation_key="o", promise="P.", episode=1, scene_sequence=1, shot_sequence=1
    )
    ledger.settle_obligation(
        project.id, obligation_key="o", episode=1, scene_sequence=2, shot_sequence=1
    )
    # The identical settlement replays as a no-op.
    ledger.settle_obligation(
        project.id, obligation_key="o", episode=1, scene_sequence=2, shot_sequence=1
    )
    # A settlement from anywhere else is a conflict, never a silent success.
    with pytest.raises(SettlementConflict, match="already SETTLED"):
        ledger.settle_obligation(
            project.id, obligation_key="o", episode=1, scene_sequence=3, shot_sequence=1
        )


def test_settle_before_open_position_is_refused(ledger, project) -> None:  # type: ignore[no-untyped-def]
    ledger.open_obligation(
        project.id, obligation_key="o2", promise="P.", episode=1, scene_sequence=3, shot_sequence=1
    )
    with pytest.raises(LedgerWriteConflict, match="before it was opened"):
        ledger.settle_obligation(
            project.id, obligation_key="o2", episode=1, scene_sequence=2, shot_sequence=9
        )


def test_disclosure_moves_earlier_with_an_audit_trail(ledger, container, project) -> None:  # type: ignore[no-untyped-def]
    ledger.establish_fact(
        project.id, fact_key="f", summary="S.", episode=1, scene_sequence=1, shot_sequence=1
    )
    ledger.disclose(
        project.id,
        fact_key="f",
        holder_key="mira",
        episode=1,
        scene_sequence=3,
        shot_sequence=2,
    )
    # A later disclosure of what they already know is a no-op.
    ledger.disclose(
        project.id, fact_key="f", holder_key="mira", episode=1, scene_sequence=4, shot_sequence=1
    )
    # An earlier one moves the record to the earliest position, audited.
    ledger.disclose(
        project.id, fact_key="f", holder_key="mira", episode=1, scene_sequence=2, shot_sequence=1
    )
    with container.database.session() as session:
        row = session.scalar(
            select(NarrativeDisclosure).where(NarrativeDisclosure.holder_key == "mira")
        )
        assert (row.disclosed_episode, row.disclosed_scene_sequence, row.disclosed_shot_sequence) == (
            1,
            2,
            1,
        )
        moves = row.metadata_json["position_moves"]
        assert moves[0]["from"] == {"episode": 1, "scene_sequence": 3, "shot_sequence": 2}


@pytest.mark.postgres_only
def test_concurrent_settlements_produce_one_winner_and_one_conflict(  # type: ignore[no-untyped-def]
    ledger, project
) -> None:
    """Two commits racing to settle one obligation: exactly one wins."""

    ledger.open_obligation(
        project.id, obligation_key="race", promise="P.", episode=1, scene_sequence=1, shot_sequence=1
    )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def settle(scene: int) -> None:
        barrier.wait()
        try:
            ledger.settle_obligation(
                project.id, obligation_key="race", episode=1, scene_sequence=scene, shot_sequence=1
            )
            result = "SETTLED"
        except SettlementConflict:
            result = "CONFLICT"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=settle, args=(scene,)) for scene in (2, 3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["CONFLICT", "SETTLED"]


# ------------------------------------------------------------------- compiler


def test_compiler_directives_declare_effects_and_dependencies(container, project) -> None:  # type: ignore[no-untyped-def]
    _, result = _compile(container, project, DIRECTIVE_SCRIPT)
    shot1, shot2, shot3 = result.shot_ids
    with container.database.session() as session:
        by_shot: dict[str, list[ShotNarrativeEffect]] = {}
        for row in session.scalars(select(ShotNarrativeEffect)):
            by_shot.setdefault(row.shot_id, []).append(row)
        shot1_effects = {row.effect_type: row for row in by_shot[shot1]}
        assert shot1_effects["ESTABLISH_FACT"].fact_key == "letter_forged"
        assert shot1_effects["ESTABLISH_FACT"].disclose_to == ["AUDIENCE"]
        assert (
            shot1_effects["ESTABLISH_FACT"].episode_number,
            shot1_effects["ESTABLISH_FACT"].scene_sequence,
            shot1_effects["ESTABLISH_FACT"].shot_sequence,
        ) == (1, 1, 1)
        shot2_effects = {row.effect_type: row for row in by_shot[shot2]}
        assert shot2_effects["OPEN_OBLIGATION"].obligation_key == "mantel_gun"
        assert shot2_effects["OPEN_OBLIGATION"].metadata_json["category"] == "FORESHADOWING"
        shot3_effects = {row.effect_type: row for row in by_shot[shot3]}
        assert shot3_effects["DISCLOSE_FACT"].fact_key == "letter_forged"
        assert shot3_effects["SETTLE_OBLIGATION"].obligation_key == "mantel_gun"
        # Nothing reached the ledger yet: effects apply at commit, not compile.
        assert session.scalar(select(NarrativeFact)) is None
        assert session.scalar(select(NarrativeObligation)) is None

    dependencies = ShotDependencyService(container.database)
    shot3_deps = {row.dependency_type: row for row in dependencies.list_for(shot3)}
    assert shot3_deps[ShotDependencyType.FACT_REVELATION.value].fact_key == "letter_forged"
    assert (
        shot3_deps[ShotDependencyType.OBLIGATION_FULFILLMENT.value].obligation_key == "mantel_gun"
    )
    assert shot3_deps[ShotDependencyType.FORESHADOWING.value].source_shot_id == shot2


def test_compiler_rejects_duplicate_establish_and_unknown_payoff(container, project) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="established twice"):
        _compile(
            container,
            project,
            """INT. A - DAY
[ESTABLISH k: One]
LinJin turns toward the door
[ESTABLISH k: Two]
LinJin walks toward the door
""",
        )
    with pytest.raises(ValueError, match="never opened at an earlier position"):
        _compile(
            container,
            project,
            """INT. A - DAY
[PAYOFF never_opened]
LinJin turns toward the door
""",
            episode_number=2,
        )


def test_directive_lines_never_become_shots(container, project) -> None:  # type: ignore[no-untyped-def]
    _, result = _compile(container, project, DIRECTIVE_SCRIPT)
    assert len(result.shot_ids) == 3
    with container.database.session() as session:
        for shot_id in result.shot_ids:
            shot = session.get(Shot, shot_id)
            assert "[" not in shot.prompt


def test_generation_refuses_fact_revelation_before_the_source_commits(  # type: ignore[no-untyped-def]
    container, project
) -> None:
    """The fact is declared by shot 1 but not canon until shot 1 commits."""

    _, result = _compile(container, project, DIRECTIVE_SCRIPT)
    shot3 = result.shot_ids[2]
    with pytest.raises(ShotDependencyUnresolved) as excinfo:
        container.shot_dependencies.resolve_for_generation(shot3)
    assert any(
        code.startswith("DEPENDENCY_FACT_NOT_CANON:letter_forged")
        or code.startswith("DEPENDENCY_OBLIGATION_NOT_CANON:mantel_gun")
        for code in excinfo.value.reason_codes
    )


# ------------------------------------------------------- commit closed loop


def _passing_qa_evidence() -> dict[str, Any]:
    return {
        "identity_samples": [{"face_similarity": 0.92}] * 6,
        "character_score": 0.92,
        "scene_score": 0.92,
        "composition_score": 0.92,
        "action_score": 0.92,
        "camera_score": 0.92,
        "lighting_score": 0.92,
        "narrative_score": 0.92,
    }


async def _commit_shot_e2e(  # type: ignore[no-untyped-def]
    container,
    shot_id: str,
    *,
    idempotency_key: str,
):
    """Generate, complete, validate and return a committable candidate."""

    candidate, _ = container.candidates.create_candidate(
        shot_id,
        idempotency_key=idempotency_key,
        enforce_entitlements=False,
    )
    submitted = await container.gateway.process(candidate.generation_job_id)
    assert submitted.status == JobStatus.SUBMITTED.value
    with container.database.session() as session:
        from production_domain.models import GenerationJob

        job = session.get(GenerationJob, submitted.id)
        job.next_retry_at = utcnow()
    completed = await container.gateway.process(submitted.id)
    assert completed.status == JobStatus.COMPLETED.value
    validated = container.candidates.sync_candidate(candidate.id, _passing_qa_evidence())
    assert validated.qa_result_id is not None
    return candidate


@pytest.fixture
def offline_video_provider(container, project, account_worker, stage_stub_output, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """The deterministic provider harness the three-shot acceptance test uses."""

    from test_director_algorithm_core import _CompletedVideoFixtureProvider

    account_id, _ = account_worker
    container.flow_affinity.bind_existing(
        local_project_id=project.id,
        provider_account_id=account_id,
        provider_project_id="flow-project-closed-loop",
    )
    fixture_provider = _CompletedVideoFixtureProvider()
    monkeypatch.setitem(container.providers._providers, "google_flow", fixture_provider)
    container.providers.register_model("google_flow", "flow-veo-3.1", "video")
    fixture_video = Path(tmp_path) / "closed-loop-output.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=160x240:d=1",
            "-pix_fmt",
            "yuv420p",
            str(fixture_video),
        ],
        check=True,
        capture_output=True,
    )

    async def download_fixture_output(url: str, **kwargs):  # type: ignore[no-untyped-def]
        del url
        return stage_stub_output(container, kwargs["key_prefix"], fixture_video.read_bytes())

    monkeypatch.setattr(
        container.media, "download_provider_output_to_staging", download_fixture_output
    )
    return fixture_provider


@pytest.mark.asyncio
async def test_commit_applies_effects_at_full_position_and_replays_idempotently(  # type: ignore[no-untyped-def]
    container, project, offline_video_provider
):
    _, result = _compile(
        container,
        project,
        """INT. KITCHEN - DAY
[ESTABLISH letter_forged: The letter is a forgery]
[FORESHADOW mantel_gun: The gun on the mantel must fire]
LinJin picks up the phone
""",
    )
    shot1 = result.shot_ids[0]
    candidate = await _commit_shot_e2e(container, shot1, idempotency_key="closed-loop-1")
    committed = container.candidates.commit(candidate.id)
    assert committed.status == "COMMITTED"

    with container.database.session() as session:
        fact = session.scalar(select(NarrativeFact).where(NarrativeFact.fact_key == "letter_forged"))
        assert fact is not None
        assert (
            fact.established_episode,
            fact.established_scene_sequence,
            fact.established_shot_sequence,
        ) == (1, 1, 1)
        assert fact.established_shot_id == shot1
        obligation = session.scalar(
            select(NarrativeObligation).where(NarrativeObligation.obligation_key == "mantel_gun")
        )
        assert obligation is not None
        assert obligation.status == "OPEN"
        assert (
            obligation.opened_episode,
            obligation.opened_scene_sequence,
            obligation.opened_shot_sequence,
        ) == (1, 1, 1)
        effect_rows = list(session.scalars(select(ShotNarrativeEffect)))
        assert all(row.applied_at is not None for row in effect_rows)
        assert all(row.applied_candidate_id == candidate.id for row in effect_rows)
        disclosure_count = session.scalars(select(NarrativeDisclosure)).all()

    # A duplicate confirmation replays: same candidate back, no new ledger rows.
    replayed = container.candidates.commit(candidate.id)
    assert replayed.id == candidate.id
    with container.database.session() as session:
        assert len(session.scalars(select(NarrativeFact)).all()) == 1
        assert len(session.scalars(select(NarrativeObligation)).all()) == 1
        assert len(session.scalars(select(NarrativeDisclosure)).all()) == len(disclosure_count)


@pytest.mark.asyncio
async def test_second_settlement_of_one_obligation_refuses_commit(  # type: ignore[no-untyped-def]
    container, project, effects, offline_video_provider
):
    """Shot 2 pays the obligation off; a rival settlement cannot also commit."""

    _, result = _compile(
        container,
        project,
        """INT. KITCHEN - DAY
[FORESHADOW mantel_gun: The gun on the mantel must fire]
LinJin picks up the phone
INT. HALLWAY - NIGHT
[PAYOFF mantel_gun]
LinJin walks toward the door
INT. OFFICE - DAY
LinJin turns toward the door
""",
    )
    shot1, shot2, shot3 = result.shot_ids
    candidate1 = await _commit_shot_e2e(container, shot1, idempotency_key="settle-race-1")
    container.candidates.commit(candidate1.id)
    # A rival settlement declared manually on shot 3.
    effects.declare(
        project.id,
        shot_id=shot3,
        effect_type="SETTLE_OBLIGATION",
        obligation_key="mantel_gun",
        summary="a second, conflicting payoff",
    )
    candidate2 = await _commit_shot_e2e(container, shot2, idempotency_key="settle-race-2")
    container.candidates.commit(candidate2.id)
    with container.database.session() as session:
        obligation = session.scalar(
            select(NarrativeObligation).where(NarrativeObligation.obligation_key == "mantel_gun")
        )
        assert obligation.status == "SETTLED"
        assert obligation.settled_shot_id == shot2
    candidate3 = await _commit_shot_e2e(container, shot3, idempotency_key="settle-race-3")
    from director_production.pipeline import CandidateNotCommittable

    with pytest.raises(CandidateNotCommittable, match="already SETTLED"):
        container.candidates.commit(candidate3.id)
    with container.database.session() as session:
        shot3_row = session.get(Shot, shot3)
        assert shot3_row.committed_candidate_id is None


@pytest.mark.asyncio
async def test_commit_refuses_a_candidate_from_an_expired_narrative_context(  # type: ignore[no-untyped-def]
    container, project, ledger, offline_video_provider
):
    """The ledger moved between generation and commit: the candidate is stale."""

    _, result = _compile(
        container,
        project,
        """INT. KITCHEN - DAY
LinJin picks up the phone
""",
    )
    shot1 = result.shot_ids[0]
    candidate = await _commit_shot_e2e(container, shot1, idempotency_key="expired-context-1")
    # The story moved after generation: a new obligation opened at a position
    # this shot can see, so the context its prompt was compiled from is stale.
    ledger.open_obligation(
        project.id,
        obligation_key="opened_after_generation",
        promise="A promise the candidate never saw.",
        episode=1,
    )
    from director_production.pipeline import CandidateNotCommittable

    with pytest.raises(CandidateNotCommittable, match="narrative context changed"):
        container.candidates.commit(candidate.id)
