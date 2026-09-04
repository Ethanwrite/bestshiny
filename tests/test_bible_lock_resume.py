"""A half-locked visual bible is resumable, and never duplicates Canon.

Locking a bible writes immutable Canon through three services that cannot share
a transaction. Asset versions, canonical promotions and style locks are
append-only by database trigger, and a project has exactly one style lock, so a
failure part-way could not be rolled back - and the retry, whose only replay
guard was an in-memory dict persisted *after* the writes, minted a second
identity version and a second canonical asset version for the same face.

These tests fail the lock at each step in turn, retry, and assert that exactly
one of everything exists at the end. The step ledger makes the resume ordered;
per-step discovery of the Canon is what makes it exactly-once even when the
process dies between the write and the ledger stamp.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from creative_director_core import CreativeSessionConflict
from production_domain.models import (
    AssetCanonicalPromotion,
    AssetVersion,
    CharacterIdentityVersion,
    CreativeLockStep,
    ProjectStyleLock,
    VisualBibleVersion,
    utcnow,
)
from sqlalchemy import func, select
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

PRODUCT = "Aurora Serum"


class _Boom(RuntimeError):
    """The failure the retry has to survive."""


async def _ready_to_lock(container, client, headers, project_id):  # type: ignore[no-untyped-def]
    started = client.post(
        "/v1/creative/sessions", headers=headers, json={"project_id": project_id, "idea": RICH_IDEA}
    ).json()
    session_id = started["session_id"]
    edited = client.post(
        f"/v1/creative/sessions/{session_id}/brief/edit",
        headers=headers,
        json={
            "operations": [
                {"op": "SET", "path": "product.name", "value": PRODUCT, "evidence": "brief editor"}
            ]
        },
    )
    assert edited.status_code == 200, edited.text
    _approve_brief(client, session_id, edited.json()["revision"], headers)
    _approve_screenplay(client, session_id, headers)
    await _complete_visuals(container, client, session_id, headers)
    bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose", headers=headers).json()
    return session_id, bible["version"]


def _canon_counts(container, project_id: str) -> dict[str, int]:
    with container.database.session() as session:
        return {
            "identities": int(
                session.scalar(select(func.count(CharacterIdentityVersion.id))) or 0
            ),
            "asset_versions": int(session.scalar(select(func.count(AssetVersion.id))) or 0),
            "promotions": int(session.scalar(select(func.count(AssetCanonicalPromotion.id))) or 0),
            "style_locks": int(
                session.scalar(
                    select(func.count(ProjectStyleLock.id)).where(
                        ProjectStyleLock.project_id == project_id
                    )
                )
                or 0
            ),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("break_after", ["STYLE", "CHARACTER_IDENTITY", "SUPPORTING_ASSET"])
@pytest.mark.parametrize(
    "when",
    [
        # The step finished and the ledger recorded it: the retry must skip it.
        "after_step",
        # The Canon was written but the process died before the ledger stamp:
        # the retry must re-enter the step and *discover* its own output rather
        # than minting a second identity version or asset version.
        "after_canon",
    ],
)
async def test_a_lock_that_fails_after_a_step_resumes_without_duplicating_canon(
    openrouter_container, break_after, when
):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    service = container.creative_director
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(
            client, container, f"resume-{break_after.lower()}@example.com"
        )
        session_id, version = await _ready_to_lock(container, client, headers, project_id)

        # Explode immediately *after* the chosen step's Canon is written and
        # its ledger row stamped: the worst case for a naive retry.
        original = service._run_lock_step
        broken: list[str] = []

        def exploding(**kwargs):  # type: ignore[no-untyped-def]
            if kwargs["kind"] == break_after and not broken:
                broken.append(kwargs["kind"])
                if when == "after_canon":
                    real_execute = kwargs["execute"]

                    def execute_then_die():  # type: ignore[no-untyped-def]
                        real_execute()  # the Canon is written...
                        raise _Boom("died before the ledger stamp")

                    return original(**{**kwargs, "execute": execute_then_die})
                original(**kwargs)
                raise _Boom(f"crash after {break_after}")
            return original(**kwargs)

        service._run_lock_step = exploding  # type: ignore[method-assign]
        failed = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": version},
        )
        assert failed.status_code == 409, failed.text
        detail = failed.json()["detail"]
        assert detail["reason_code"] == "LOCK_FAILED"
        assert detail["retryable"] is True
        lineage = detail["lineage"]
        assert lineage["lock_status"] == "FAILED"
        # The recovery record: exactly which steps stand and which are missing.
        statuses = {item["kind"]: item["status"] for item in lineage["steps"]}
        assert break_after in statuses, statuses
        assert statuses[break_after] == ("FAILED" if when == "after_canon" else "COMPLETED")
        after_failure = _canon_counts(container, project_id)

        with container.database.session() as session:
            bible = session.scalar(
                select(VisualBibleVersion).where(VisualBibleVersion.session_id == session_id)
            )
            assert bible.status == "DRAFT"  # never LOCKED on a partial lock

        # Retry, this time without the crash.
        service._run_lock_step = original  # type: ignore[method-assign]
        locked = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": version},
        )
        assert locked.status_code == 200, locked.text
        assert locked.json()["status"] == "LOCKED"
        assert locked.json()["lineage"]["lock_status"] == "LOCKED"

    final = _canon_counts(container, project_id)
    # Exactly one identity per character anchor, one style lock, and no
    # duplicate asset version or promotion anywhere.
    with container.database.session() as session:
        anchors = _state_anchor_counts(container, session_id)
        steps = list(
            session.scalars(select(CreativeLockStep).where(CreativeLockStep.session_id == session_id))
        )
    assert final["style_locks"] == 1
    assert final["identities"] == anchors["CHARACTER"]
    assert final["asset_versions"] == final["promotions"]
    # Every step is COMPLETED, and the ones that had already run were
    # recovered rather than executed a second time.
    assert {step.status for step in steps} == {"COMPLETED"}
    of_kind = [step for step in steps if step.step_kind == break_after]
    assert of_kind, [step.step_kind for step in steps]
    if when == "after_canon":
        # Exactly one step of that kind ran twice, and its second run
        # *recovered* its own earlier output rather than executing again -
        # that is the exactly-once guarantee, proved.
        retried = [step for step in of_kind if step.attempts == 2]
        assert len(retried) == 1, [(s.step_key, s.attempts) for s in of_kind]
        assert retried[0].resolution == "RECOVERED"
        assert retried[0].produced_json.get("recovered") is True
    else:
        # The step had already completed, so the retry skipped it entirely.
        assert all(step.attempts == 1 for step in of_kind)
        assert {step.resolution for step in of_kind} == {"EXECUTED"}
    # Nothing was minted twice: one identity per character, and one asset
    # version per (asset, key visual) - the two duplications the old retry made.
    del after_failure
    with container.database.session() as session:
        per_character = session.execute(
            select(CharacterIdentityVersion.character_id, func.count(CharacterIdentityVersion.id))
            .group_by(CharacterIdentityVersion.character_id)
        ).all()
        per_asset_media = session.execute(
            select(
                AssetVersion.asset_id,
                AssetVersion.primary_media_asset_id,
                func.count(AssetVersion.id),
            ).group_by(AssetVersion.asset_id, AssetVersion.primary_media_asset_id)
        ).all()
    assert all(count == 1 for _character, count in per_character), per_character
    assert all(count == 1 for _asset, _media, count in per_asset_media), per_asset_media


def _state_anchor_counts(container, session_id: str) -> dict[str, int]:
    from production_domain.models import CreativeVisualAnchor

    with container.database.session() as session:
        anchors = list(
            session.scalars(
                select(CreativeVisualAnchor).where(
                    CreativeVisualAnchor.session_id == session_id,
                    CreativeVisualAnchor.status == "READY",
                )
            )
        )
    counts: dict[str, int] = {}
    for anchor in anchors:
        counts[anchor.kind] = counts.get(anchor.kind, 0) + 1
    return counts


@pytest.mark.asyncio
async def test_a_second_lock_of_the_same_bible_creates_nothing_new(openrouter_container):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "twice-lock@example.com")
        session_id, version = await _ready_to_lock(container, client, headers, project_id)
        first = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": version},
        )
        assert first.status_code == 200, first.text
        before = _canon_counts(container, project_id)
        again = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": version},
        )
        assert again.status_code == 200
    assert _canon_counts(container, project_id) == before


@pytest.mark.asyncio
async def test_a_style_lock_from_another_session_is_inherited_on_record_not_claimed(
    openrouter_container,
):
    """Rule 4: an existing lock that is not this bible's plate is not silently adopted."""

    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "inherit@example.com")
        first_session, first_version = await _ready_to_lock(container, client, headers, project_id)
        assert (
            client.post(
                f"/v1/creative/sessions/{first_session}/bible/approve",
                headers=headers,
                json={"version": first_version},
            ).status_code
            == 200
        )
        second_session, second_version = await _ready_to_lock(container, client, headers, project_id)
        locked = client.post(
            f"/v1/creative/sessions/{second_session}/bible/approve",
            headers=headers,
            json={"version": second_version},
        )
        assert locked.status_code == 200, locked.text
        lineage = locked.json()["lineage"]

    assert lineage["style_inherited"] is True
    assert lineage["style_matches_this_bible"] is False
    assert lineage["style_inherited_from_session_id"] == first_session
    with container.database.session() as session:
        locks = list(
            session.scalars(select(ProjectStyleLock).where(ProjectStyleLock.project_id == project_id))
        )
    assert len(locks) == 1 and locks[0].id == lineage["style_lock_id"]


@pytest.mark.asyncio
async def test_a_bible_superseded_while_locking_does_not_become_locked(openrouter_container):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    service = container.creative_director
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "superseded@example.com")
        session_id, version = await _ready_to_lock(container, client, headers, project_id)

        original = service._run_lock_step
        supersede_after_first: list[int] = []

        def racing(**kwargs):  # type: ignore[no-untyped-def]
            produced = original(**kwargs)
            if not supersede_after_first:
                supersede_after_first.append(1)
                with container.database.session() as session:
                    bible = session.scalar(
                        select(VisualBibleVersion).where(
                            VisualBibleVersion.session_id == session_id,
                            VisualBibleVersion.version == version,
                        )
                    )
                    bible.status = "SUPERSEDED"
            return produced

        service._run_lock_step = racing  # type: ignore[method-assign]
        refused = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": version},
        )
        service._run_lock_step = original  # type: ignore[method-assign]

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["reason_code"] == "REVISION_SUPERSEDED"
    with container.database.session() as session:
        bible = session.scalar(
            select(VisualBibleVersion).where(
                VisualBibleVersion.session_id == session_id, VisualBibleVersion.version == version
            )
        )
    assert bible.status == "SUPERSEDED"
    assert bible.lineage_json["lock_status"] == "SUPERSEDED_DURING_LOCK"


@pytest.mark.asyncio
async def test_a_concurrent_approval_is_refused_rather_than_duplicating_a_step(
    openrouter_container,
):
    """Two approvals must not run the same step at once.

    The step ledger gives exactly-once against *sequential* retries by itself.
    A second approval arriving while the first is inside a step - a double
    click, a second tab, a re-click after the slow lock appears to hang - would
    otherwise call confirm_identity twice for one face, because the row lock is
    released before the step's own work begins.
    """

    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    service = container.creative_director
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "concurrent@example.com")
        session_id, version = await _ready_to_lock(container, client, headers, project_id)

        original = service._run_lock_step
        claimed: list[dict] = []

        def capture(**kwargs):  # type: ignore[no-untyped-def]
            if kwargs["kind"] == "CHARACTER_IDENTITY" and not claimed:
                claimed.append(kwargs)
            return original(**kwargs)

        service._run_lock_step = capture  # type: ignore[method-assign]
        locked = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": version},
        )
        service._run_lock_step = original  # type: ignore[method-assign]
        assert locked.status_code == 200, locked.text
        assert claimed, "the identity step never ran"

    # The interleaving, constructed directly: a second approval enters the
    # identical step while the first still holds it.
    with container.database.session() as session:
        row = session.scalar(
            select(CreativeLockStep).where(
                CreativeLockStep.idempotency_key == claimed[0]["idempotency_key"]
            )
        )
        row.status = "RUNNING"
        row.claimed_at = utcnow()
    with pytest.raises(CreativeSessionConflict) as raised:
        service._run_lock_step(**claimed[0])
    assert raised.value.as_detail()["reason_code"] == "LOCK_IN_PROGRESS"
    assert raised.value.as_detail()["retryable"] is True

    # A claim older than the lease is taken over rather than wedging the bible.
    with container.database.session() as session:
        row = session.scalar(
            select(CreativeLockStep).where(
                CreativeLockStep.idempotency_key == claimed[0]["idempotency_key"]
            )
        )
        row.claimed_at = utcnow() - timedelta(hours=2)
    produced = service._run_lock_step(**claimed[0])
    assert produced["identity_version_id"]
    with container.database.session() as session:
        identities = session.execute(
            select(CharacterIdentityVersion.character_id, func.count(CharacterIdentityVersion.id))
            .group_by(CharacterIdentityVersion.character_id)
        ).all()
    # Taking over a dead claim recovers; it never mints a second identity.
    assert all(count == 1 for _character, count in identities), identities


@pytest.mark.asyncio
async def test_a_style_lock_that_fails_after_its_asset_version_recovers_it(openrouter_container):
    """The style step writes the asset version and the promotion before the lock."""

    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    service = container.creative_director
    real_lock = service.styles.lock
    calls: list[int] = []

    def failing_lock(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) == 1:
            raise _Boom("the semantic style model is unreachable")
        return real_lock(*args, **kwargs)

    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "style-half@example.com")
        session_id, version = await _ready_to_lock(container, client, headers, project_id)
        service.styles.lock = failing_lock  # type: ignore[method-assign]
        failed = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": version},
        )
        assert failed.status_code == 409, failed.text
        service.styles.lock = real_lock  # type: ignore[method-assign]
        locked = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": version},
        )
        assert locked.status_code == 200, locked.text

    with container.database.session() as session:
        per_asset_media = session.execute(
            select(
                AssetVersion.asset_id,
                AssetVersion.primary_media_asset_id,
                func.count(AssetVersion.id),
            ).group_by(AssetVersion.asset_id, AssetVersion.primary_media_asset_id)
        ).all()
        promotions = int(session.scalar(select(func.count(AssetCanonicalPromotion.id))) or 0)
        versions = int(session.scalar(select(func.count(AssetVersion.id))) or 0)
        locks = int(
            session.scalar(
                select(func.count(ProjectStyleLock.id)).where(
                    ProjectStyleLock.project_id == project_id
                )
            )
            or 0
        )
    # One style asset version, one promotion for it, one lock - the retry
    # finished the half that was missing instead of appending another.
    assert all(count == 1 for _asset, _media, count in per_asset_media), per_asset_media
    assert promotions == versions
    assert locks == 1
