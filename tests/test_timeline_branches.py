"""Timeline branch lifecycle: registration, merge policy, retirement, sweeps, history."""

from __future__ import annotations

import threading

import pytest
from character_core import (
    TimelineBranchConflict,
    TimelineBranchError,
    TimelineBranchReferenced,
    TimelineBranchService,
    assert_branch_writable_in_session,
)
from production_domain.models import (
    CharacterStateVersion,
    Episode,
    TimelineBranch,
    TimelineTransition,
    utcnow,
)
from sqlalchemy import select

SCRIPT = """INT. KITCHEN - DAY
LinJin picks up the phone
LinJin turns toward the door
INT. HALLWAY - NIGHT
LinJin walks toward the door
"""


@pytest.fixture
def branches(container):  # type: ignore[no-untyped-def]
    return TimelineBranchService(container.database)


def _seed_state_reference(container, project, scope: str, *, seed: str) -> str:  # type: ignore[no-untyped-def]
    """A CharacterStateVersion on `scope` — the lightest auditable reference."""

    import hashlib as _hashlib

    from production_domain.models import Character, CharacterIdentityVersion, MediaAsset

    with container.database.session() as session:
        payload = f"identity-{seed}".encode()
        master = MediaAsset(
            project_id=project.id,
            asset_type="CHARACTER_MASTER",
            mime_type="image/png",
            storage_key=f"identities/{seed}.png",
            sha256=_hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )
        session.add(master)
        session.flush()
        character = Character(project_id=project.id, name=f"Branch{seed}")
        session.add(character)
        session.flush()
        identity = CharacterIdentityVersion(
            character_id=character.id, version=1, master_asset_id=master.id
        )
        session.add(identity)
        session.flush()
        version = CharacterStateVersion(
            project_id=project.id,
            character_id=character.id,
            timeline_scope_key=scope,
            version=1,
            identity_version_id=identity.id,
            identity_fingerprint="f" * 64,
            state_hash=_hashlib.sha256(f"{scope}:{seed}".encode()).hexdigest(),
            narrative_state_json={},
            state_schema_version="test",
        )
        session.add(version)
        session.flush()
        return character.id


def _compile(container, project, *, episode_number: int = 1):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title=f"Episode {episode_number}",
            episode_number=episode_number,
            script_source=SCRIPT,
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    return container.narrative.compile_episode(episode_id)


def test_a_dream_transition_registers_its_branch_with_parent_and_fork(  # type: ignore[no-untyped-def]
    container, project, branches
) -> None:
    result = _compile(container, project)
    first, second, _third = result.shot_ids
    container.narrative_timeline_engine = None  # not a container field; direct use below
    from narrative_core import AuthoritativeTimelineStateEngine

    engine = AuthoritativeTimelineStateEngine(container.database)
    engine.set_transition(second, "DREAM")
    listed = branches.list_for_project(project.id)
    assert len(listed) == 1
    branch = listed[0]
    assert branch["scope_key"] == f"dream:{second}"
    assert branch["branch_kind"] == "DREAM"
    assert branch["status"] == "ACTIVE"
    assert branch["parent_scope_key"] == "main", "a dream must have a clear parent"
    assert branch["fork_shot_id"] == first
    assert branch["metadata"]["first_branch_shot_id"] == second


def test_merge_requires_declared_paths_and_blocks_dream_states_by_default(  # type: ignore[no-untyped-def]
    container, project, branches
) -> None:
    result = _compile(container, project)
    second = result.shot_ids[1]
    from narrative_core import AuthoritativeTimelineStateEngine

    AuthoritativeTimelineStateEngine(container.database).set_transition(second, "DREAM")
    scope = f"dream:{second}"
    with pytest.raises(TimelineBranchError, match="must declare which state paths"):
        branches.merge(
            project.id, scope, allowed_state_paths=[], merged_by="director"
        )
    with pytest.raises(TimelineBranchError, match="dream states do not merge into the main"):
        branches.merge(
            project.id,
            scope,
            allowed_state_paths=["injuries"],
            merged_by="director",
        )
    merged = branches.merge(
        project.id,
        scope,
        allowed_state_paths=["injuries", "props.held"],
        merged_by="director",
        allow_dream_states=True,
    )
    assert merged["status"] == "MERGED"
    assert merged["merge_policy"]["allowed_state_paths"] == ["injuries", "props.held"]
    assert merged["merge_policy"]["allow_dream_states"] is True
    # A closed branch accepts no more state writes.
    with container.database.session() as session:
        with pytest.raises(TimelineBranchError, match="no longer accepts state writes"):
            assert_branch_writable_in_session(
                session, project_id=project.id, scope_key=scope
            )


def test_merge_captures_a_manifest_of_only_the_allowed_paths(  # type: ignore[no-untyped-def]
    container, project, branches
) -> None:
    # Episode 2: the committed-state seeding helper owns episode number 1.
    result = _compile(container, project, episode_number=2)
    second = result.shot_ids[1]
    from narrative_core import AuthoritativeTimelineStateEngine

    AuthoritativeTimelineStateEngine(container.database).set_transition(second, "FLASHBACK")
    scope = f"flashback:{second}"
    from test_character_state_schema import _committed_initial_state

    seeded = _committed_initial_state(
        container,
        project,
        timeline_scope_key=scope,
        narrative_state={
            "injuries": ["scar"],
            "secret": "not merged",
            "props": {"held": ["locket"]},
        },
    )
    character_id = seeded["character_id"]
    merged = branches.merge(
        project.id,
        scope,
        allowed_state_paths=["injuries", "props.held", "absent.path"],
        merged_by="director",
    )
    manifest = merged["merge_manifest"][character_id]
    assert manifest["values"] == {"injuries": ["scar"], "props.held": ["locket"]}
    assert "secret" not in str(manifest["values"])


@pytest.mark.postgres_only
def test_merge_waits_for_an_inflight_branch_state_write(container, project, branches) -> None:  # type: ignore[no-untyped-def]
    result = _compile(container, project, episode_number=2)
    second = result.shot_ids[1]
    from narrative_core import AuthoritativeTimelineStateEngine
    from production_domain.models import CharacterStateHead
    from test_character_state_schema import _committed_initial_state

    AuthoritativeTimelineStateEngine(container.database).set_transition(second, "FLASHBACK")
    scope = f"flashback:{second}"
    seeded = _committed_initial_state(
        container,
        project,
        timeline_scope_key=scope,
        narrative_state={"injuries": ["old"]},
    )
    outcome: dict[str, object] = {}

    def merge_branch() -> None:
        outcome.update(
            branches.merge(
                project.id,
                scope,
                allowed_state_paths=["injuries"],
                merged_by="concurrency-test",
            )
        )

    with container.database.session() as session:
        assert_branch_writable_in_session(
            session, project_id=project.id, scope_key=scope
        )
        thread = threading.Thread(target=merge_branch)
        thread.start()
        thread.join(timeout=0.2)
        assert thread.is_alive(), "merge must wait while a state writer owns the branch row"
        head = session.scalar(
            select(CharacterStateHead).where(
                CharacterStateHead.project_id == project.id,
                CharacterStateHead.character_id == seeded["character_id"],
                CharacterStateHead.timeline_scope_key == scope,
            )
        )
        assert head is not None
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert outcome["merge_manifest"][seeded["character_id"]]["values"] == {
        "injuries": ["old"]
    }


def test_concurrent_merges_produce_one_winner(container, project, branches) -> None:  # type: ignore[no-untyped-def]
    result = _compile(container, project)
    second = result.shot_ids[1]
    from narrative_core import AuthoritativeTimelineStateEngine

    AuthoritativeTimelineStateEngine(container.database).set_transition(second, "FLASHBACK")
    scope = f"flashback:{second}"
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(who: str) -> None:
        barrier.wait()
        try:
            branches.merge(
                project.id, scope, allowed_state_paths=["injuries"], merged_by=who
            )
            result = "MERGED"
        except (TimelineBranchConflict, Exception) as exc:  # noqa: BLE001 - record what happened
            result = type(exc).__name__
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("MERGED") == 1, outcomes


def test_repeated_retire_is_a_noop_and_history_stays_readable(  # type: ignore[no-untyped-def]
    container, project, branches
) -> None:
    result = _compile(container, project)
    second = result.shot_ids[1]
    from narrative_core import AuthoritativeTimelineStateEngine

    AuthoritativeTimelineStateEngine(container.database).set_transition(second, "DREAM")
    scope = f"dream:{second}"
    first_close = branches.retire(project.id, scope, reason="the dream arc was cut")
    assert first_close["status"] == "RETIRED"
    replay = branches.retire(project.id, scope, reason="the dream arc was cut")
    assert replay["status"] == "RETIRED"
    assert replay["retired_at"] == first_close["retired_at"]
    # History read: the transition that anchors the branch is still there.
    with container.database.session() as session:
        transition = session.scalar(
            select(TimelineTransition).where(TimelineTransition.branch_key == scope)
        )
        assert transition is not None
    listed = branches.get(project.id, scope)
    assert listed["retire_reason"] == "the dream arc was cut"


def test_referenced_branches_cannot_be_purged(container, project, branches) -> None:  # type: ignore[no-untyped-def]
    result = _compile(container, project)
    second = result.shot_ids[1]
    from narrative_core import AuthoritativeTimelineStateEngine

    AuthoritativeTimelineStateEngine(container.database).set_transition(second, "DREAM")
    scope = f"dream:{second}"
    branches.retire(project.id, scope, reason="done")
    with pytest.raises(TimelineBranchReferenced, match="still referenced"):
        branches.purge(project.id, scope)
    # Remove the anchoring reference; the closed branch may then go.
    with container.database.session() as session:
        transition = session.scalar(
            select(TimelineTransition).where(TimelineTransition.branch_key == scope)
        )
        session.delete(transition)
    branches.purge(project.id, scope)
    with pytest.raises(LookupError):
        branches.get(project.id, scope)


def test_orphan_sweep_abandons_unreferenced_idle_branches_only(  # type: ignore[no-untyped-def]
    container, project, branches
) -> None:
    """Branch proliferation: registered branches nothing references get closed."""

    from datetime import timedelta

    branches.ensure(
        project.id, "alternate:orphan-1", branch_kind="ALTERNATE", parent_scope_key="main"
    )
    branches.ensure(
        project.id, "alternate:orphan-2", branch_kind="ALTERNATE", parent_scope_key="main"
    )
    with container.database.session() as session:
        for row in session.scalars(select(TimelineBranch)):
            row.created_at = utcnow() - timedelta(days=30)
    # One of them gains a reference and must survive the sweep.
    _seed_state_reference(container, project, "alternate:orphan-2", seed="orphan")
    swept = branches.sweep_orphans(project.id, min_idle_seconds=60)
    assert swept.abandoned == ["alternate:orphan-1"]
    assert swept.kept_referenced == 1
    assert branches.get(project.id, "alternate:orphan-1")["status"] == "ABANDONED"
    assert branches.get(project.id, "alternate:orphan-2")["status"] == "ACTIVE"


def test_main_cannot_be_closed_and_non_main_requires_a_parent(container, project, branches) -> None:  # type: ignore[no-untyped-def]
    branches.ensure(project.id, "main")
    with pytest.raises(TimelineBranchError, match="cannot be retired"):
        branches.retire(project.id, "main", reason="never")
    with pytest.raises(TimelineBranchError, match="cannot be merged"):
        branches.merge(
            project.id, "main", allowed_state_paths=["x"], merged_by="nobody"
        )
    ensured = branches.ensure(project.id, "dream:implicit-parent")
    assert ensured["parent_scope_key"] == "main"
    assert ensured["branch_kind"] == "DREAM"
