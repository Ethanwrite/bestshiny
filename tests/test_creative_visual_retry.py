"""Retry after an asynchronous key-visual failure actually retries.

The provider accepted the job, so the CreativeAction was EXECUTED and the
anchor GENERATING. When the job later failed, `sync_visuals` moved only the
anchor to FAILED: the action stayed EXECUTED, `pending_actions` never returned
it again, and the Retry button posted to an endpoint that found nothing to do
and reported nothing. Behind that sat a second defect - the retry reused the
same generation idempotency key, so even a fixed retry would have replayed the
dead job instead of buying a new one.
"""

from __future__ import annotations

import pytest
from production_domain.models import (
    CreativeAction,
    CreativeVisualAnchor,
    GenerationJob,
    JobStatus,
)
from sqlalchemy import select
from test_creative_director import (
    RICH_IDEA,
    ScriptedDirector,
    _approve_brief,
    _approve_screenplay,
    _client,
    _registered_pro,
    _rich_turn,
    _state,
    _wire_openrouter_images,
)
from test_creative_director import (
    openrouter_container as openrouter_container,  # re-exported so the fixture resolves here
)


def _fail_job(container, job_id: str, *, status: str = JobStatus.FAILED.value) -> None:
    """The provider accepted it, then it failed - the real async shape."""

    with container.database.session() as session:
        job = session.get(GenerationJob, job_id)
        job.status = status
        job.error_code = "PROVIDER_TIMEOUT"
        job.error_message = "the provider stopped responding after submission"


async def _session_with_visuals(container, client, headers, project_id, email_suffix=""):  # type: ignore[no-untyped-def]
    started = client.post(
        "/v1/creative/sessions", headers=headers, json={"project_id": project_id, "idea": RICH_IDEA}
    ).json()
    session_id = started["session_id"]
    _approve_brief(client, session_id, started["brief_revision"], headers)
    _approve_screenplay(client, session_id, headers)
    return session_id


@pytest.mark.asyncio
async def test_an_async_failure_reopens_its_action_and_the_retry_buys_a_new_job(
    openrouter_container,
):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "retry@example.com")
        session_id = await _session_with_visuals(container, client, headers, project_id)

        view = _state(client, session_id, headers)
        anchor = next(a for a in view["anchors"] if a["anchor_key"] == "character:mira")
        assert anchor["status"] == "GENERATING" and anchor["generation_job_id"]
        first_job_id = anchor["generation_job_id"]
        action = next(
            item
            for item in view["actions"]
            if item["kind"] == "GENERATE_KEY_VISUAL" and item["payload"]["anchor_id"] == anchor["id"]
        )
        assert action["status"] == "EXECUTED"

        # The provider accepted it; it fails afterwards.
        _fail_job(container, first_job_id)

        synced = client.post(f"/v1/creative/sessions/{session_id}/visuals/sync", headers=headers)
        assert synced.status_code == 200, synced.text
        after_sync = _state(client, session_id, headers)
        failed_anchor = next(a for a in after_sync["anchors"] if a["id"] == anchor["id"])
        failed_action = next(item for item in after_sync["actions"] if item["id"] == action["id"])
        # Both moved, not just the anchor: this is what made Retry inert.
        assert failed_anchor["status"] == "FAILED"
        assert failed_action["status"] == "FAILED"
        assert failed_action["result"]["failed_asynchronously"] is True
        assert failed_action["result"]["job_id"] == first_job_id
        assert failed_action["result"]["error_message"].startswith("the provider stopped")

        with container.database.session() as session:
            jobs_before = len(list(session.scalars(select(GenerationJob))))

        retried = client.post(f"/v1/creative/sessions/{session_id}/visuals/execute", headers=headers)
        assert retried.status_code == 200, retried.text
        executions = retried.json()["executions"]
        assert [entry["status"] for entry in executions] == ["EXECUTED"], executions
        new_job_id = executions[0]["job_id"]

        # A new attempt, not the dead job replayed.
        assert new_job_id != first_job_id
        with container.database.session() as session:
            assert len(list(session.scalars(select(GenerationJob)))) == jobs_before + 1
            old_job = session.get(GenerationJob, first_job_id)
            assert old_job.status == JobStatus.FAILED.value  # history is kept
        after_retry = _state(client, session_id, headers)
        retried_anchor = next(a for a in after_retry["anchors"] if a["id"] == anchor["id"])
        assert retried_anchor["status"] == "GENERATING"
        assert retried_anchor["generation_job_id"] == new_job_id
        assert retried_anchor["failure_code"] is None
        # The same anchor version, and the attempt is on record for the UI.
        assert retried_anchor["version"] == anchor["version"]
        retried_action = next(item for item in after_retry["actions"] if item["id"] == action["id"])
        assert retried_action["result"]["attempt"] == 2
        assert retried_action["result"]["previous_job_id"] == first_job_id

        # Pressing Retry again does not create a third job or charge again.
        again = client.post(f"/v1/creative/sessions/{session_id}/visuals/execute", headers=headers)
        assert again.status_code == 200
        assert again.json()["executions"] == []
        with container.database.session() as session:
            assert len(list(session.scalars(select(GenerationJob)))) == jobs_before + 1

        # And it succeeds this time.
        completed = await container.gateway.process(new_job_id)
        assert completed.status == "COMPLETED"
        final = client.post(f"/v1/creative/sessions/{session_id}/visuals/sync", headers=headers)
        assert final.status_code == 200
        ready = next(a for a in final.json()["anchors"] if a["id"] == anchor["id"])
        assert ready["status"] == "READY" and ready["media_asset_id"]


@pytest.mark.asyncio
async def test_a_job_waiting_on_the_user_is_a_failure_to_retry_not_a_generation_in_flight(
    openrouter_container,
):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "needs-user@example.com")
        session_id = await _session_with_visuals(container, client, headers, project_id)
        view = _state(client, session_id, headers)
        anchor = next(a for a in view["anchors"] if a["anchor_key"] == "style:master")
        _fail_job(container, anchor["generation_job_id"], status="WORKER_NEEDS_USER_ACTION")
        synced = client.post(f"/v1/creative/sessions/{session_id}/visuals/sync", headers=headers)
        assert synced.status_code == 200

    stuck = next(a for a in synced.json()["anchors"] if a["id"] == anchor["id"])
    assert stuck["status"] == "FAILED"


@pytest.mark.asyncio
async def test_syncing_twice_reopens_the_action_once(openrouter_container):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "twice@example.com")
        session_id = await _session_with_visuals(container, client, headers, project_id)
        view = _state(client, session_id, headers)
        anchor = next(a for a in view["anchors"] if a["anchor_key"] == "character:ren")
        _fail_job(container, anchor["generation_job_id"])
        for _ in range(3):
            assert (
                client.post(
                    f"/v1/creative/sessions/{session_id}/visuals/sync", headers=headers
                ).status_code
                == 200
            )
        after = _state(client, session_id, headers)

    failed_actions = [
        item
        for item in after["actions"]
        if item["kind"] == "GENERATE_KEY_VISUAL"
        and item["payload"]["anchor_id"] == anchor["id"]
        and item["status"] == "FAILED"
    ]
    assert len(failed_actions) == 1
    with container.database.session() as session:
        rows = list(
            session.scalars(
                select(CreativeAction).where(
                    CreativeAction.session_id == session_id,
                    CreativeAction.kind == "GENERATE_KEY_VISUAL",
                )
            )
        )
        anchors = list(
            session.scalars(
                select(CreativeVisualAnchor).where(CreativeVisualAnchor.session_id == session_id)
            )
        )
    # Nothing was duplicated by re-syncing.
    assert len(rows) == len(anchors)
