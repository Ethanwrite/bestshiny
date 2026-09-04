"""What the director creates can be found again.

`CreativeDirectorService` had no memory engine at all, so the character
identities, the style plate and the canonical scene, product and prop key
visuals a locked visual bible produces never entered the vector memory; and a
committed shot result was indexed only from the Passenger route, never from a
candidate commit. A project could hold a complete visual bible and still return
nothing on a similarity query.

The fix cannot be a direct call: embedding is an external HTTPS request, and
making it inside the transaction that locks a bible or commits a candidate
would put a vendor's availability on the critical path of Canon. So the writer
enqueues one durable row and a worker drains it - advisory, idempotent, and
unable to fail the business request either way.
"""

from __future__ import annotations

import pytest
from memory_core import AuthorityLevel, EvidencePurpose, MemoryLayer
from memory_core.outbox import (
    MEMORY_FEATURE_FLAG,
    MemoryIndexOutboxWorker,
    MemoryIndexOutboxWriter,
)
from production_domain.models import MemoryIndexOutbox, ShotMemory
from sqlalchemy import select
from test_creative_director import (
    RICH_IDEA,
    ScriptedDirector,
    _approve_brief,
    _approve_screenplay,
    _client,
    _complete_visuals,
    _registered_pro,
    _rich_turn,
    _wire_openrouter_images,
)
from test_creative_director import (
    openrouter_container as openrouter_container,  # re-exported so the fixture resolves here
)


class _AllowAll:
    def enabled(self, name: str, *, project_id: str | None = None) -> bool:
        del project_id
        return name == MEMORY_FEATURE_FLAG


class _DenyAll:
    def enabled(self, name: str, *, project_id: str | None = None) -> bool:
        del name, project_id
        return False


async def _locked_bible(container, client, headers, project_id):  # type: ignore[no-untyped-def]
    started = client.post(
        "/v1/creative/sessions", headers=headers, json={"project_id": project_id, "idea": RICH_IDEA}
    ).json()
    session_id = started["session_id"]
    _approve_brief(client, session_id, started["brief_revision"], headers)
    _approve_screenplay(client, session_id, headers)
    await _complete_visuals(container, client, session_id, headers)
    bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose", headers=headers).json()
    locked = client.post(
        f"/v1/creative/sessions/{session_id}/bible/approve",
        headers=headers,
        json={"version": bible["version"]},
    )
    assert locked.status_code == 200, locked.text
    return session_id, locked.json()


@pytest.mark.asyncio
async def test_locking_a_bible_queues_every_canonical_artefact(openrouter_container):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "memory-lock@example.com")
        _session_id, locked = await _locked_bible(container, client, headers, project_id)

    with container.database.session() as session:
        rows = list(
            session.scalars(
                select(MemoryIndexOutbox).where(MemoryIndexOutbox.project_id == project_id)
            )
        )
    kinds = {row.payload_json["memory_type"] for row in rows}
    # Both identities, the style plate, and every canonical supporting asset.
    assert "CHARACTER_IDENTITY" in kinds
    assert "STYLE" in kinds
    assert {"SCENE", "PROP"} & kinds, kinds
    assert all(row.status == "PENDING" for row in rows)
    assert all(row.source == "VISUAL_BIBLE_LOCK" for row in rows)
    # Advisory by construction, never authoritative.
    assert {row.payload_json["authority_level"] for row in rows} == {AuthorityLevel.ADVISORY.value}
    assert {row.payload_json["evidence_purpose"] for row in rows} == {
        EvidencePurpose.RETRIEVAL_HINT.value
    }
    # Every row names the artefact it remembers, and the lineage behind it.
    identities = [row for row in rows if row.payload_json["memory_type"] == "CHARACTER_IDENTITY"]
    assert identities
    for row in identities:
        assert row.payload_json["media_asset_ids"]
        assert row.payload_json["metadata"]["visual_bible_id"] == locked["id"]
        assert row.idempotency_key.startswith("memory:identity:")


@pytest.mark.asyncio
async def test_relocking_the_same_bible_queues_nothing_new(openrouter_container):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "memory-replay@example.com")
        session_id, locked = await _locked_bible(container, client, headers, project_id)
        with container.database.session() as session:
            before = session.scalar(
                select(MemoryIndexOutbox.id).where(MemoryIndexOutbox.project_id == project_id)
            )
            count_before = len(
                list(
                    session.scalars(
                        select(MemoryIndexOutbox).where(
                            MemoryIndexOutbox.project_id == project_id
                        )
                    )
                )
            )
        again = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": locked["version"]},
        )
        assert again.status_code == 200
    with container.database.session() as session:
        rows = list(
            session.scalars(
                select(MemoryIndexOutbox).where(MemoryIndexOutbox.project_id == project_id)
            )
        )
    assert len(rows) == count_before
    assert before is not None


@pytest.mark.asyncio
async def test_the_worker_indexes_the_queue_and_the_assets_become_searchable(
    openrouter_container,
):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "memory-drain@example.com")
        await _locked_bible(container, client, headers, project_id)

    worker = MemoryIndexOutboxWorker(container.database, container.memory, flags=_AllowAll())
    result = worker.drain(limit=50)
    assert result.indexed >= 3, result.as_dict()
    assert result.failed == 0

    with container.database.session() as session:
        memories = list(
            session.scalars(select(ShotMemory).where(ShotMemory.project_id == project_id))
        )
        rows = list(
            session.scalars(
                select(MemoryIndexOutbox).where(MemoryIndexOutbox.project_id == project_id)
            )
        )
    assert {row.status for row in rows} == {"DONE"}
    assert all(row.shot_memory_id for row in rows)
    assert len(memories) == len(rows)
    # ADVISORY, never authoritative: nothing here writes a fact or a verdict.
    assert all(memory.canonical is False for memory in memories)
    assert all(memory.layer == MemoryLayer.EPISODIC.value for memory in memories)

    # And a later shot's query finds the director's own assets.
    from memory_core import MemoryQuery

    found = container.memory.search(
        MemoryQuery(project_id=project_id, text="Mira canonical identity", top_k=5)
    )
    assert found, [memory.memory_type for memory in memories]
    assert any(item.memory_type == "CHARACTER_IDENTITY" for item in found)


@pytest.mark.asyncio
async def test_draining_twice_writes_one_memory_per_artefact(openrouter_container):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "memory-twice@example.com")
        await _locked_bible(container, client, headers, project_id)

    worker = MemoryIndexOutboxWorker(container.database, container.memory, flags=_AllowAll())
    first = worker.drain(limit=50)
    second = worker.drain(limit=50)
    assert second.indexed == 0 and second.claimed == 0
    with container.database.session() as session:
        memories = list(
            session.scalars(select(ShotMemory).where(ShotMemory.project_id == project_id))
        )
    assert len(memories) == first.indexed


@pytest.mark.asyncio
async def test_the_flag_governs_whether_anything_is_embedded(openrouter_container):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "memory-flag@example.com")
        await _locked_bible(container, client, headers, project_id)

    off = MemoryIndexOutboxWorker(container.database, container.memory, flags=_DenyAll())
    result = off.drain(limit=50)
    assert result.indexed == 0 and result.deferred > 0
    with container.database.session() as session:
        rows = list(
            session.scalars(
                select(MemoryIndexOutbox).where(MemoryIndexOutbox.project_id == project_id)
            )
        )
        memories = list(
            session.scalars(select(ShotMemory).where(ShotMemory.project_id == project_id))
        )
    # Waiting, not lost: enabling the flag later indexes exactly the same work.
    assert {row.status for row in rows} == {"PENDING"}
    assert memories == []
    on = MemoryIndexOutboxWorker(container.database, container.memory, flags=_AllowAll())
    assert on.drain(limit=50).indexed == len(rows)


@pytest.mark.asyncio
async def test_an_embedding_outage_never_touches_canon_and_backs_off(openrouter_container):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "memory-down@example.com")
        _session_id, locked = await _locked_bible(container, client, headers, project_id)

    class _Broken:
        def index(self, value):  # type: ignore[no-untyped-def]
            del value
            raise RuntimeError("the embedding provider is unreachable")

    worker = MemoryIndexOutboxWorker(container.database, _Broken(), flags=_AllowAll())
    result = worker.drain(limit=50)
    assert result.indexed == 0 and result.retried > 0
    with container.database.session() as session:
        rows = list(
            session.scalars(
                select(MemoryIndexOutbox).where(MemoryIndexOutbox.project_id == project_id)
            )
        )
    assert {row.status for row in rows} == {"PENDING"}
    assert all(row.attempts == 1 and row.next_attempt_at for row in rows)
    assert all("unreachable" in (row.last_error or "") for row in rows)
    # The lock itself is untouched: Canon does not depend on being remembered.
    assert locked["lineage"]["lock_status"] == "LOCKED"
    assert locked["status"] == "LOCKED"


def test_committing_a_candidate_queues_the_shot_result(
    container, project, account_worker, register_bytes
):  # type: ignore[no-untyped-def]
    """The second source: a shot that became canon is remembered too."""

    from production_domain.models import Episode

    account_id, _worker = account_worker
    container.flow_affinity.bind_existing(
        local_project_id=project.id,
        provider_account_id=account_id,
        provider_project_id="flow-project-test",
    )
    writer = MemoryIndexOutboxWriter(container.database)
    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title="E1",
            episode_number=1,
            script_source="INT. ROOM - NIGHT\nMira raises the phone.\n",
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    shot_id = container.narrative.compile_episode(episode_id).shot_ids[0]

    # The commit path's own enqueue, exercised directly on the same contract
    # the pipeline uses, so this test does not need a full generation.
    asset = register_bytes(container, project.id, "SHOT_OUTPUT", b"committed-shot")
    with container.database.session() as session:
        assert (
            writer.enqueue(
                project.id,
                session=session,
                idempotency_key=f"memory:shot:{shot_id}:candidate:candidate-1",
                source="CANDIDATE_COMMIT",
                memory_type="SHOT_RESULT",
                text="Mira raises the phone",
                media_asset_ids=[asset.id],
                shot_id=shot_id,
            )
            is not None
        )
        # Same artefact, same key: one memory.
        assert (
            writer.enqueue(
                project.id,
                session=session,
                idempotency_key=f"memory:shot:{shot_id}:candidate:candidate-1",
                source="CANDIDATE_COMMIT",
                memory_type="SHOT_RESULT",
                text="Mira raises the phone",
                media_asset_ids=[asset.id],
                shot_id=shot_id,
            )
            is None
        )
    worker = MemoryIndexOutboxWorker(container.database, container.memory, flags=_AllowAll())
    assert worker.drain().indexed == 1
    with container.database.session() as session:
        memories = list(
            session.scalars(select(ShotMemory).where(ShotMemory.shot_id == shot_id))
        )
    assert len(memories) == 1
    assert memories[0].memory_type == "SHOT_RESULT"
    assert memories[0].canonical is False


def test_the_pipeline_is_wired_to_the_outbox(container) -> None:  # type: ignore[no-untyped-def]
    """Pinned so the enqueue cannot be quietly unwired from the container."""

    assert container.candidates.memory_outbox is not None
    assert container.creative_director.memory_outbox is not None
    assert container.memory_outbox_worker.flags is container.feature_flags
