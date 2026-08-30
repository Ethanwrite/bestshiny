"""The FREE plan's server-side hard gates.

Every gate here must hold against a hostile browser: the payload can name
models, invent tiers, replay keys or hammer endpoints, and the backend alone
decides. What is pinned:

1. FREE image generation routes through the FREE catalogue to the one bound
   model (`doubao-seedream-5-0-260128`) — no tier, no named model, no bypass;
2. the public image-quality tiers map server-side, and Pro tiers are denied
   to FREE workspaces rather than substituted;
3. the FREE image budget (3) is enforced at admission, counted from the jobs
   table, with idempotent replays of an admitted submission exempt;
4. the FREE director-dialogue budget is per session and enforced in the
   service, not the browser;
5. the FREE deep-prompt-optimization budget is a row-locked counter that a
   failed refine hands back.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from creative_director_core import CreativeTurnLimitReached
from fastapi.testclient import TestClient
from production_domain.models import (
    GenerationJob,
    Project,
    User,
    Workspace,
    WorkspaceUsageCounter,
)
from sqlalchemy import select
from video_platform_api.main import create_app


def _register(client: TestClient, email: str) -> tuple[dict, str]:
    registered = client.post(
        "/api/auth/register",
        json={"email": email, "password": "correct horse battery staple"},
    ).json()
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    project = client.post("/v1/projects", headers=headers, json={"title": "Gate"}).json()
    return headers, project["id"]


def _capture_submission(container, monkeypatch) -> dict:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}
    monkeypatch.setattr(container.providers.get("seedance"), "configured", True)

    def estimate(**_kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            provider_cost_usd=0.03,
            resolution_multiplier=1.0,
            reference_multiplier=1.0,
            service_multiplier=1.2,
            estimated_total_usd=0.036,
            credits=4,
        )

    def submit(command, **_server_quote):  # type: ignore[no-untyped-def]
        captured["command"] = command
        return (
            SimpleNamespace(
                id="free-image-job",
                status="NEW",
                provider=command.provider,
                model=command.model,
                output_asset_id=None,
                cost_estimate=command.estimated_cost,
            ),
            False,
        )

    monkeypatch.setattr(container.credit_pricing, "estimate", estimate)
    monkeypatch.setattr(container.visual_runtime, "submit_passenger", submit)
    return captured


def _image_payload(project_id: str, key: str, **extra) -> dict:  # type: ignore[no-untyped-def]
    return {
        "project_id": project_id,
        "media_type": "image",
        "prompt": "A rainy doorway at night",
        "idempotency_key": key,
        **extra,
    }


def test_free_image_generation_routes_to_the_free_bound_seedream(container, monkeypatch):
    container.settings.auth_required = True
    captured = _capture_submission(container, monkeypatch)
    with TestClient(create_app(container)) as client:
        headers, project_id = _register(client, "free-image@example.com")
        response = client.post(
            "/api/passenger/generate",
            headers=headers,
            json=_image_payload(project_id, "free-image-1"),
        )
    assert response.status_code == 202, response.text
    command = captured["command"]
    assert command.provider == "seedance"  # type: ignore[attr-defined]
    assert command.model == "doubao-seedream-5-0-260128"  # type: ignore[attr-defined]


def test_image_tiers_map_server_side_and_pro_tiers_deny_free(container, monkeypatch):
    container.settings.auth_required = True
    captured = _capture_submission(container, monkeypatch)
    with TestClient(create_app(container)) as client:
        headers, project_id = _register(client, "free-tiers@example.com")

        shiny = client.post(
            "/api/passenger/generate",
            headers=headers,
            json=_image_payload(project_id, "tier-shiny", image_tier="shiny"),
        )
        assert shiny.status_code == 202, shiny.text
        assert captured["command"].model == "doubao-seedream-5-0-260128"  # type: ignore[attr-defined]

        locked = client.post(
            "/api/passenger/generate",
            headers=headers,
            json=_image_payload(project_id, "tier-locked", image_tier="shiniest"),
        )
        assert locked.status_code == 403
        assert "Pro" in locked.json()["detail"]

        unknown = client.post(
            "/api/passenger/generate",
            headers=headers,
            json=_image_payload(project_id, "tier-unknown", image_tier="ultrashiny"),
        )
        assert unknown.status_code == 400

        # Upgrading the workspace unlocks the tier, still mapped server-side.
        # (The test container has no OpenRouter credential, which correctly
        # leaves the model disabled — enable it to exercise the mapping.)
        container.model_infrastructure.configure_runtime_model(
            "gpt-image-2-openrouter", "openai/gpt-image-2", enabled=True
        )
        with container.database.session() as session:
            workspace = session.scalar(
                select(Workspace)
                .join(Project, Project.workspace_id == Workspace.id)
                .where(Project.id == project_id)
            )
            workspace.plan_tier = "PRO"
        pro = client.post(
            "/api/passenger/generate",
            headers=headers,
            json=_image_payload(project_id, "tier-pro", image_tier="shiniest"),
        )
        assert pro.status_code == 202, pro.text
        assert captured["command"].provider == "openrouter"  # type: ignore[attr-defined]
        assert captured["command"].model == "openai/gpt-image-2"  # type: ignore[attr-defined]


def test_free_image_budget_is_counted_from_jobs_and_replays_are_exempt(container, monkeypatch):
    container.settings.auth_required = True
    _capture_submission(container, monkeypatch)
    with TestClient(create_app(container)) as client:
        headers, project_id = _register(client, "free-quota@example.com")

        # Two images per request never passes on FREE. The passenger payload
        # cannot even express a count, so the gate is exercised at admission —
        # the path the creative director's key-visual actions go through.
        from entitlement_core import PlanEntitlementDenied
        from platform_contracts import GenerationRequest

        with pytest.raises(PlanEntitlementDenied, match="one image per request"):
            container.generation_admission.admit_passenger(
                GenerationRequest(
                    project_id=project_id,
                    type="image",
                    prompt="two at once",
                    image_count=2,
                    idempotency_key="quota-many",
                )
            )

        with container.database.session() as session:
            for index in range(container.settings.free_plan_max_images):
                session.add(
                    GenerationJob(
                        project_id=project_id,
                        generation_type="image",
                        provider="seedance",
                        model="doubao-seedream-5-0-260128",
                        status="COMPLETED",
                        request_json={},
                        request_hash=f"{index:064x}",
                    )
                )

        over = client.post(
            "/api/passenger/generate",
            headers=headers,
            json=_image_payload(project_id, "quota-over"),
        )
        assert over.status_code == 403
        assert "Free plan" in over.json()["detail"]

        # A video request is not an image and still admits.
        monkeypatch.setattr(container.providers.get("seedance"), "configured", True)
        video = client.post(
            "/api/passenger/generate",
            headers=headers,
            json={
                "project_id": project_id,
                "media_type": "video",
                "prompt": "One visible action",
                "idempotency_key": "quota-video",
            },
        )
        assert video.status_code == 202, video.text

        # A replay of an already-admitted submission is not new spend.
        from production_domain.models import GenerationIdempotency

        with container.database.session() as session:
            job = session.scalars(select(GenerationJob)).first()
            session.add(
                GenerationIdempotency(
                    project_id=project_id,
                    key="quota-replay",
                    request_hash="0" * 64,
                    generation_job_id=job.id,
                )
            )
        replay = client.post(
            "/api/passenger/generate",
            headers=headers,
            json=_image_payload(project_id, "quota-replay"),
        )
        assert replay.status_code == 202, replay.text


@pytest.mark.asyncio
async def test_free_director_dialogue_budget_is_per_session(container):
    with container.database.session() as session:
        user = User(email="free-director@example.com", display_name="Free Director")
        session.add(user)
        session.flush()
        workspace = Workspace(
            owner_user_id=user.id, name="Free", status="ACTIVE", plan_tier="FREE"
        )
        session.add(workspace)
        session.flush()
        project = Project(workspace_id=workspace.id, title="Budget", status="ACTIVE")
        session.add(project)
        session.flush()
        project_id, workspace_id = project.id, workspace.id

    creative = container.creative_director
    creative.free_plan_turn_limit = 2
    reply = await creative.start_session(
        project_id, idea="帮我做一个短剧", workspace_id=workspace_id
    )
    await creative.post_message(reply.session_id, "主角是雨桐，在天台")
    with pytest.raises(CreativeTurnLimitReached, match="Free plan"):
        await creative.post_message(reply.session_id, "再长一点")

    # A second session gets its own budget: the limit is per conversation.
    second = await creative.start_session(
        project_id, idea="一支30秒的产品广告", workspace_id=workspace_id
    )
    assert second.session_id != reply.session_id


def test_free_deep_prompt_optimization_budget_refunds_failed_refines(container, monkeypatch):
    container.settings.auth_required = True
    container.settings.free_plan_max_prompt_optimizations = 2

    async def refine_ok(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            optimized_candidate="refined prompt",
            accepted=True,
            source="test",
            reason_codes=(),
            diff=None,
        )

    async def refine_boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("model runtime fell over")

    monkeypatch.setattr(container.model_roles, "refine_prompt", refine_ok)
    app = create_app(container)
    with TestClient(app, raise_server_exceptions=False) as client:
        headers, project_id = _register(client, "free-refine@example.com")
        body = {"project_id": project_id, "prompt": "Mina raises the red phone."}

        first = client.post("/v1/prompts/refine", headers=headers, json=body)
        assert first.status_code == 200, first.text

        # A refine that never happened hands its unit back.
        monkeypatch.setattr(container.model_roles, "refine_prompt", refine_boom)
        failed = client.post("/v1/prompts/refine", headers=headers, json=body)
        assert failed.status_code == 500

        # A refine that degraded to the unoptimized prompt — outage or a
        # live-canary refusal — answers 200 but is not a deep optimization:
        # the unit goes back too.
        async def refine_degraded(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                optimized_candidate="Mina raises the red phone.",
                accepted=False,
                source="local_safe_fallback",
                reason_codes=("PRIMARY_UNAVAILABLE", "FALLBACK_UNAVAILABLE"),
                diff="",
            )

        monkeypatch.setattr(container.model_roles, "refine_prompt", refine_degraded)
        degraded = client.post("/v1/prompts/refine", headers=headers, json=body)
        assert degraded.status_code == 200, degraded.text
        assert degraded.json()["model_refinement"]["source"] == "local_safe_fallback"

        monkeypatch.setattr(container.model_roles, "refine_prompt", refine_ok)
        second = client.post("/v1/prompts/refine", headers=headers, json=body)
        assert second.status_code == 200, second.text

        exhausted = client.post("/v1/prompts/refine", headers=headers, json=body)
        assert exhausted.status_code == 403
        assert "Free plan" in exhausted.json()["detail"]

        with container.database.session() as session:
            counter = session.scalars(select(WorkspaceUsageCounter)).one()
            assert counter.prompt_optimizations == 2
