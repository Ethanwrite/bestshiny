"""The 2026-09-07 production review's server-side findings, pinned.

1. ``GET /v1/generations`` pages older creations by a keyset cursor
   (``before`` / ``next_cursor``) instead of stopping at the newest hundred;
2. the job view carries the length that runs beside the length asked for,
   and ``allowed_actions`` decided by the gateway's own retry/cancel rule -
   the same rule the admin console reads, so neither surface offers a
   command the gateway answers with 409;
3. the admin audit log can be read by its own id;
4. ``GET /v1/payments/catalog`` is public and is the checkout's catalogue,
   and the public page's fallback rows are those same rows;
5. an asset with no ``public_url`` (a direct upload) gets a read address
   minted from its storage key;
6. ``POST /api/passenger/generate`` answers with the execution length.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from generation_gateway import job_allowed_actions
from payment_core import PAYMENT_PACKAGES, XUNHUPAY_PACKAGES
from production_domain.models import AdminAuditLog, GenerationJob, MediaAsset, PlatformRole, User
from sqlalchemy import select
from video_platform_api.main import create_app

PUBLIC_JS = (Path(__file__).resolve().parents[1] / "apps" / "web" / "public.js").read_text(encoding="utf-8")


def _register(client: TestClient, email: str) -> tuple[dict, dict, str]:
    registered = client.post(
        "/api/auth/register",
        json={"email": email, "password": "correct horse battery staple"},
    ).json()
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    project = client.post("/v1/projects", headers=headers, json={"title": "Review"}).json()
    return registered, headers, project["id"]


def _seed_job(container, project_id: str, index: int, **columns) -> str:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    defaults = dict(
        id=f"job-{index}",
        project_id=project_id,
        generation_type="video",
        provider="seedance",
        model="doubao-seedance-2-5-260628",
        status="COMPLETED",
        request_json={},
        request_hash=f"{index:064x}",
        cost_estimate=0.12,
        quoted_credits=12,
        created_at=now + timedelta(seconds=index),
    )
    defaults.update(columns)
    with container.database.session() as session:
        session.add(GenerationJob(**defaults))
    return str(defaults["id"])


# ------------------------------------------------------------------
# 1. The listing pages by cursor.
# ------------------------------------------------------------------
def test_generation_listing_pages_older_creations_by_cursor(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        _, headers, project_id = _register(client, "pages@example.com")
        for index in range(5):
            _seed_job(container, project_id, index)
        _, stranger_headers, stranger_project = _register(client, "pages-stranger@example.com")
        _seed_job(container, stranger_project, 9)

        first = client.get(f"/v1/generations?project_id={project_id}&limit=2", headers=headers)
        assert first.status_code == 200, first.text
        assert [job["id"] for job in first.json()["jobs"]] == ["job-4", "job-3"]
        assert first.json()["has_more"] is True
        assert first.json()["next_cursor"] == "job-3"

        second = client.get(
            f"/v1/generations?project_id={project_id}&limit=2&before=job-3", headers=headers
        )
        assert [job["id"] for job in second.json()["jobs"]] == ["job-2", "job-1"]
        assert second.json()["next_cursor"] == "job-1"

        last = client.get(
            f"/v1/generations?project_id={project_id}&limit=2&before=job-1", headers=headers
        )
        assert [job["id"] for job in last.json()["jobs"]] == ["job-0"]
        assert last.json()["has_more"] is False
        assert last.json()["next_cursor"] is None

        unknown = client.get(
            f"/v1/generations?project_id={project_id}&before=no-such-job", headers=headers
        )
        # A cursor from another project names nothing in this one.
        foreign = client.get(f"/v1/generations?project_id={project_id}&before=job-9", headers=headers)
        # Nor can a stranger walk my project with my cursor.
        stranger = client.get(
            f"/v1/generations?project_id={project_id}&before=job-3", headers=stranger_headers
        )
    assert unknown.status_code == 404
    assert foreign.status_code == 404
    assert stranger.status_code == 403


# ------------------------------------------------------------------
# 2. The job view: both lengths, and the gateway's own actions.
# ------------------------------------------------------------------
def test_job_view_carries_the_length_that_runs_and_the_length_asked_for(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        _, headers, project_id = _register(client, "lengths@example.com")
        _seed_job(
            container,
            project_id,
            0,
            request_json={
                "duration": 6.0,
                "aspect_ratio": "16:9",
                "metadata": {
                    "resolution": "720p",
                    "requested_duration_seconds": 5.0,
                    "execution_duration_seconds": 6.0,
                    "execution_strategy": "SNAP_UP",
                },
            },
        )
        detail = client.get("/v1/generations/job-0", headers=headers)
        listed = client.get(f"/v1/generations?project_id={project_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    job = detail.json()
    assert job["duration"] == 6.0
    assert job["requested_duration"] == 5.0
    assert job["aspect_ratio"] == "16:9"
    assert job["resolution"] == "720p"
    row = listed.json()["jobs"][0]
    assert (row["duration"], row["requested_duration"]) == (6.0, 5.0)


def test_job_allowed_actions_is_the_gateway_rule() -> None:
    def job(**columns):  # type: ignore[no-untyped-def]
        base = dict(
            status="RETRY_WAIT",
            safe_to_retry=True,
            submission_state="NOT_SENT",
            provider_job_id=None,
            output_asset_id=None,
        )
        base.update(columns)
        return SimpleNamespace(**base)

    # A failure before submission keeps safe_to_retry, and is still terminal.
    assert job_allowed_actions(job(status="FAILED"), credit_status="REFUNDED") == {
        "retry": False,
        "cancel": False,
        "resubmit": True,
    }
    nothing = {"retry": False, "cancel": False}
    assert job_allowed_actions(job(status="CANCELLED")) == {**nothing, "resubmit": True}
    assert job_allowed_actions(job(status="COMPLETED")) == {**nothing, "resubmit": False}
    # What retry() will accept: never sent, still safe, credits still held.
    assert job_allowed_actions(job(), credit_status="RESERVED") == {
        "retry": True,
        "cancel": True,
        "resubmit": False,
    }
    assert job_allowed_actions(job())["retry"] is True
    assert job_allowed_actions(job(), credit_status="SETTLED")["retry"] is False
    assert job_allowed_actions(job(safe_to_retry=False))["retry"] is False
    running = job(provider_job_id="prov-1", submission_state="CONFIRMED", status="RUNNING")
    assert job_allowed_actions(running) == {
        "retry": False,
        "cancel": True,
        "resubmit": False,
    }
    # An unconfirmed submission must be reconciled before anything else.
    unconfirmed = job_allowed_actions(job(status="SUBMITTED", submission_state="SENT_UNCONFIRMED"))
    assert unconfirmed == {"retry": False, "cancel": False, "resubmit": False}


def test_a_failed_creation_offers_no_retry_on_either_surface_and_the_gateway_agrees(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        registered, headers, project_id = _register(client, "retry-rule@example.com")
        _seed_job(container, project_id, 0, status="FAILED", safe_to_retry=True, error_code="PROVIDER_DOWN")
        _seed_job(container, project_id, 1, status="RETRY_WAIT", safe_to_retry=True)
        with container.database.session() as session:
            user = session.get(User, registered["user"]["id"])
            assert user is not None
            user.platform_role = PlatformRole.ADMIN.value

        failed = client.get("/v1/generations/job-0", headers=headers).json()
        waiting = client.get("/v1/generations/job-1", headers=headers).json()
        admin_failed = client.get("/api/admin/jobs/job-0", headers=headers)
        admin_waiting = client.get("/api/admin/jobs/job-1", headers=headers)
        refused = client.post("/v1/generations/job-0/retry", headers=headers)
        admin_refused = client.post(
            "/api/admin/jobs/job-0/retry", headers=headers, json={"reason": "operator retry attempt"}
        )
    assert failed["allowed_actions"] == {"retry": False, "cancel": False, "resubmit": True}
    assert waiting["allowed_actions"]["retry"] is True and waiting["allowed_actions"]["cancel"] is True
    assert admin_failed.status_code == 200, admin_failed.text
    assert admin_failed.json()["allowed_actions"] == failed["allowed_actions"]
    assert admin_waiting.json()["allowed_actions"] == waiting["allowed_actions"]
    # What the surfaces stopped offering is exactly what the gateway refuses.
    assert refused.status_code == 409, refused.text
    assert admin_refused.status_code == 409, admin_refused.text


# ------------------------------------------------------------------
# 3. An audit entry by its own id.
# ------------------------------------------------------------------
def test_audit_log_can_be_read_by_its_own_id(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        registered, headers, _ = _register(client, "audit-id@example.com")
        with container.database.session() as session:
            user = session.get(User, registered["user"]["id"])
            assert user is not None
            user.platform_role = PlatformRole.SUPER_ADMIN.value
            for action in ("USER_SUSPENDED", "USER_UNSUSPENDED"):
                session.add(
                    AdminAuditLog(
                        actor_user_id=user.id,
                        actor_role="SUPER_ADMIN",
                        action=action,
                        entity_type="USER",
                        entity_id="user-1",
                        request_id=f"req-{action}",
                    )
                )
        with container.database.session() as session:
            wanted = session.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "USER_UNSUSPENDED"))
            assert wanted is not None
            wanted_id = wanted.id
        by_id = client.get(f"/api/admin/audit?id={wanted_id}", headers=headers)
        by_entity = client.get("/api/admin/audit?entity_id=user-1", headers=headers)
        # The console's old query: the log id as an entity id finds nothing.
        misfiled = client.get(f"/api/admin/audit?entity_id={wanted_id}", headers=headers)
    assert by_id.status_code == 200, by_id.text
    assert [item["id"] for item in by_id.json()["items"]] == [wanted_id]
    assert by_id.json()["items"][0]["action"] == "USER_UNSUSPENDED"
    assert len(by_entity.json()["items"]) == 2
    assert by_id.json()["pagination"]["total"] == 1
    assert misfiled.json()["items"] == []


# ------------------------------------------------------------------
# 4. The public pricing page reads the checkout's catalogue.
# ------------------------------------------------------------------
def _js_packs(name: str) -> list[dict[str, object]]:
    block = PUBLIC_JS[PUBLIC_JS.index(f"const {name} = [") :]
    block = block[: block.index("];")]
    rows = []
    for row in re.finditer(r"\{([^}]*)\}", block):
        fields = {key: value.strip() for key, value in re.findall(r'(\w+): ("?[^,"]+"?)', row.group(1))}
        rows.append(
            {
                "sku": fields["sku"].strip('"'),
                "amount": fields["amount"].strip('"'),
                "currency": fields["currency"].strip('"'),
                "credits": int(fields["credits"]),
                "recommended": fields["recommended"] == "true",
            }
        )
    return rows


def test_the_pack_catalogue_is_public_and_the_page_fallback_matches_it(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        catalog = client.get("/v1/payments/catalog")
        config = client.get("/v1/payments/config")
    assert config.status_code == 401, "the configuration stays behind sign-in"
    assert catalog.status_code == 200, catalog.text
    body = catalog.json()
    expected = [package.as_public_dict() for package in PAYMENT_PACKAGES.values()]
    expected_cny = [package.as_public_dict() for package in XUNHUPAY_PACKAGES.values()]
    assert body["packages"] == expected
    assert body["xunhupay_packages"] == expected_cny
    assert body["usd_per_credit"] == container.credit_pricing.usd_per_credit
    # The page's fallback rows are the catalogue's rows, so it cannot drift
    # back to advertising a pack the checkout does not sell.
    assert _js_packs("STATIC_PACKS") == expected
    assert _js_packs("STATIC_CNY_PACKS") == expected_cny
    for stale in ("<strong>$30</strong>", "adds 3,000 credits", "Includes 3,000 credits"):
        assert stale not in PUBLIC_JS, stale


# ------------------------------------------------------------------
# 5. A direct upload's asset has a read address.
# ------------------------------------------------------------------
def test_an_asset_with_no_public_url_gets_one_minted_from_its_storage_key(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        _, headers, project_id = _register(client, "direct-preview@example.com")
        with container.database.session() as session:
            asset = MediaAsset(
                project_id=project_id,
                asset_type="REFERENCE",
                sha256="ab" * 32,
                lineage_key="shared",
                storage_key="uploads/ab/abab.png",
                local_path=None,
                public_url=None,
                mime_type="image/png",
                size_bytes=10,
                verification_status="READY",
                metadata_json={"source": "direct_upload"},
            )
            session.add(asset)
            session.flush()
            asset_id = asset.id
        detail = client.get(f"/v1/assets/{asset_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    # conftest's storage carries public_base_url=http://testserver, the same
    # base put() mints for an ordinary upload.
    assert detail.json()["public_url"] == "http://testserver/v1/storage/uploads/ab/abab.png"
    assert detail.json()["storage_key"] == "uploads/ab/abab.png"


# ------------------------------------------------------------------
# 6. The submit answer says what length runs.
# ------------------------------------------------------------------
def test_passenger_generate_answers_with_the_execution_length(container, project, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(container.providers.get("openrouter"), "configured", True, raising=False)
    captured: dict[str, object] = {}

    def submit(command, **_server_quote):  # type: ignore[no-untyped-def]
        captured["command"] = command
        return (
            SimpleNamespace(
                id="veo-5s-job",
                status="NEW",
                provider=command.provider,
                model=command.model,
                output_asset_id=None,
                submission_state="NOT_SENT",
                safe_to_retry=True,
                provider_job_id=None,
                cost_estimate=command.estimated_cost,
            ),
            False,
        )

    monkeypatch.setattr(container.visual_runtime, "submit_passenger", submit)
    with TestClient(create_app(container)) as client:
        response = client.post(
            "/api/passenger/generate",
            json={
                "project_id": project.id,
                "media_type": "video",
                "provider": "openrouter",
                "model": "google/veo-3.1",
                "prompt": "a lantern-lit alley after rain",
                "duration": 5,
                "aspect_ratio": "16:9",
                "idempotency_key": "veo-5s-answer",
            },
        )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["duration"] == 6.0, "Veo runs 4/6/8: a 5 s request runs 6 s"
    assert body["requested_duration"] == 5.0
    assert body["aspect_ratio"] == "16:9"
    assert body["resolution"] == "720p"
    assert body["allowed_actions"] == {"retry": True, "cancel": True, "resubmit": False}
    assert captured["command"].duration == 6.0  # type: ignore[attr-defined]


@pytest.mark.parametrize("path", ["/v1/payments/catalog"])
def test_public_routes_need_no_session(container, path):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        assert client.get(path).status_code == 200
