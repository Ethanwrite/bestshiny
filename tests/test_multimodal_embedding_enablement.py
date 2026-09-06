"""Both multimodal embeddings on, both advisory.

`MULTIMODAL_EMBEDDING` resolves to voyage-multimodal-3.5 on the official
Voyage API and falls back to google/gemini-embedding-2 through the OpenRouter
credential; `STYLE_SEMANTIC_EMBEDDING` is the same Gemini model. Neither is
authority for anything: memory indexes off the request path and degrades to
the structured timeline, and the style layer advises unless an operator
enforces it. Usage still settles at the vendors' list prices. The retired
OpenRouter Voyage row stays retired.
"""

from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from cost_core import TOKEN_BILLING_UNIT, TokenCostEngine
from entitlement_core import (
    LiveCanaryPermitService,
    ModelRoleRuntime,
    ProductionBudgetPolicy,
    ProductionBudgetService,
    WorkspaceModelResolver,
)
from memory_core.embedding import MemoryEmbeddingUnavailable, ModelRoleEmbeddingProvider
from memory_core.schemas import MultimodalContent
from model_registry_core import ModelRole, load_model_infrastructure_config
from openrouter_provider.adapter import OpenRouterProvider
from PIL import Image
from platform_shared import Settings
from production_domain.models import (
    ModelDefinition,
    ModelExecutionRecord,
    ModelPricingProfile,
    ProjectStyleLock,
    User,
    utcnow,
)
from provider_sdk import ProviderError, ProviderTrustLevel
from provider_sdk.capabilities import (
    CapabilityProviderNotFound,
    EmbeddingCapability,
    ProviderCapability,
    ProviderCapabilityCatalog,
)
from provider_sdk.transport import MockProviderTransport, ProviderHttpResponse
from sqlalchemy import select
from style_core import SemanticStyleLayerRequired, SemanticStyleUnavailable
from style_core.service import ProjectStyleService

VOYAGE = ("voyage", "voyage-multimodal-3.5")
GEMINI = ("openrouter", "google/gemini-embedding-2")
CONFIG_PATH = Settings(_env_file=None).model_infrastructure_config


# --- registry ------------------------------------------------------------------------


def test_the_catalogue_binds_voyage_first_and_gemini_as_the_fallback() -> None:
    config = load_model_infrastructure_config(CONFIG_PATH)
    bindings = [item for item in config.role_bindings if item.role is ModelRole.MULTIMODAL_EMBEDDING]
    assert [(item.model_logical_name, item.binding_kind.value, item.priority) for item in bindings] == [
        ("voyage-multimodal-3.5-official", "PRIMARY", 0),
        ("gemini-embedding-2-openrouter", "FALLBACK", 10),
    ]
    semantic = [item for item in config.role_bindings if item.role is ModelRole.STYLE_SEMANTIC_EMBEDDING]
    assert [item.model_logical_name for item in semantic] == ["gemini-embedding-2-openrouter"]
    # The string-only OpenRouter Voyage transport stays retired and unbound.
    retired = next(item for item in config.models if item.logical_name == "voyage-multimodal-3.5-openrouter")
    assert retired.enabled is False
    assert all(
        item.model_logical_name != "voyage-multimodal-3.5-openrouter" for item in config.role_bindings
    )


def test_a_seeded_database_resolves_the_same_order(container, project) -> None:  # type: ignore[no-untyped-def]
    candidates = container.model_infrastructure.candidates_for_role(ModelRole.MULTIMODAL_EMBEDDING)
    assert [(item.provider, item.provider_model_id) for item in candidates] == [VOYAGE, GEMINI]
    resolver = WorkspaceModelResolver(container.database, container.model_infrastructure)
    ordered = resolver.candidates(project.id, "MULTIMODAL_EMBEDDING")
    assert [(item.provider, item.provider_model_id) for item in ordered] == [VOYAGE, GEMINI]


def test_the_flags_ship_on_and_the_enforced_layer_stays_off() -> None:
    settings = Settings(_env_file=None)
    assert settings.feature_voyage_memory is True
    assert settings.feature_semantic_style_advisory is True
    assert settings.feature_semantic_style_lock is False


# --- the credentials gate decides between the bindings --------------------------------------


class _RecordingEmbeddings(EmbeddingCapability):
    trust_level = ProviderTrustLevel.PRODUCTION
    configured = True

    def __init__(self, usage: dict[str, int] | None = None, *, dimension: int = 8) -> None:
        self.calls: list[dict[str, Any]] = []
        self.usage = usage or {"prompt_tokens": 1_200, "total_tokens": 1_200}
        self.dimension = dimension

    async def create_embeddings(  # type: ignore[override]
        self, *, model: str, inputs: Any, parameters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append({"model": model, "inputs": inputs, "parameters": parameters})
        return {"data": [{"embedding": [1.0] * self.dimension}], "model": model, "usage": dict(self.usage)}


class _Unconfigured(EmbeddingCapability):
    trust_level = ProviderTrustLevel.PRODUCTION
    configured = False

    async def create_embeddings(  # type: ignore[override]
        self, *, model: str, inputs: Any, parameters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raise AssertionError("an unconfigured provider must never be called")


def _runtime(  # type: ignore[no-untyped-def]
    container, *, providers: ProviderCapabilityCatalog, provider_mode: str = "live"
) -> ModelRoleRuntime:
    with container.database.session() as session:
        for provider, model in (VOYAGE, GEMINI):
            definition = session.scalar(
                select(ModelDefinition).where(
                    ModelDefinition.provider == provider, ModelDefinition.provider_model_id == model
                )
            )
            assert definition is not None
            definition.live_enabled = True
    return ModelRoleRuntime(
        container.database,
        WorkspaceModelResolver(container.database, container.model_infrastructure),
        providers,
        provider_mode=provider_mode,
        live_canary=LiveCanaryPermitService(container.database),
        token_costs=TokenCostEngine(container.database),
        production_budget=ProductionBudgetService(
            container.database,
            ProductionBudgetPolicy(platform_limit_usd=Decimal("1.00"), provider_limits_usd={}),
        ),
    )


def _seed_gemini_pricing(container) -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        session.add(
            ModelPricingProfile(
                provider=GEMINI[0],
                provider_model_id=GEMINI[1],
                input_mode="input_tokens",
                resolution="",
                currency="USD",
                billing_unit=TOKEN_BILLING_UNIT,
                unit_price=Decimal("0.15"),
                estimate_unit=TOKEN_BILLING_UNIT,
                estimate_unit_price=Decimal("0.15"),
                usd_per_currency=Decimal("1.0"),
                effective_from=utcnow() - timedelta(days=1),
                source_url="https://openrouter.ai/google/gemini-embedding-2",
                source_checked_at=utcnow() - timedelta(days=1),
            )
        )


@pytest.mark.asyncio
async def test_voyage_serves_when_its_key_is_present(container, project) -> None:  # type: ignore[no-untyped-def]
    voyage, gemini = _RecordingEmbeddings(), _RecordingEmbeddings()
    catalog = ProviderCapabilityCatalog()
    catalog.register("voyage", voyage, {ProviderCapability.EMBEDDINGS.value})
    catalog.register("openrouter", gemini, {ProviderCapability.EMBEDDINGS.value})
    # Mock mode: the credentials gate is the thing under test, not the live fence.
    runtime = _runtime(container, providers=catalog, provider_mode="mock")

    execution = await runtime.execute_embeddings(
        project.id, inputs=[{"content": [{"type": "text", "text": "rooftop lantern"}]}]
    )

    assert (execution.resolved_model.provider, execution.resolved_model.provider_model_id) == VOYAGE
    assert len(voyage.calls) == 1 and gemini.calls == []


@pytest.mark.asyncio
async def test_gemini_serves_when_voyage_has_no_credential(container, project) -> None:  # type: ignore[no-untyped-def]
    """The credentials gate chooses the binding, not merely refuses the first."""

    gemini = _RecordingEmbeddings()
    catalog = ProviderCapabilityCatalog()
    catalog.register("voyage", _Unconfigured(), {ProviderCapability.EMBEDDINGS.value})
    catalog.register("openrouter", gemini, {ProviderCapability.EMBEDDINGS.value})
    _seed_gemini_pricing(container)
    runtime = _runtime(container, providers=catalog)

    execution = await runtime.execute_embeddings(
        project.id,
        inputs=[{"content": [{"type": "text", "text": "rooftop lantern"}]}],
        parameters={"dimensions": 512, "input_type": "document"},
    )

    assert (execution.resolved_model.provider, execution.resolved_model.provider_model_id) == GEMINI
    assert len(gemini.calls) == 1
    with container.database.session() as session:
        record = session.get(ModelExecutionRecord, execution.execution_record_id)
        assert record is not None
        # 1,200 prompt tokens at USD 0.15 per million: the list price, settled
        # from the provider's own usage block, on the fallback exactly as on the primary.
        assert record.actual_cost_usd == Decimal("0.000180")
        assert record.cost_source == "TOKENS_LIST"


@pytest.mark.asyncio
async def test_no_configured_provider_is_a_degradation_not_a_500(container, project) -> None:  # type: ignore[no-untyped-def]
    catalog = ProviderCapabilityCatalog()
    catalog.register("voyage", _Unconfigured(), {ProviderCapability.EMBEDDINGS.value})
    catalog.register("openrouter", _Unconfigured(), {ProviderCapability.EMBEDDINGS.value})
    runtime = _runtime(container, providers=catalog)

    with pytest.raises(CapabilityProviderNotFound, match="voyage"):
        await runtime.execute_embeddings(project.id, inputs=[{"content": [{"type": "text", "text": "x"}]}])

    # What memory sees: the advisory boundary, which the engine records as a
    # degraded row and the outbox retries; the generation that queued it is long done.
    provider = ModelRoleEmbeddingProvider(runtime, dimension=256)
    with pytest.raises(MemoryEmbeddingUnavailable):
        provider.embed_with_provenance(
            MultimodalContent(text="rooftop lantern"),
            input_type="document",
            project_id=project.id,
        )


# --- the OpenRouter wire -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_carries_voyage_shaped_pieces_on_its_own_wire() -> None:
    answer = ProviderHttpResponse(
        200, {"data": [{"embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 3}}
    )
    transport = MockProviderTransport({("POST", "/embeddings"): answer})
    provider = OpenRouterProvider(transport=transport)
    await provider.create_embeddings(
        model=GEMINI[1],
        inputs=[
            {
                "content": [
                    {"type": "text", "text": "rooftop lantern"},
                    {"type": "image_url", "image_url": "https://media.invalid/frame.png"},
                    {"type": "image_base64", "image_base64": "data:image/png;base64,AAAA"},
                    {"type": "image_url", "image_url": {"url": "https://media.invalid/already.png"}},
                ]
            }
        ],
        parameters={"dimensions": 512, "input_type": "document"},
    )
    body = transport.requests[0].json_body
    assert body is not None
    assert body["model"] == GEMINI[1]
    assert body["input"] == [
        {
            "content": [
                {"type": "text", "text": "rooftop lantern"},
                {"type": "image_url", "image_url": {"url": "https://media.invalid/frame.png"}},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                {"type": "image_url", "image_url": {"url": "https://media.invalid/already.png"}},
            ]
        }
    ]
    assert "input_type" not in body, "a Voyage-only parameter never reaches this surface"
    assert body["dimensions"] == 512
    # Plain strings keep the string surface.
    await provider.create_embeddings(model=GEMINI[1], inputs=["a", "b"])
    assert transport.requests[1].json_body["input"] == ["a", "b"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_openrouter_refuses_a_video_piece_like_voyage_does() -> None:
    provider = OpenRouterProvider(transport=MockProviderTransport())
    with pytest.raises(ProviderError) as refused:
        await provider.create_embeddings(
            model=GEMINI[1],
            inputs=[{"content": [{"type": "video_url", "video_url": "https://media.invalid/clip.mp4"}]}],
        )
    assert refused.value.code == "EMBEDDING_INPUT_UNSUPPORTED"


def test_gemini_usage_settles_from_prompt_tokens(container) -> None:  # type: ignore[no-untyped-def]
    _seed_gemini_pricing(container)
    settlement = TokenCostEngine(container.database).settle_from_usage(
        GEMINI[0], GEMINI[1], {"prompt_tokens": 2_000, "total_tokens": 2_000}
    )
    assert settlement is not None
    assert settlement.cost_usd == Decimal("0.000300")
    assert settlement.detail == f"2000in+0cached+0out@openrouter:{GEMINI[1]}:{TOKEN_BILLING_UNIT}"


# --- the style layer as advice ----------------------------------------------------------------


class _StubSemanticEmbedder:
    version = "stub-semantic-v1"
    normalization = "L2"
    distance_metric = "cosine"

    def __init__(self, vector: list[float] | None = None, *, fail: bool = False) -> None:
        self.model = "stub/semantic-style"
        self.provider = "stub"
        self.model_revision = ""
        self._vector = vector or [1.0, 0.0, 0.0, 0.0]
        self.fail = fail
        self._dimension = 0

    def space_identity(self):  # type: ignore[no-untyped-def]
        from style_core import EmbeddingSpaceIdentity

        if not self._dimension:
            raise SemanticStyleUnavailable("stub has not answered yet")
        return EmbeddingSpaceIdentity(
            provider=self.provider,
            model=self.model,
            model_revision=self.model_revision,
            input_schema_version=self.version,
            dimension=self._dimension,
            normalization=self.normalization,
            distance_metric=self.distance_metric,
        )

    def embed_images(self, images, *, project_id):  # type: ignore[no-untyped-def]
        if self.fail:
            raise SemanticStyleUnavailable("SEMANTIC_MODEL_UNAVAILABLE: stub is offline")
        self._dimension = len(self._vector)
        return [list(self._vector) for _ in images]


def _png(color: tuple[int, int, int]) -> bytes:
    payload = io.BytesIO()
    Image.new("RGB", (96, 96), color).save(payload, format="PNG")
    return payload.getvalue()


def _style_version(container, project_id: str):  # type: ignore[no-untyped-def]
    media = container.media.register(
        project_id,
        "REFERENCE",
        io.BytesIO(_png((12, 40, 80))),
        filename="style.png",
        mime_type="image/png",
    )[0]
    asset = container.asset_registry.create(project_id, "STYLE", "锁定画风", canonical_metadata={})
    version = container.asset_registry.add_version(asset.id, primary_media_asset_id=media.id)
    container.asset_registry.promote(asset.id, version.id, reason="user approved style")
    return version


def _lock(service: ProjectStyleService, container, project_id: str, version_id: str):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        actor = User(email=f"style-{project_id}-{version_id}@example.com", display_name="Owner")
        session.add(actor)
        session.flush()
        actor_id = actor.id
    return service.lock(
        project_id,
        version_id,
        locked_by_user_id=actor_id,
        reason="用户确认整部作品使用这一版画风",
        explicit_confirmation=True,
    )


def test_an_advisory_layer_records_an_outage_and_locks_anyway(container, project) -> None:  # type: ignore[no-untyped-def]
    service = ProjectStyleService(
        container.database,
        container.storage,
        semantic=_StubSemanticEmbedder(fail=True),
        semantic_mode="advisory",
    )
    version = _style_version(container, project.id)
    locked = _lock(service, container, project.id, version.id)
    with container.database.session() as session:
        stored = session.get(ProjectStyleLock, locked.id)
        assert stored is not None
        assert stored.semantic_style_embedding_id is None
        assert stored.metadata_json["style_layers"] == 1
        assert stored.metadata_json["semantic_layer_mode"] == "advisory"
        reason = str(stored.metadata_json["semantic_layer_absent_reason"])
        assert reason.startswith("SEMANTIC_MODEL_UNAVAILABLE")


def test_the_enforced_layer_still_refuses_to_lock_without_its_reference(container, project) -> None:  # type: ignore[no-untyped-def]
    service = ProjectStyleService(
        container.database,
        container.storage,
        semantic=_StubSemanticEmbedder(fail=True),
        semantic_mode="enforced",
    )
    version = _style_version(container, project.id)
    with pytest.raises(SemanticStyleLayerRequired):
        _lock(service, container, project.id, version.id)


def test_an_advisory_verdict_is_recorded_but_never_the_verdict(container, project) -> None:  # type: ignore[no-untyped-def]
    from production_domain.models import (
        CandidateStatus,
        Episode,
        GenerationCandidate,
        Scene,
        Shot,
        TimelineState,
    )

    service = ProjectStyleService(
        container.database,
        container.storage,
        semantic=_StubSemanticEmbedder([1.0, 0.0, 0.0, 0.0]),
        semantic_mode="advisory",
    )
    version = _style_version(container, project.id)
    _lock(service, container, project.id, version.id)
    output = container.media.register(
        project.id,
        "REFERENCE",
        io.BytesIO(_png((12, 40, 80))),
        filename="candidate.png",
        mime_type="image/png",
    )[0]
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Advisory", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Rainy platform")
        session.add(scene)
        session.flush()
        states = [
            TimelineState(
                project_id=project.id,
                episode_id=episode.id,
                scene_id=scene.id,
                state_kind=kind,
                state_json={},
            )
            for kind in ("SHOT_INPUT", "SHOT_OUTPUT")
        ]
        session.add_all(states)
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            sequence=1,
            prompt="a frame",
            input_state_id=states[0].id,
            output_state_id=states[1].id,
        )
        session.add(shot)
        session.flush()
        candidate = GenerationCandidate(
            shot_id=shot.id,
            attempt_number=1,
            output_asset_id=output.id,
            status=CandidateStatus.VALIDATING.value,
        )
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id

    # Same frame, so layer 1 passes; the semantic model reports another medium.
    service.semantic = _StubSemanticEmbedder([0.0, 1.0, 0.0, 0.0])
    evaluation = service.evaluate_candidate(candidate_id)
    assert evaluation.semantic_status == "FAIL"
    assert evaluation.status == "PASS", "advice is recorded beside the verdict, never over it"
    assert "STYLE_SEMANTIC_ADVISORY:FAIL" in evaluation.reason_codes
    assert "STYLE_SEMANTIC_SIMILARITY_TOO_LOW" in evaluation.reason_codes


def test_the_container_builds_the_advisory_embedder_only_on_a_real_transport(  # type: ignore[no-untyped-def]
    container, tmp_path, database_url
) -> None:
    from video_platform_api.container import build_container

    # The offline suite's container: mock transport, no embedder, single layer as before.
    assert container.styles.semantic is None
    assert container.styles.semantic_mode == "enforced"

    recorded = build_container(
        Settings(
            _env_file=None,
            database_url=database_url,
            storage_root=tmp_path / "media-recorded",
            public_base_url="http://testserver",
            flow_project_id="flow-project-test",
            auth_required=False,
            deployment_environment="test",
            provider_mode="recorded",
        )
    )
    try:
        assert recorded.styles.semantic is not None
        assert recorded.styles.semantic_mode == "advisory"
    finally:
        recorded.database.engine.dispose()
