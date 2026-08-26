from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from entitlement_core import ModelRoleRuntime
from generation_gateway import GenerationTargetError, ProviderRouter
from memory_core import MultimodalContent, VoyageMultimodalEmbeddingProvider
from model_registry_core import ModelRole
from openrouter_provider import OpenRouterProvider
from platform_contracts import GenerationRequest
from platform_shared import Settings
from production_domain.models import (
    DecisionRecord,
    JobStatus,
    ModelDefinition,
    Project,
    ProviderAccount,
    ProviderCredential,
    RunAPIBenchmark,
    User,
    Workspace,
)
from provider_sdk import (
    LIVE_PROVIDER_CONFIRMATION,
    AssetCriticality,
    ChatCapability,
    EdgeTask,
    EdgeTaskRole,
    EmbeddingCapability,
    FactLockPromptRefiner,
    FactLockSet,
    InMemoryProviderBudgetRepository,
    LiveProviderCallDenied,
    LiveProviderSettings,
    MockProviderTransport,
    ProviderBudgetExceeded,
    ProviderCapability,
    ProviderCapabilityCatalog,
    ProviderError,
    ProviderHttpRequest,
    ProviderHttpResponse,
    ProviderMode,
    ProviderTransport,
    RecordedFixtureTransport,
    create_provider_transport,
)
from runapi_provider import RunAPIEdgeProvider
from seedance_provider import SeedanceProvider
from sqlalchemy import func, select, update
from video_platform_api.container import build_container
from wan_provider import WanProvider


def response(status_code: int = 200, **body: Any) -> ProviderHttpResponse:
    return ProviderHttpResponse(status_code, body)


class FakeRoleProvider(ChatCapability, EmbeddingCapability):
    configured = True

    def __init__(self, *, invalid_edge_refinement: bool = False):
        self.invalid_edge_refinement = invalid_edge_refinement
        self.chat_calls: list[dict[str, Any]] = []
        self.embedding_calls: list[dict[str, Any]] = []

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.chat_calls.append({"model": model, "messages": messages, "parameters": parameters})
        if self.invalid_edge_refinement:
            content = {
                "refined_prompt": "Two strangers hold a blue cup",
                "immutable_facts": {"character_count": 2},
            }
        else:
            content = {
                "refined_prompt": "Cinematic close shot: one actor raises the red phone",
                "immutable_facts": {
                    "character_count": 1,
                    "required_prop": "red phone",
                },
            }
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    async def create_embeddings(
        self,
        *,
        model: str,
        inputs: str | list[str] | list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.embedding_calls.append({"model": model, "inputs": inputs, "parameters": parameters})
        return {"data": [{"embedding": [0.25, 0.75]}]}


async def test_live_transport_requires_all_three_gates_before_network(monkeypatch) -> None:
    network_called = False

    class ForbiddenClient:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            nonlocal network_called
            network_called = True

    monkeypatch.setattr("provider_sdk.transport.httpx.AsyncClient", ForbiddenClient)
    transport = create_provider_transport(
        settings=LiveProviderSettings(
            provider_mode="live",
            allow_live_provider_calls=True,
            live_provider_confirmation="WRONG",
        ),
        base_url="https://provider.invalid/v1",
        api_key="unit-test-placeholder",
    )
    with pytest.raises(LiveProviderCallDenied):
        await transport.send(ProviderHttpRequest("GET", "/health"))
    assert network_called is False


async def test_recorded_transport_never_falls_through_to_network() -> None:
    transport = RecordedFixtureTransport({("GET", "/models"): response(data=[{"id": "m"}])})
    result = await transport.send(ProviderHttpRequest("GET", "/models"))
    assert result.json_body["data"] == [{"id": "m"}]
    with pytest.raises(LookupError):
        await transport.send(ProviderHttpRequest("POST", "/videos"))


@pytest.mark.parametrize("mode", ["mock", "recorded"])
def test_voyage_embedding_never_calls_http_outside_live_gate(monkeypatch, mode: str) -> None:
    network_called = False

    def forbidden_post(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal network_called
        network_called = True
        raise AssertionError("network must remain unreachable")

    monkeypatch.setattr("memory_core.embedding.httpx.post", forbidden_post)
    provider = VoyageMultimodalEmbeddingProvider(
        "configured-key-is-not-call-authority",
        transport_settings=LiveProviderSettings(
            provider_mode=mode,
            allow_live_provider_calls=True,
            live_provider_confirmation=LIVE_PROVIDER_CONFIRMATION,
        ),
    )
    with pytest.raises(LiveProviderCallDenied):
        provider.embed(MultimodalContent(text="offline fixture"), input_type="document")
    assert network_called is False


async def test_openrouter_unifies_chat_responses_embeddings_and_video() -> None:
    def handler(request: ProviderHttpRequest) -> ProviderHttpResponse:
        if request.path == "/chat/completions":
            return response(choices=[{"message": {"content": "ok"}}])
        if request.path == "/responses":
            return response(id="resp-1", status="completed")
        if request.path == "/embeddings":
            return response(data=[{"embedding": [0.1, 0.2]}])
        if request.path == "/videos" and request.method == "POST":
            return response(202, id="video-job-1", status="pending")
        if request.path == "/videos/video-job-1":
            return response(id="video-job-1", status="completed", output_url="https://media.invalid/v.mp4")
        raise AssertionError(f"unexpected request {request.method} {request.path}")

    transport = MockProviderTransport(handler=handler)
    provider = OpenRouterProvider(transport=transport)
    assert (await provider.chat(model="text-model", messages=[{"role": "user", "content": "hi"}]))["choices"]
    assert (await provider.create_response(model="text-model", input_value="hi"))["id"] == "resp-1"
    assert (await provider.create_embeddings(model="embed-model", inputs=["a"]))["data"]
    submission = await provider.generate_video(
        {"model": "video-model", "prompt": "one action"}, account_id="", worker_id=""
    )
    job = await provider.get_job(
        submission.provider_job_id,
        account_id="",
        worker_id="",
        generation_type="video",
    )
    assert job.status == "COMPLETED"
    assert [item.path for item in transport.requests] == [
        "/chat/completions",
        "/responses",
        "/embeddings",
        "/videos",
        "/videos/video-job-1",
    ]


async def test_ark_adapter_compiles_seedance_task_and_doubao_chat() -> None:
    def handler(request: ProviderHttpRequest) -> ProviderHttpResponse:
        if request.path == "/contents/generations/tasks":
            return response(202, id="seedance-task-1")
        if request.path == "/chat/completions":
            return response(choices=[{"message": {"content": "draft"}}])
        raise AssertionError(request.path)

    transport = MockProviderTransport(handler=handler)
    provider = SeedanceProvider(
        transport=transport,
        seedance_model_id="seedance-test-model",
        doubao_model_id="doubao-test-model",
    )
    await provider.generate_video(
        {
            "prompt": "subject walks to the door",
            "first_frame_image": "https://media.invalid/start.png",
            "aspect_ratio": "9:16",
            "duration": 5,
        },
        account_id="",
        worker_id="",
    )
    await provider.chat(model="", messages=[{"role": "user", "content": "refine"}])
    video_request = transport.requests[0]
    assert video_request.path == "/contents/generations/tasks"
    assert video_request.json_body is not None
    assert video_request.json_body["model"] == "seedance-test-model"
    assert video_request.json_body["ratio"] == "9:16"
    assert video_request.json_body["return_last_frame"] is True
    assert video_request.json_body["content"][0]["type"] == "text"
    assert transport.requests[1].json_body["model"] == "doubao-test-model"  # type: ignore[index]


async def test_wan_27_uses_dashscope_async_protocol_and_workspace_client() -> None:
    chat_transport = MockProviderTransport(
        {("POST", "/chat/completions"): response(choices=[{"message": {"content": "ok"}}])}
    )
    video_transport = MockProviderTransport(
        {
            (
                "POST",
                "/services/aigc/video-generation/video-synthesis",
            ): response(202, output={"task_id": "wan-task-1"}),
            ("GET", "/tasks/wan-task-1"): response(
                output={"task_status": "SUCCEEDED", "video_url": "https://media.invalid/wan.mp4"}
            ),
        }
    )
    provider = WanProvider(
        chat_transport=chat_transport,
        video_transport=video_transport,
        chat_model_id="chat-test-model",
        t2v_model_id="wan-test-t2v",
    )
    submission = await provider.generate_video(
        {"prompt": "one simple action", "duration": 5, "resolution": "720p"},
        account_id="",
        worker_id="",
    )
    request = video_transport.requests[0]
    assert request.headers == {"X-DashScope-Async": "enable"}
    assert request.json_body == {
        "model": "wan-test-t2v",
        "input": {"prompt": "one simple action"},
        # A resolution *tier*. "720p" used to be posted into `size`, which takes
        # pixel dimensions.
        "parameters": {"duration": 5, "resolution": "720P", "watermark": False},
    }
    job = await provider.get_job(
        submission.provider_job_id,
        account_id="",
        worker_id="",
        generation_type="video",
    )
    assert job.status == "COMPLETED"


async def test_unconfigured_official_adapters_report_not_configured() -> None:
    seedance = await SeedanceProvider().health()
    wan = await WanProvider().health()
    assert (seedance.ok, seedance.detail, seedance.metadata["status"]) == (
        False,
        "NOT_CONFIGURED",
        "NOT_CONFIGURED",
    )
    assert (wan.ok, wan.detail, wan.metadata["status"]) == (
        False,
        "NOT_CONFIGURED",
        "NOT_CONFIGURED",
    )


def test_provider_budget_reserves_atomically_and_disables_at_zero() -> None:
    budget = InMemoryProviderBudgetRepository({"runapi": Decimal("10")})
    first = budget.reserve(
        provider="runapi",
        task_id="edge-1",
        task_role="PROMPT_DRAFT_REFINEMENT",
        estimated_cost_usd=Decimal("6"),
    )
    replay = budget.reserve(
        provider="runapi",
        task_id="edge-1",
        task_role="PROMPT_DRAFT_REFINEMENT",
        estimated_cost_usd=Decimal("6"),
    )
    assert first.acquired is True
    assert replay.acquired is False
    budget.settle(first.reservation_id, actual_cost_usd=Decimal("6"))
    second = budget.reserve(
        provider="runapi",
        task_id="edge-2",
        task_role="ASSET_AUTO_CAPTION",
        estimated_cost_usd=Decimal("4"),
    )
    budget.settle(second.reservation_id, actual_cost_usd=Decimal("4"))
    snapshot = budget.get("runapi")
    assert snapshot.remaining_budget_usd == Decimal("0.000000")
    assert snapshot.routing_enabled is False
    with pytest.raises(ProviderBudgetExceeded):
        budget.reserve(
            provider="runapi",
            task_id="edge-3",
            task_role="PROMPT_TRANSLATION",
            estimated_cost_usd=Decimal("0.01"),
        )


def _runapi_request(criticality: AssetCriticality, task_id: str) -> dict[str, Any]:
    return {
        "model": "edge-test-model",
        "prompt": "temporary placeholder",
        "asset_criticality": criticality.value,
        "_edge_task": EdgeTask(
            task_id=task_id,
            role=EdgeTaskRole.TEMPORARY_PLACEHOLDER_ASSET,
            asset_criticality=criticality,
            estimated_cost_usd=Decimal("0.10"),
        ),
    }


async def test_runapi_can_never_generate_canonical_character() -> None:
    transport = MockProviderTransport(handler=lambda request: response(202, id="must-not-run"))
    provider = RunAPIEdgeProvider(
        transport=transport,
        model_id="edge-test-model",
        budget_repository=InMemoryProviderBudgetRepository({"runapi": 10}),
    )
    with pytest.raises(ProviderError, match="provider trust EDGE cannot handle CANONICAL") as exc:
        await provider.generate_image(
            _runapi_request(AssetCriticality.CANONICAL, "canonical-character"),
            account_id="",
            worker_id="",
        )
    assert exc.value.code == "EDGE_POLICY_DENIED"
    assert transport.requests == []


async def test_runapi_can_never_generate_committed_hero_asset() -> None:
    transport = MockProviderTransport(handler=lambda request: response(202, id="must-not-run"))
    provider = RunAPIEdgeProvider(
        transport=transport,
        model_id="edge-test-model",
        budget_repository=InMemoryProviderBudgetRepository({"runapi": 10}),
    )
    with pytest.raises(ProviderError, match="provider trust EDGE cannot handle HERO") as exc:
        await provider.generate_video(
            _runapi_request(AssetCriticality.HERO, "committed-hero"),
            account_id="",
            worker_id="",
        )
    assert exc.value.code == "EDGE_POLICY_DENIED"
    assert transport.requests == []


async def test_runapi_rejects_public_dict_that_spoofs_internal_edge_task() -> None:
    transport = MockProviderTransport(handler=lambda request: response(202, id="must-not-run"))
    provider = RunAPIEdgeProvider(
        transport=transport,
        model_id="edge-test-model",
        budget_repository=InMemoryProviderBudgetRepository({"runapi": 10}),
    )
    request = _runapi_request(AssetCriticality.TEMPORARY, "server-owned-task")
    task = request["_edge_task"]
    assert isinstance(task, EdgeTask)
    request["_edge_task"] = {
        "task_id": task.task_id,
        "task_role": task.role.value,
        "asset_criticality": task.asset_criticality.value,
        "estimated_cost_usd": "0.000001",
    }

    with pytest.raises(ProviderError, match="server-issued EdgeTask") as exc:
        await provider.generate_image(request, account_id="", worker_id="")

    assert exc.value.code == "EDGE_POLICY_DENIED"
    assert transport.requests == []


def test_provider_router_applies_runapi_trust_before_job_creation() -> None:
    provider = RunAPIEdgeProvider(
        transport=MockProviderTransport(),
        model_id="edge-test-model",
        budget_repository=InMemoryProviderBudgetRepository({"runapi": 10}),
    )
    router = ProviderRouter()
    router.register(provider)
    router.register_model("runapi", "edge-test-model", "image")
    with pytest.raises(GenerationTargetError) as exc:
        router.validate_target(
            "runapi",
            "edge-test-model",
            "image",
            asset_criticality=AssetCriticality.CANONICAL,
        )
    assert exc.value.code == "PROVIDER_TRUST_DENIED"


async def test_runapi_budget_records_required_fields_and_actual_cost() -> None:
    transport = MockProviderTransport(
        {("POST", "/v1/images/generations"): response(202, id="edge-image-1", usage={"cost": "0.04"})}
    )
    budget = InMemoryProviderBudgetRepository({"runapi": 10})
    provider = RunAPIEdgeProvider(
        transport=transport,
        model_id="edge-test-model",
        budget_repository=budget,
    )
    await provider.generate_image(
        _runapi_request(AssetCriticality.TEMPORARY, "temporary-image"),
        account_id="",
        worker_id="",
    )
    record = budget.records("runapi")[0]
    assert record.task_id == "temporary-image"
    assert record.task_role == EdgeTaskRole.TEMPORARY_PLACEHOLDER_ASSET.value
    assert record.estimated_cost_usd == Decimal("0.100000")
    assert record.actual_cost_usd == Decimal("0.040000")
    assert record.remaining_budget_usd == Decimal("9.960000")


async def test_runapi_missing_provider_cost_keeps_server_estimate_reserved() -> None:
    transport = MockProviderTransport(
        {("POST", "/v1/images/generations"): response(202, id="edge-image-uncertain")}
    )
    budget = InMemoryProviderBudgetRepository({"runapi": 10})
    provider = RunAPIEdgeProvider(
        transport=transport,
        model_id="edge-test-model",
        budget_repository=budget,
    )

    await provider.generate_image(
        _runapi_request(AssetCriticality.TEMPORARY, "temporary-image-uncertain-cost"),
        account_id="",
        worker_id="",
    )

    record = budget.records("runapi")[0]
    snapshot = budget.get("runapi")
    assert record.status == "UNCERTAIN"
    assert record.actual_cost_usd is None
    assert snapshot.actual_cost_usd == Decimal("0.000000")
    assert snapshot.reserved_cost_usd == Decimal("0.100000")
    assert snapshot.remaining_budget_usd == Decimal("9.900000")


async def test_runapi_live_paid_boundary_is_durable_before_transport_and_survives_cancellation() -> None:
    budget = InMemoryProviderBudgetRepository({"runapi": 10})

    class CancelledOfflineLiveTransport(ProviderTransport):
        mode = ProviderMode.LIVE

        async def send(self, request: ProviderHttpRequest) -> ProviderHttpResponse:
            del request
            assert budget.records("runapi")[0].status == "UNCERTAIN"
            raise asyncio.CancelledError

    provider = RunAPIEdgeProvider(
        transport=CancelledOfflineLiveTransport(),
        api_key="offline-test-placeholder",
        model_id="edge-test-model",
        budget_repository=budget,
        allow_edge_calls=True,
    )

    with pytest.raises(asyncio.CancelledError):
        await provider.generate_image(
            _runapi_request(AssetCriticality.TEMPORARY, "cancelled-after-paid-boundary"),
            account_id="",
            worker_id="",
        )

    record = budget.records("runapi")[0]
    assert record.status == "UNCERTAIN"
    assert budget.get("runapi").reserved_cost_usd == Decimal("0.100000")


async def test_runapi_live_boundary_is_atomic_with_the_initial_budget_insert() -> None:
    class AtomicBoundaryBudget(InMemoryProviderBudgetRepository):
        uncertain_settle_calls = 0

        def settle(  # type: ignore[override]
            self,
            reservation_id: str,
            *,
            actual_cost_usd: Decimal | None,
            status: str = "SETTLED",
        ):  # type: ignore[no-untyped-def]
            if status == "UNCERTAIN":
                self.uncertain_settle_calls += 1
            return super().settle(
                reservation_id,
                actual_cost_usd=actual_cost_usd,
                status=status,
            )

    budget = AtomicBoundaryBudget({"runapi": 10})

    class CancelBeforeTransportBody(ProviderTransport):
        mode = ProviderMode.LIVE

        async def send(self, request: ProviderHttpRequest) -> ProviderHttpResponse:
            del request
            record = budget.records("runapi")[0]
            assert record.status == "UNCERTAIN"
            assert budget.uncertain_settle_calls == 0
            raise asyncio.CancelledError

    provider = RunAPIEdgeProvider(
        transport=CancelBeforeTransportBody(),
        api_key="offline-test-placeholder",
        model_id="edge-test-model",
        budget_repository=budget,
        allow_edge_calls=True,
    )

    with pytest.raises(asyncio.CancelledError):
        await provider.generate_image(
            _runapi_request(AssetCriticality.TEMPORARY, "atomic-live-budget-boundary"),
            account_id="",
            worker_id="",
        )

    assert budget.records("runapi")[0].status == "UNCERTAIN"
    assert budget.uncertain_settle_calls == 0


async def test_runapi_live_actual_response_atomically_settles_uncertain_boundary() -> None:
    budget = InMemoryProviderBudgetRepository({"runapi": 10})

    class SuccessfulOfflineLiveTransport(ProviderTransport):
        mode = ProviderMode.LIVE

        async def send(self, request: ProviderHttpRequest) -> ProviderHttpResponse:
            del request
            assert budget.records("runapi")[0].status == "UNCERTAIN"
            return response(202, id="live-fixture-image", usage={"cost": "0.04"})

    provider = RunAPIEdgeProvider(
        transport=SuccessfulOfflineLiveTransport(),
        api_key="offline-test-placeholder",
        model_id="edge-test-model",
        budget_repository=budget,
        allow_edge_calls=True,
    )

    result = await provider.generate_image(
        _runapi_request(AssetCriticality.TEMPORARY, "settle-after-paid-boundary"),
        account_id="",
        worker_id="",
    )

    assert result.provider_job_id == "live-fixture-image"
    record = budget.records("runapi")[0]
    assert record.status == "SETTLED"
    assert record.actual_cost_usd == Decimal("0.040000")
    assert budget.get("runapi").reserved_cost_usd == Decimal("0.000000")


async def test_runapi_live_edge_switch_is_additional_to_global_gate() -> None:
    transport = create_provider_transport(
        settings=LiveProviderSettings(
            provider_mode="live",
            allow_live_provider_calls=True,
            live_provider_confirmation=LIVE_PROVIDER_CONFIRMATION,
        ),
        base_url="https://provider.invalid/v1",
        api_key="unit-test-placeholder",
    )
    budget = InMemoryProviderBudgetRepository({"runapi": 10})
    provider = RunAPIEdgeProvider(
        transport=transport,
        base_url="https://provider.invalid/v1",
        model_id="edge-test-model",
        budget_repository=budget,
        allow_edge_calls=False,
    )
    with pytest.raises(ProviderError) as exc:
        await provider.generate_image(
            _runapi_request(AssetCriticality.TEMPORARY, "live-denied"),
            account_id="",
            worker_id="",
        )
    assert exc.value.code == "RUNAPI_EDGE_CALL_DENIED"
    assert budget.records("runapi") == []


async def test_runapi_failed_global_live_preflight_creates_no_budget_boundary() -> None:
    transport = create_provider_transport(
        settings=LiveProviderSettings(
            provider_mode="live",
            allow_live_provider_calls=True,
            live_provider_confirmation="WRONG",
        ),
        base_url="https://provider.invalid/v1",
        api_key="offline-test-placeholder",
    )
    budget = InMemoryProviderBudgetRepository({"runapi": 10})
    provider = RunAPIEdgeProvider(
        transport=transport,
        api_key="offline-test-placeholder",
        model_id="edge-test-model",
        budget_repository=budget,
        allow_edge_calls=True,
    )

    with pytest.raises(ProviderError) as exc:
        await provider.generate_image(
            _runapi_request(AssetCriticality.TEMPORARY, "live-preflight-denied"),
            account_id="",
            worker_id="",
        )

    assert exc.value.code == "LIVE_PROVIDER_CALL_DENIED"
    assert budget.records("runapi") == []


async def test_runapi_prompt_fact_lock_rejects_changed_facts_and_uses_fallback() -> None:
    facts = FactLockSet(
        {
            "character_identity": {"name": "LinJin"},
            "character_count": 1,
            "required_prop": "red phone",
        },
        required_literals=("red phone",),
    )

    async def changed_draft(payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["immutable_facts"] == facts.immutable_facts
        return {
            "refined_prompt": "Two people inspect a blue phone",
            "immutable_facts": {"character_count": 2},
        }

    async def safe_fallback(original: str, locks: FactLockSet) -> dict[str, Any]:
        return {
            "refined_prompt": f"Cinematic medium shot: {original}",
            "immutable_facts": locks.immutable_facts,
        }

    result = await FactLockPromptRefiner(
        changed_draft,
        fallback_generator=safe_fallback,
    ).refine(
        original_prompt="One character named LinJin raises the red phone",
        fact_locks=facts,
    )
    assert result.accepted is True
    assert result.source == "fallback"
    assert "red phone" in result.optimized_candidate
    assert result.diff


async def test_runapi_fact_lock_never_overwrites_original_on_rejection() -> None:
    facts = FactLockSet(
        {"scene_identity": "approved-lobby"},
        required_literals=("lobby",),
    )

    async def invalid_draft(payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        return {"refined_prompt": "A beach", "immutable_facts": {"scene_identity": "beach"}}

    original = "The actor waits in the lobby"
    result = await FactLockPromptRefiner(invalid_draft).refine(
        original_prompt=original,
        fact_locks=facts,
    )
    assert result.accepted is False
    assert result.optimized_candidate == original
    assert "IMMUTABLE_FACTS_CHANGED" in result.reason_codes


async def test_fact_lock_rejects_self_consistent_echo_with_changed_candidate_semantics() -> None:
    facts = FactLockSet({"character_count": 1})

    async def self_consistent_lie(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "refined_prompt": "Two strangers enter the room",
            "immutable_facts": payload["immutable_facts"],
        }

    original = "One character enters the room"
    result = await FactLockPromptRefiner(self_consistent_lie).refine(
        original_prompt=original,
        fact_locks=facts,
    )

    assert result.accepted is False
    assert result.optimized_candidate == original
    assert "IMMUTABLE_FACT_CONTENT_CHANGED:character_count" in result.reason_codes


@pytest.mark.parametrize(
    ("original", "candidate", "literal"),
    [
        (
            "LinJin wears a red coat in the lobby",
            "LinJin does not wear a red coat; he wears a blue jacket",
            "red coat",
        ),
        (
            "林晋穿着红色外套站在大厅里",
            "林晋没有穿红色外套，而是穿着蓝色夹克",
            "红色外套",
        ),
    ],
)
def test_fact_lock_rejects_local_negation_polarity_change(
    original: str,
    candidate: str,
    literal: str,
) -> None:
    facts = FactLockSet(
        {"costume_version": literal},
        required_literals=(literal,),
        locked_spans={"costume_version": (literal,)},
    )

    valid, reasons = facts.validate(
        candidate,
        facts.immutable_facts,
        source_prompt=original,
    )

    assert valid is False
    assert "REQUIRED_FACT_LITERAL_POLARITY_CHANGED" in reasons
    assert "IMMUTABLE_FACT_POLARITY_CHANGED:costume_version" in reasons


@pytest.mark.parametrize(
    ("original", "candidate", "literal"),
    [
        (
            "LinJin does not wear a red coat in this scene",
            "Cinematic close-up: LinJin still does not wear a red coat in this scene",
            "red coat",
        ),
        (
            "林晋在这个场景里没有穿红色外套",
            "近景中，林晋依然没有穿红色外套",
            "红色外套",
        ),
    ],
)
def test_fact_lock_preserves_original_negative_polarity(
    original: str,
    candidate: str,
    literal: str,
) -> None:
    facts = FactLockSet(
        {"costume_version": literal},
        required_literals=(literal,),
        locked_spans={"costume_version": (literal,)},
    )

    valid, reasons = facts.validate(
        candidate,
        facts.immutable_facts,
        source_prompt=original,
    )

    assert valid is True
    assert reasons == []


def test_edge_task_contract_parses_decimal_without_float_loss() -> None:
    task = EdgeTask.from_mapping(
        {
            "task_id": "precision",
            "task_role": EdgeTaskRole.ASSET_AUTO_CAPTION.value,
            "asset_criticality": AssetCriticality.EDGE.value,
            "estimated_cost_usd": "0.000001",
        }
    )
    assert task.estimated_cost_usd == Decimal("0.000001")


def test_runtime_model_ids_enable_only_from_complete_provider_configuration(tmp_path) -> None:
    container = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'runtime-models.db'}",
            storage_root=tmp_path / "media",
            deployment_environment="test",
            auth_required=False,
            provider_mode="mock",
            ark_api_key="unit-test-ark-placeholder",
            doubao_model_id="doubao-runtime-model",
            seedance_model_id="seedance-runtime-model",
            wan_api_key="unit-test-wan-placeholder",
            wan_dashscope_base_url="https://workspace.invalid/api/v1",
            wan2_7_t2v_model_id="wan-runtime-model",
        )
    )
    free_director = container.model_infrastructure.resolve_role(
        ModelRole.DIRECTOR,
        plan_tier="FREE",
        asset_criticality=AssetCriticality.STANDARD,
    )
    free_seedance = container.model_infrastructure.resolve_role(
        ModelRole.VIDEO_SEEDANCE,
        plan_tier="FREE",
        asset_criticality=AssetCriticality.HERO,
    )
    wan = container.model_infrastructure.resolve_role(
        ModelRole.VIDEO_WAN,
        asset_criticality=AssetCriticality.STANDARD,
    )
    assert free_director.provider_model_id == "doubao-runtime-model"
    assert free_seedance.provider_model_id == "seedance-runtime-model"
    assert wan.provider_model_id == "wan-runtime-model"
    assert (
        container.credit_pricing.estimate(
            provider="seedance",
            model="seedance-runtime-model",
            media_type="video",
            duration=5,
        ).provider_cost_usd
        > 0
    )
    assert (
        container.credit_pricing.estimate(
            provider="wan",
            model="wan-runtime-model",
            media_type="video",
            duration=5,
        ).provider_cost_usd
        > 0
    )
    with pytest.raises(LookupError, match="no compatible model binding"):
        container.model_infrastructure.resolve_role(
            ModelRole.VIDEO_SEEDANCE,
            plan_tier="FREE",
            asset_criticality=AssetCriticality.HERO,
            require_live=True,
        )


def test_openrouter_live_enablement_requires_complete_three_part_gate(tmp_path) -> None:
    container = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'live-models.db'}",
            storage_root=tmp_path / "media",
            deployment_environment="test",
            auth_required=False,
            provider_mode="live",
            allow_live_provider_calls=True,
            live_provider_confirmation=LIVE_PROVIDER_CONFIRMATION,
            openrouter_api_key="unit-test-openrouter-placeholder",
        )
    )
    # Deployment gates do not silently overwrite the persisted admin switch.
    container.model_infrastructure.configure_runtime_model(
        "gpt-5.6-sol-openrouter",
        "openai/gpt-5.6-sol",
        enabled=True,
        live_enabled=True,
    )
    route = container.model_infrastructure.resolve_role(
        ModelRole.DIRECTOR,
        asset_criticality=AssetCriticality.STANDARD,
        require_live=True,
    )
    assert route.provider == "openrouter"


@pytest.mark.parametrize(
    ("boundary_mutation", "expected_error_code"),
    [
        ("disable_live", "MODEL_LIVE_DISABLED"),
        ("change_runtime_id", "MODEL_DEFINITION_NOT_FOUND"),
    ],
)
async def test_live_model_change_at_atomic_boundary_never_reaches_transport(
    tmp_path,
    monkeypatch,
    boundary_mutation: str,
    expected_error_code: str,
) -> None:
    container = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'live-disabled-model.db'}",
            storage_root=tmp_path / "media",
            deployment_environment="test",
            auth_required=False,
            provider_mode="live",
            allow_live_provider_calls=True,
            live_provider_confirmation=LIVE_PROVIDER_CONFIRMATION,
            openrouter_api_key="unit-test-openrouter-placeholder",
        )
    )
    provider = container.providers.get("openrouter")
    transport_called = False
    atomic_fence_called = False

    async def forbidden_send(request: ProviderHttpRequest) -> ProviderHttpResponse:
        del request
        nonlocal transport_called
        transport_called = True
        raise AssertionError("live transport must remain behind the persistent model gate")

    monkeypatch.setattr(provider.client.transport, "send", forbidden_send)  # type: ignore[attr-defined]
    with container.database.session() as session:
        definition = session.scalar(
            select(ModelDefinition).where(
                ModelDefinition.provider == "openrouter",
                ModelDefinition.provider_model_id == "kwaivgi/kling-v3.0-std",
                ModelDefinition.modality == "video",
            )
        )
        assert definition is not None
        assert definition.enabled is True
        assert definition.live_enabled is False
        # Let the early check pass; the hook below changes the switch between
        # the final locked read and the GenerationJob boundary CAS.
        definition.live_enabled = True
        user = User(email="live-model-gate@example.com", display_name="Live Model Gate")
        session.add(user)
        session.flush()
        workspace = Workspace(
            owner_user_id=user.id,
            name="Live Model Gate",
            plan_tier="PRO",
        )
        session.add(workspace)
        session.flush()
        project = Project(workspace_id=workspace.id, title="Live Model Gate")
        session.add(project)
        session.flush()
        project_id = project.id

    original_target_lookup = container.model_infrastructure.runtime_model_for_target_in_session

    def disable_model_inside_paid_boundary(
        session,  # type: ignore[no-untyped-def]
        provider_name: str,
        model: str,
        modality: str,
        *,
        for_update: bool = False,
    ):  # type: ignore[no-untyped-def]
        nonlocal atomic_fence_called
        state = original_target_lookup(
            session,
            provider_name,
            model,
            modality,
            for_update=for_update,
        )
        if for_update and state is not None and state.live_enabled:
            atomic_fence_called = True
            changed_values = (
                {"live_enabled": False}
                if boundary_mutation == "disable_live"
                else {"provider_model_id": "admin/revoked-kling-model"}
            )
            session.execute(
                update(ModelDefinition)
                .where(ModelDefinition.id == state.definition_id)
                .values(**changed_values)
            )
        return state

    monkeypatch.setattr(
        container.model_infrastructure,
        "runtime_model_for_target_in_session",
        disable_model_inside_paid_boundary,
    )
    container.live_canary.create(
        provider="openrouter",
        model="kwaivgi/kling-v3.0-std",
        max_requests=1,
        max_cost_usd="0.10",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        purpose="reach the independent atomic model-switch regression fence",
    )

    job, replayed = container.gateway.create(
        GenerationRequest(
            project_id=project_id,
            type="video",
            provider="openrouter",
            model="kwaivgi/kling-v3.0-std",
            prompt="One subject performs one action",
            idempotency_key="live-disabled-model-gate",
            asset_criticality=AssetCriticality.STANDARD,
            metadata={"live_enabled": True, "model_definition_enabled": True},
        ),
        # Every plan is charged now, so every generation carries a server-owned
        # quote. This test is about the live-model fence, not about pricing, so
        # the quote is the smallest one that is still a real charge.
        estimated_credits=1,
    )

    assert replayed is False
    failed = await container.gateway.process(job.id)
    assert failed.status == JobStatus.FAILED.value
    assert failed.error_code == expected_error_code
    assert failed.submission_state == "NOT_SENT"
    assert failed.provider_job_id is None
    assert atomic_fence_called is True
    assert transport_called is False


@pytest.mark.parametrize(
    "model_id",
    ["kwaivgi/kling-v3.0-std", "kwaivgi/kling-v3.0-pro"],
)
def test_openrouter_kling_models_use_persisted_manual_pricing_profiles(container, model_id: str) -> None:
    profile = container.model_registry.get(model_id, "openrouter")
    assert profile is not None
    assert profile.source == "MANUAL_PRIOR"
    assert profile.adapter == "kling"
    estimate = container.credit_pricing.estimate(
        provider="openrouter",
        model=model_id,
        media_type="video",
        duration=5,
    )
    assert estimate.provider_cost_usd > 0
    assert estimate.credits > 0


def test_container_restart_preserves_openrouter_admin_override(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'persistent-models.db'}"
    base_settings = dict(
        _env_file=None,
        database_url=database_url,
        storage_root=tmp_path / "media",
        deployment_environment="test",
        auth_required=False,
        provider_mode="live",
        allow_live_provider_calls=True,
        live_provider_confirmation=LIVE_PROVIDER_CONFIRMATION,
        openrouter_api_key="unit-test-openrouter-placeholder",
    )
    first = build_container(Settings(**base_settings))
    with first.database.session() as session:
        definition = session.scalar(
            select(ModelDefinition).where(ModelDefinition.logical_name == "gpt-5.6-sol-openrouter")
        )
        assert definition is not None
        definition.provider_model_id = "admin/approved-director-model"
        definition.enabled = False
        definition.live_enabled = False

    second = build_container(Settings(**base_settings))
    persisted = second.model_infrastructure.runtime_model("gpt-5.6-sol-openrouter")
    assert persisted.provider_model_id == "admin/approved-director-model"
    assert persisted.enabled is False
    assert persisted.live_enabled is False


def test_container_restart_preserves_env_backed_model_admin_overrides(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'persistent-env-models.db'}"
    base_settings = dict(
        _env_file=None,
        database_url=database_url,
        storage_root=tmp_path / "media",
        deployment_environment="test",
        auth_required=False,
        provider_mode="live",
        allow_live_provider_calls=True,
        live_provider_confirmation=LIVE_PROVIDER_CONFIRMATION,
        ark_api_key="unit-test-ark-placeholder",
        doubao_model_id="env-doubao-model",
        seedance_model_id="env-seedance-model",
        wan_api_key="unit-test-wan-placeholder",
        wan_dashscope_base_url="https://workspace.invalid/api/v1",
        wan2_7_t2v_model_id="env-wan-model",
        runapi_api_key="unit-test-runapi-placeholder",
        runapi_base_url="https://runapi.invalid",
        runapi_model_id="env-runapi-model",
        allow_runapi_edge_calls=True,
    )
    first = build_container(Settings(**base_settings))
    overrides = {
        "doubao-free-reasoner": "admin-doubao-model",
        "seedance-2.5-official": "admin-seedance-model",
        "wan-2.7-official": "admin-wan-model",
        "runapi-prompt-refiner-edge": "admin-runapi-model",
    }
    with first.database.session() as session:
        definitions = session.scalars(
            select(ModelDefinition).where(ModelDefinition.logical_name.in_(overrides))
        ).all()
        assert len(definitions) == len(overrides)
        assert all(definition.enabled and definition.live_enabled for definition in definitions)
        for definition in definitions:
            definition.provider_model_id = overrides[definition.logical_name]
            definition.enabled = False
            definition.live_enabled = False

    second = build_container(Settings(**base_settings))
    for logical_name, admin_model_id in overrides.items():
        persisted = second.model_infrastructure.runtime_model(logical_name)
        assert persisted.provider_model_id == admin_model_id
        assert persisted.enabled is False
        assert persisted.live_enabled is False


async def test_live_model_role_runtime_cannot_be_downgraded_by_caller(container, project) -> None:
    provider = FakeRoleProvider()
    catalog = ProviderCapabilityCatalog()
    catalog.register("openrouter", provider, {ProviderCapability.CHAT.value})
    runtime = ModelRoleRuntime(
        container.database,
        container.workspace_models,
        catalog,
        provider_mode="live",
    )

    with pytest.raises(LookupError, match="no compatible model binding"):
        await runtime.execute_chat(
            project.id,
            ModelRole.DIRECTOR,
            messages=[{"role": "user", "content": "Plan one shot"}],
            require_live=False,
        )
    assert provider.chat_calls == []


async def test_live_model_role_runtime_rechecks_persisted_switch_at_transport_boundary(
    container,
    project,
    monkeypatch,
) -> None:
    container.model_infrastructure.configure_runtime_model(
        "gpt-5.6-sol-openrouter",
        "openai/gpt-5.6-sol",
        enabled=True,
        live_enabled=True,
    )
    provider = FakeRoleProvider()
    catalog = ProviderCapabilityCatalog()
    catalog.register("openrouter", provider, {ProviderCapability.CHAT.value})
    runtime = ModelRoleRuntime(
        container.database,
        container.workspace_models,
        catalog,
        provider_mode="live",
    )
    original_resolve = runtime.resolve
    first_resolution = True

    def resolve_then_disable(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal first_resolution
        resolved = original_resolve(*args, **kwargs)
        if first_resolution:
            first_resolution = False
            container.model_infrastructure.configure_runtime_model(
                "gpt-5.6-sol-openrouter",
                "openai/gpt-5.6-sol",
                enabled=True,
                live_enabled=False,
            )
        return resolved

    monkeypatch.setattr(runtime, "resolve", resolve_then_disable)
    with pytest.raises(LookupError, match="no compatible model binding"):
        await runtime.execute_chat(
            project.id,
            ModelRole.DIRECTOR,
            messages=[{"role": "user", "content": "Plan one shot"}],
        )
    assert provider.chat_calls == []


def test_flow_legacy_video_and_narwhal_image_use_persisted_live_switches(tmp_path) -> None:
    container = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'flow-live-switches.db'}",
            storage_root=tmp_path / "media",
            deployment_environment="test",
            auth_required=False,
            provider_mode="live",
            allow_live_provider_calls=True,
            live_provider_confirmation=LIVE_PROVIDER_CONFIRMATION,
        )
    )

    for model, modality in (("veo", "video"), ("NARWHAL", "image")):
        with pytest.raises(GenerationTargetError) as exc_info:
            container.gateway._validate_persisted_generation_target(  # noqa: SLF001
                "google_flow",
                model,
                modality,
                workspace_scoped=True,
            )
        assert exc_info.value.code == "MODEL_LIVE_DISABLED"

    container.model_infrastructure.configure_runtime_model(
        "flow-veo-3.1-internal",
        "flow-veo-3.1",
        enabled=True,
        live_enabled=True,
    )
    container.model_infrastructure.configure_runtime_model(
        "flow-narwhal-image-internal",
        "NARWHAL",
        enabled=True,
        live_enabled=True,
    )
    assert (
        container.gateway._validate_persisted_generation_target(  # noqa: SLF001
            "google_flow",
            "veo",
            "video",
            workspace_scoped=True,
        )
        is not None
    )
    assert (
        container.gateway._validate_persisted_generation_target(  # noqa: SLF001
            "google_flow",
            "NARWHAL",
            "image",
            workspace_scoped=True,
        )
        is not None
    )


async def test_model_role_runtime_executes_chat_and_embeddings_and_logs_decisions(
    container,
    project,
) -> None:
    provider = FakeRoleProvider()
    catalog = ProviderCapabilityCatalog()
    catalog.register(
        "openrouter",
        provider,
        {ProviderCapability.CHAT.value, ProviderCapability.EMBEDDINGS.value},
    )
    runtime = ModelRoleRuntime(container.database, container.workspace_models, catalog)

    chat = await runtime.execute_chat(
        project.id,
        ModelRole.DIRECTOR,
        messages=[{"role": "user", "content": "Plan one shot"}],
    )
    embedding = await runtime.execute_embeddings(project.id, inputs=["frame one", "frame two"])

    assert chat.resolved_model.provider == "openrouter"
    assert chat.capability is ProviderCapability.CHAT
    assert embedding.capability is ProviderCapability.EMBEDDINGS
    assert embedding.response["data"][0]["embedding"] == [0.25, 0.75]
    with container.database.session() as session:
        records = session.scalars(
            select(DecisionRecord)
            .where(DecisionRecord.project_id == project.id)
            .order_by(DecisionRecord.created_at)
        ).all()
    assert [record.decision_type for record in records] == [
        "MODEL_ROLE_EXECUTION",
        "MODEL_ROLE_EXECUTION",
    ]
    assert all(record.input_features["outcome"] == "SUCCEEDED" for record in records)
    assert all("content" not in record.input_features for record in records)


async def test_fact_lock_runtime_uses_openrouter_when_runapi_changes_facts(container, project) -> None:
    container.model_infrastructure.configure_runtime_model(
        "runapi-prompt-refiner-edge",
        "edge-refiner-test",
        enabled=True,
    )
    edge = FakeRoleProvider(invalid_edge_refinement=True)
    fallback = FakeRoleProvider()
    catalog = ProviderCapabilityCatalog()
    catalog.register("runapi", edge, {ProviderCapability.CHAT.value})
    catalog.register("openrouter", fallback, {ProviderCapability.CHAT.value})
    runtime = ModelRoleRuntime(container.database, container.workspace_models, catalog)
    locks = FactLockSet(
        {"character_count": 1, "required_prop": "red phone"},
        required_literals=("red phone",),
    )

    result = await runtime.refine_prompt(
        project.id,
        original_prompt="One actor raises the red phone",
        fact_locks=locks,
        task_id="fact-lock-runtime-test",
        estimated_cost_usd="0.01",
    )

    assert result.accepted is True
    assert result.source == "fallback"
    assert result.optimized_candidate.startswith("Cinematic close shot")
    issued_task = edge.chat_calls[0]["parameters"]["_edge_task"]
    assert isinstance(issued_task, EdgeTask)
    assert issued_task.role is EdgeTaskRole.PROMPT_DRAFT_REFINEMENT
    assert issued_task.task_id != "fact-lock-runtime-test"
    assert issued_task.estimated_cost_usd == Decimal("0.01")
    with container.database.session() as session:
        refinement = session.scalar(
            select(DecisionRecord).where(
                DecisionRecord.project_id == project.id,
                DecisionRecord.decision_type == "FACT_LOCK_PROMPT_REFINEMENT",
            )
        )
    assert refinement is not None
    assert refinement.selected_action == "fallback"
    with container.database.session() as session:
        benchmark = session.scalar(select(RunAPIBenchmark))
    assert benchmark is not None
    assert benchmark.task_id == issued_task.task_id
    assert benchmark.task_type == EdgeTaskRole.PROMPT_DRAFT_REFINEMENT.value
    assert benchmark.fact_lock_pass is False
    assert benchmark.fallback_required is True
    assert benchmark.latency_ms >= 0
    assert benchmark.actual_cost_usd is None


@pytest.mark.parametrize(
    ("provider_name", "model_id", "fixture_path", "fixture_response"),
    [
        (
            "openrouter",
            "kwaivgi/kling-v3.0-std",
            "/videos",
            response(202, id="openrouter-e2e-job", status="pending"),
        ),
        (
            "seedance",
            "seedance-e2e-model",
            "/contents/generations/tasks",
            response(202, id="seedance-e2e-job"),
        ),
        (
            "wan",
            "wan-e2e-model",
            "/services/aigc/video-generation/video-synthesis",
            response(202, output={"task_id": "wan-e2e-job"}),
        ),
    ],
)
async def test_direct_api_provider_mock_gateway_reaches_submitted(
    tmp_path,
    provider_name: str,
    model_id: str,
    fixture_path: str,
    fixture_response: ProviderHttpResponse,
) -> None:
    container = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'direct-api.db'}",
            storage_root=tmp_path / "media",
            public_base_url="http://testserver",
            deployment_environment="test",
            auth_required=False,
            platform_api_key="unit-test-platform-placeholder",
            provider_mode="mock",
            openrouter_api_key="unit-test-openrouter-placeholder",
            ark_api_key="unit-test-ark-placeholder",
            seedance_model_id="seedance-e2e-model",
            wan_api_key="unit-test-wan-placeholder",
            wan_openai_base_url="https://workspace.invalid/compatible-mode/v1",
            wan_dashscope_base_url="https://workspace.invalid/api/v1",
            wan2_7_t2v_model_id="wan-e2e-model",
        )
    )
    provider = container.providers.get(provider_name)
    transport = (
        provider.video_client.transport if isinstance(provider, WanProvider) else provider.client.transport  # type: ignore[attr-defined]
    )
    assert isinstance(transport, MockProviderTransport)
    transport.add_fixture("POST", fixture_path, fixture_response)

    with container.database.session() as session:
        from production_domain.models import Project

        project = Project(title=f"{provider_name} direct API E2E")
        session.add(project)
        session.flush()
        project_id = project.id

    job, replayed = container.gateway.create(
        GenerationRequest(
            project_id=project_id,
            type="video",
            provider=provider_name,
            model=model_id,
            prompt="One subject performs one action",
            idempotency_key=f"{provider_name}-direct-api-e2e",
            asset_criticality=AssetCriticality.STANDARD,
        )
    )
    assert replayed is False
    assert job.status == JobStatus.NEW.value
    submitted = await container.gateway.process(job.id)
    assert submitted.status == JobStatus.SUBMITTED.value
    assert submitted.provider_job_id
    with container.database.session() as session:
        account = session.scalar(
            select(ProviderAccount).where(
                ProviderAccount.provider == provider_name,
                ProviderAccount.account_identifier == f"direct-api://{provider_name}",
            )
        )
        assert account is not None
        assert account.metadata_json == {
            "resource_kind": "DIRECT_API",
            "stores_provider_secret": False,
        }
        assert session.scalar(select(func.count(ProviderCredential.id))) == 0


def test_declared_model_id_divergence_is_reported_and_never_silently_applied(tmp_path) -> None:
    """The registry wins, but the disagreement stops being invisible.

    An operator override of `provider_model_id` must survive a restart — that is
    pinned by `test_container_restart_preserves_env_backed_model_admin_overrides`.
    The defect was never that the registry won; it was that nothing said the two
    disagreed. Seedance 2.5 sat with `.env` naming `doubao-seedance-2-5-260628`
    and the row still holding the seeded placeholder `seedance-2.5`, and the
    first report of it was Ark refusing a submission that had already reserved
    credits.
    """

    database_url = f"sqlite:///{tmp_path / 'declared-divergence.db'}"
    container = build_container(
        Settings(
            _env_file=None,
            database_url=database_url,
            storage_root=tmp_path / "media",
            deployment_environment="test",
            auth_required=False,
            ark_api_key="unit-test-ark-placeholder",
            seedance_model_id="doubao-seedance-2-5-260628",
        )
    )
    infrastructure = container.model_infrastructure

    # Agreement is silence.
    assert (
        infrastructure.declared_model_id_divergence(
            "seedance-2.5-official", "doubao-seedance-2-5-260628"
        )
        is None
    )

    with container.database.session() as session:
        definition = session.scalar(
            select(ModelDefinition).where(ModelDefinition.logical_name == "seedance-2.5-official")
        )
        assert definition is not None
        definition.provider_model_id = "seedance-2.5"

    stored = infrastructure.declared_model_id_divergence(
        "seedance-2.5-official", "doubao-seedance-2-5-260628"
    )
    assert stored == "seedance-2.5"

    # Reporting must not be a disguised write.
    after = infrastructure.runtime_model("seedance-2.5-official")
    assert after.provider_model_id == "seedance-2.5"
