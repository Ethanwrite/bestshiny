"""Passenger Seat model selection: image is routed, video may be named.

The contract under test:

* **image** — the caller states a creative task and never a model. The router
  resolves the target before the quote and before the credit reservation, so the
  figure shown can only ever belong to the model that runs. A named image model
  is refused outright.
* **video** — Auto (an empty provider/model pair) lets the router choose. Naming
  a model uses exactly that model for pricing, for the stored job, for what
  reaches the provider and for what is billed, or fails explicitly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cost_core import CreditEstimate
from fastapi.testclient import TestClient
from production_domain.models import GenerationJob, Project, Workspace
from sqlalchemy import select
from video_platform_api import create_app


@pytest.fixture
def paid_client(container, monkeypatch):  # type: ignore[no-untyped-def]
    """A PRO workspace with two usable image models and one usable video model."""

    container.settings.auth_required = True
    # google_flow backs the PRO default video role, so Auto needs it configured.
    for provider in ("openrouter", "seedance", "google_flow"):
        # raising=False: adapters differ in whether they carry the flag natively.
        monkeypatch.setattr(container.providers.get(provider), "configured", True, raising=False)
    for provider, model, modality in (
        ("openrouter", "openai/gpt-image-2", "image"),
        ("seedance", "seedream-5-0", "image"),
        ("seedance", "doubao-seedance-2-5-260628", "video"),
    ):
        try:
            container.providers.register_model(provider, model, modality)
        except ValueError:
            pass  # already registered by the default catalogue

    with TestClient(create_app(container)) as client:
        registered = client.post(
            "/api/auth/register",
            json={"email": "qa018@example.com", "password": "correct horse battery staple"},
        ).json()
        headers = {"Authorization": f"Bearer {registered['access_token']}"}
        project = client.post("/v1/projects", headers=headers, json={"title": "QA018"}).json()
        with container.database.session() as session:
            stored = session.get(Project, project["id"])
            assert stored is not None
            workspace = session.get(Workspace, stored.workspace_id)
            assert workspace is not None
            workspace.plan_tier = "PRO"
            workspace.credit_balance = 5_000
        yield client, headers, project["id"]


def _capture_submission(container, monkeypatch):  # type: ignore[no-untyped-def]
    """Record the command that would reach the provider, without submitting."""

    seen: dict[str, object] = {}

    def submit(command, **_quote):  # type: ignore[no-untyped-def]
        seen["command"] = command
        return (
            SimpleNamespace(
                id="qa018-job",
                status="NEW",
                provider=command.provider,
                model=command.model,
                output_asset_id=None,
                cost_estimate=command.estimated_cost,
            ),
            False,
        )

    monkeypatch.setattr(container.visual_runtime, "submit_passenger", submit)
    return seen


def test_image_is_routed_and_quoted_for_the_model_that_runs(paid_client, container, monkeypatch):  # type: ignore[no-untyped-def]
    """The router resolves first, so the quote can only describe the real target."""

    client, headers, project_id = paid_client
    seen = _capture_submission(container, monkeypatch)

    priced: dict[str, object] = {}
    original = container.credit_pricing.estimate

    def estimate(**kwargs):  # type: ignore[no-untyped-def]
        priced.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(container.credit_pricing, "estimate", estimate)

    response = client.post(
        "/api/passenger/generate",
        headers=headers,
        json={
            "project_id": project_id,
            "media_type": "image",
            "image_task": "product",
            "prompt": "A product hero on dark grey",
            "idempotency_key": "img-routed",
        },
    )

    assert response.status_code == 202, response.text
    body = response.json()
    command = seen["command"]

    # One model throughout: quoted, submitted and reported.
    assert priced["provider"] == command.provider  # type: ignore[attr-defined]
    assert priced["model"] == command.model  # type: ignore[attr-defined]
    assert body["provider"] == command.provider  # type: ignore[attr-defined]
    assert body["model"] == command.model  # type: ignore[attr-defined]
    # Routed, and attributed to the role that routed it.
    assert command.model_role == "IMAGE_GENERATION"  # type: ignore[attr-defined]
    assert command.image_task == "product"  # type: ignore[attr-defined]


def test_job_metadata_distinguishes_routed_from_manual(paid_client, container, monkeypatch):  # type: ignore[no-untyped-def]
    """The stored job says how its model was chosen, for both paths."""

    client, headers, project_id = paid_client
    seen = _capture_submission(container, monkeypatch)

    client.post(
        "/api/passenger/generate",
        headers=headers,
        json={
            "project_id": project_id,
            "media_type": "image",
            "image_task": "commercial",
            "prompt": "Routed",
            "idempotency_key": "meta-routed",
        },
    )
    assert seen["command"].model_role == "IMAGE_GENERATION"  # type: ignore[attr-defined]

    client.post(
        "/api/passenger/generate",
        headers=headers,
        json={
            "project_id": project_id,
            "media_type": "video",
            "provider": "seedance",
            "model": "doubao-seedance-2-5-260628",
            "prompt": "Manual",
            "idempotency_key": "meta-manual",
        },
    )
    # Admission clears the role when it obeyed a named model.
    assert seen["command"].model_role is None  # type: ignore[attr-defined]


def test_named_video_model_is_the_model_billed(paid_client, container, monkeypatch):  # type: ignore[no-untyped-def]
    """The credits charged must be the named model's price, not another model's."""

    client, headers, project_id = paid_client
    _capture_submission(container, monkeypatch)

    quotes = {
        ("seedance", "doubao-seedance-2-5-260628"): 4,
        ("openrouter", "kwaivgi/kling-v3.0-pro"): 40,
    }

    def estimate(*, provider, model, **_kwargs):  # type: ignore[no-untyped-def]
        credits = quotes[(provider, model)]
        return CreditEstimate(
            provider_cost_usd=credits / 100,
            resolution_multiplier=1.0,
            reference_multiplier=1.0,
            service_multiplier=1.0,
            estimated_total_usd=credits / 100,
            credits=credits,
            usd_per_credit=0.01,
        )

    monkeypatch.setattr(container.credit_pricing, "estimate", estimate)

    response = client.post(
        "/api/passenger/generate",
        headers=headers,
        json={
            "project_id": project_id,
            "media_type": "video",
            "provider": "seedance",
            "model": "doubao-seedance-2-5-260628",
            "prompt": "Cheap model must be charged at the cheap price",
            "idempotency_key": "qa018-billing",
        },
    )

    assert response.status_code == 202, response.text
    # 4, the named model's quote — not 40, the one the router would have picked.
    assert response.json()["estimated_credits"] == 4


def test_video_auto_lets_the_router_choose(paid_client, container, monkeypatch):  # type: ignore[no-untyped-def]
    client, headers, project_id = paid_client
    seen = _capture_submission(container, monkeypatch)

    response = client.post(
        "/api/passenger/generate",
        headers=headers,
        json={
            "project_id": project_id,
            "media_type": "video",
            # No provider/model: the router owns the choice. The role pins it to a
            # provider this fixture has configured, so the test is about routing
            # rather than about which adapter happens to have credentials.
            "model_role": "VIDEO_SEEDANCE",
            "prompt": "No model named, so the platform chooses",
            "idempotency_key": "video-auto",
        },
    )

    assert response.status_code == 202, response.text
    command = seen["command"]
    assert command.provider  # type: ignore[attr-defined]
    assert command.model  # type: ignore[attr-defined]
    # Routed selections are attributed to the role that made them.
    assert str(command.model_role).startswith("VIDEO_")  # type: ignore[attr-defined]


def test_named_image_model_is_refused(paid_client, container, monkeypatch):  # type: ignore[no-untyped-def]
    """Image targets are router-owned; naming one is a contract error, not a hint."""

    client, headers, project_id = paid_client
    seen = _capture_submission(container, monkeypatch)

    response = client.post(
        "/api/passenger/generate",
        headers=headers,
        json={
            "project_id": project_id,
            "media_type": "image",
            "provider": "seedance",
            "model": "seedream-5-0",
            "prompt": "Naming an image model must be refused",
            "idempotency_key": "img-named",
        },
    )

    assert response.status_code == 400, response.text
    assert "does not accept a named model" in response.text
    assert "command" not in seen


def test_image_task_defaults_to_auto_and_is_recorded(paid_client, container, monkeypatch):  # type: ignore[no-untyped-def]
    client, headers, project_id = paid_client
    seen = _capture_submission(container, monkeypatch)

    response = client.post(
        "/api/passenger/generate",
        headers=headers,
        json={
            "project_id": project_id,
            "media_type": "image",
            "prompt": "No task given",
            "idempotency_key": "img-default-task",
        },
    )

    assert response.status_code == 202, response.text
    assert seen["command"].image_task == "auto"  # type: ignore[attr-defined]


def test_unknown_named_model_is_refused_not_replaced(paid_client, container, monkeypatch):  # type: ignore[no-untyped-def]
    client, headers, project_id = paid_client
    seen = _capture_submission(container, monkeypatch)

    response = client.post(
        "/api/passenger/generate",
        headers=headers,
        json={
            "project_id": project_id,
            "media_type": "video",
            "provider": "openrouter",
            "model": "no-such-model-v9",
            "prompt": "This must fail loudly",
            "idempotency_key": "qa018-unknown",
        },
    )

    assert response.status_code == 400, response.text
    assert "not a known model" in response.text
    assert "command" not in seen
    with container.database.session() as session:
        assert session.scalar(select(GenerationJob.id)) is None


def test_named_video_model_of_the_wrong_modality_is_refused(paid_client, container, monkeypatch):  # type: ignore[no-untyped-def]
    client, headers, project_id = paid_client
    seen = _capture_submission(container, monkeypatch)

    response = client.post(
        "/api/passenger/generate",
        headers=headers,
        json={
            "project_id": project_id,
            "media_type": "video",
            "provider": "seedance",
            "model": "seedream-5-0",
            "prompt": "An image model cannot serve a video request",
            "idempotency_key": "qa018-modality",
        },
    )

    assert response.status_code == 400, response.text
    assert "cannot serve" in response.text
    assert "command" not in seen


def test_named_video_model_on_a_disabled_provider_is_refused(paid_client, container, monkeypatch):  # type: ignore[no-untyped-def]
    """The provider kill switch refuses a named model instead of routing past it."""

    client, headers, project_id = paid_client
    seen = _capture_submission(container, monkeypatch)
    monkeypatch.setattr(container.model_registry, "provider_enabled", lambda provider: False)

    response = client.post(
        "/api/passenger/generate",
        headers=headers,
        json={
            "project_id": project_id,
            "media_type": "video",
            "provider": "seedance",
            "model": "doubao-seedance-2-5-260628",
            "prompt": "Disabled provider must refuse",
            "idempotency_key": "qa018-disabled",
        },
    )

    assert response.status_code == 400, response.text
    assert "disabled by platform operations" in response.text
    assert "command" not in seen


def test_half_named_selection_is_rejected(paid_client, container, monkeypatch):  # type: ignore[no-untyped-def]
    client, headers, project_id = paid_client
    _capture_submission(container, monkeypatch)

    response = client.post(
        "/api/passenger/generate",
        headers=headers,
        json={
            "project_id": project_id,
            "media_type": "video",
            "provider": "seedance",
            "prompt": "Provider without a model is ambiguous",
            "idempotency_key": "qa018-half",
        },
    )

    assert response.status_code == 400, response.text
    assert "must be given together" in response.text


def test_free_plan_refuses_a_named_paid_video_model(container, monkeypatch):  # type: ignore[no-untyped-def]
    """FREE denies a paid model outright; it no longer swaps in Seedance."""

    container.settings.auth_required = True
    monkeypatch.setattr(container.providers.get("openrouter"), "configured", True)
    monkeypatch.setattr(container.providers.get("seedance"), "configured", True)
    container.providers.register_model("openrouter", "kwaivgi/kling-v3.0-pro", "video")
    container.providers.register_model("seedance", "doubao-seedance-2-5-260628", "video")
    seen = _capture_submission(container, monkeypatch)

    with TestClient(create_app(container)) as client:
        registered = client.post(
            "/api/auth/register",
            json={"email": "qa018-free@example.com", "password": "correct horse battery staple"},
        ).json()
        headers = {"Authorization": f"Bearer {registered['access_token']}"}
        project = client.post("/v1/projects", headers=headers, json={"title": "QA018 Free"}).json()
        response = client.post(
            "/api/passenger/generate",
            headers=headers,
            json={
                "project_id": project["id"],
                "media_type": "video",
                "provider": "openrouter",
                "model": "kwaivgi/kling-v3.0-pro",
                "prompt": "A paid model named on a free plan",
                "idempotency_key": "qa018-free-named",
            },
        )

    assert response.status_code == 403, response.text
    assert "requires a paid plan" in response.text
    assert "command" not in seen
