"""Advisory vector memory must never damage what it is describing.

Three failures pinned here, all of them the same mistake in different clothes:
advisory work behaving as though it were allowed to fail a business request.

* a duplicate enqueue rolling back the *paid* transaction it was queued inside;
* an embedding outage closing a row as DONE, costing that artefact its vector
  for ever;
* an undecodable frame escaping as a 500 from asset confirmation.
"""

from __future__ import annotations

from typing import Any

from memory_core.outbox import (
    DEFERRED_RETRY_SECONDS,
    RETRY_BACKOFF_SECONDS,
    MemoryIndexOutboxWorker,
    MemoryIndexOutboxWriter,
)
from production_domain.models import MemoryIndexOutbox
from sqlalchemy import select


def _writer(container) -> MemoryIndexOutboxWriter:  # type: ignore[no-untyped-def]
    return MemoryIndexOutboxWriter(container.database)


class _RacedSession:
    """A session whose first duplicate-check reads as though the row is absent.

    That is exactly the race the savepoint exists for: two transactions both
    run `enqueue`'s pre-check before either has inserted, both see nothing, and
    the loser meets the unique index on flush. Simulating the missed read makes
    the collision deterministic on both engines instead of only under real
    concurrency on PostgreSQL.
    """

    def __init__(self, session: Any) -> None:
        self._session = session
        self._missed = False

    def scalar(self, *args: Any, **kwargs: Any) -> Any:
        if not self._missed:
            self._missed = True
            return None
        return self._session.scalar(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


def test_a_duplicate_enqueue_cannot_roll_back_the_callers_own_write(container, project) -> None:  # type: ignore[no-untyped-def]
    """The savepoint that keeps a paid commit safe from an advisory row.

    `enqueue(session=...)` deliberately writes inside the caller's transaction
    so a memory is never queued for Canon that rolled back. The cost is that a
    unique-key collision - two commits racing on the same idempotency key -
    raised IntegrityError straight into that transaction and took the business
    write down with it. The caller here is a stand-in for a candidate commit
    the user has already been charged for.
    """

    from production_domain.models import Project

    writer = _writer(container)
    key = "bible-lock:duplicate-key"

    with container.database.session() as session:
        assert writer.enqueue(
            project.id,
            session=session,
            idempotency_key=key,
            source="VISUAL_BIBLE_LOCK",
            memory_type="STYLE",
            text="first",
        )

    with container.database.session() as session:
        # The business write: what the caller is actually here to do.
        row = session.get(Project, project.id)
        row.title = "committed alongside a duplicate enqueue"
        session.flush()
        # The collision. Before the savepoint this raised out of enqueue and
        # rolled the title change back with it.
        assert (
            writer.enqueue(
                project.id,
                session=_RacedSession(session),
                idempotency_key=key,
                source="VISUAL_BIBLE_LOCK",
                memory_type="STYLE",
                text="second",
            )
            is None
        )
        # The caller's transaction is still usable after the collision, which
        # is the other half of what the savepoint buys.
        row.title = "still writable after the collision"
        session.flush()

    with container.database.session() as session:
        assert session.get(Project, project.id).title == "still writable after the collision"
        queued = list(
            session.scalars(
                select(MemoryIndexOutbox).where(MemoryIndexOutbox.idempotency_key == key)
            )
        )
        assert len(queued) == 1
        assert queued[0].payload_json["text"] == "first"


# --------------------------------------------------------------------------
# A degraded vector is retried, not silently accepted for ever
# --------------------------------------------------------------------------


class _DegradedMemory:
    """An engine whose embedding provider is down: the row lands, unvectored."""

    def __init__(self, *, degrade_times: int) -> None:
        self.calls = 0
        self._degrade_times = degrade_times

    def index(self, value):  # type: ignore[no-untyped-def]
        self.calls += 1
        degraded = self.calls <= self._degrade_times
        return type(
            "Memory",
            (),
            {
                "id": f"memory-{self.calls}",
                "metadata_json": {"vector_degraded": True} if degraded else {},
            },
        )()


def test_an_embedding_outage_is_retried_rather_than_closed_as_done(container, project) -> None:  # type: ignore[no-untyped-def]
    """Pins the fix for "queued during the outage, vector-less for ever".

    The engine writes the structurally retrievable row even when the embedding
    provider is down and marks the vector degraded. Recording that as DONE
    meant a provider blip during a deploy window permanently cost those
    artefacts their vector, with only a `last_error` string to find them by.
    """

    memory = _DegradedMemory(degrade_times=1)
    worker = MemoryIndexOutboxWorker(container.database, memory)
    _writer(container).enqueue(
        project.id,
        idempotency_key="shot:degraded",
        source="CANDIDATE_COMMIT",
        memory_type="SHOT_RESULT",
        text="a shot that outlived the outage",
    )

    first = worker.drain(limit=10)
    assert (first.retried, first.indexed) == (1, 0)
    with container.database.session() as session:
        row = session.scalar(select(MemoryIndexOutbox))
        assert row.status == "PENDING"
        assert row.last_error == "VECTOR_DEGRADED"
        assert row.next_attempt_at is not None
        row.next_attempt_at = None  # make it due again without waiting out the backoff
        session.flush()

    second = worker.drain(limit=10)
    assert (second.retried, second.indexed) == (0, 1)
    with container.database.session() as session:
        row = session.scalar(select(MemoryIndexOutbox))
        assert row.status == "DONE"
        assert row.last_error is None
        assert memory.calls == 2


def test_a_permanently_degraded_row_still_closes_rather_than_retrying_for_ever(
    container, project
) -> None:  # type: ignore[no-untyped-def]
    """The retry budget is the same one every other failure gets."""

    memory = _DegradedMemory(degrade_times=99)
    worker = MemoryIndexOutboxWorker(container.database, memory, max_attempts=2)
    _writer(container).enqueue(
        project.id,
        idempotency_key="shot:always-degraded",
        source="CANDIDATE_COMMIT",
        memory_type="SHOT_RESULT",
        text="the provider never came back",
    )

    worker.drain(limit=10)
    with container.database.session() as session:
        row = session.scalar(select(MemoryIndexOutbox))
        assert row.status == "PENDING"
        row.next_attempt_at = None
        session.flush()
    worker.drain(limit=10)

    with container.database.session() as session:
        row = session.scalar(select(MemoryIndexOutbox))
        assert row.status == "DONE"
        assert row.last_error == "VECTOR_DEGRADED"
    assert memory.calls == 2


def test_a_flag_off_deferral_outlasts_the_sweep_that_would_re_examine_it() -> None:
    """Starvation: a deferral shorter than the sweep interval defers nothing.

    Rows whose project has the flag off are pushed forward so they cannot hold
    the head of the queue. At 60s with a 120s sweep they were due again every
    single pass, refilled the batch limit, and an enabled project queued behind
    them was never reached.
    """

    from platform_shared.config import Settings

    interval = Settings().memory_index_sweep_interval_seconds
    assert DEFERRED_RETRY_SECONDS > interval, (
        f"deferral {DEFERRED_RETRY_SECONDS}s must outlast the {interval}s sweep"
    )
    # And it must still be shorter than the longest ordinary retry backoff, or
    # a flag-off row is treated worse than a repeatedly failing one.
    assert DEFERRED_RETRY_SECONDS < max(RETRY_BACKOFF_SECONDS)


# --------------------------------------------------------------------------
# An undecodable frame degrades; it does not reach the caller
# --------------------------------------------------------------------------


def test_a_decompression_bomb_frame_yields_no_frame_instead_of_raising() -> None:
    """Pillow's DecompressionBombError derives from Exception, not OSError.

    `_bounded_frame` caught (OSError, ValueError), so a frame above Pillow's
    own pixel ceiling escaped into asset confirmation as a business 500 - on a
    path whose whole contract is that Voyage indexing is advisory and failure
    degrades.
    """

    import io

    from memory_core import embedding as embedding_module
    from PIL import Image

    class _Bomb:
        def __enter__(self):  # type: ignore[no-untyped-def]
            raise Image.DecompressionBombError("frame exceeds the pixel ceiling")

        def __exit__(self, *_exc):  # type: ignore[no-untyped-def]
            return False

    original = embedding_module.Image.open
    embedding_module.Image.open = lambda *_a, **_k: _Bomb()  # type: ignore[assignment]
    try:
        assert embedding_module._bounded_frame(b"not really a png") is None
    finally:
        embedding_module.Image.open = original  # type: ignore[assignment]

    # A genuinely malformed frame still degrades the same way.
    assert embedding_module._bounded_frame(io.BytesIO(b"garbage").getvalue()) is None
