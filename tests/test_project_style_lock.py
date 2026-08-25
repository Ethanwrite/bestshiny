from __future__ import annotations

import io

import pytest
from director_production import CandidateNotCommittable
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from production_domain.models import (
    AssetVersion,
    CandidateStatus,
    CandidateStyleEvaluation,
    Episode,
    GenerationCandidate,
    Project,
    QADecision,
    QAResult,
    Scene,
    Shot,
    StyleEmbedding,
    TimelineState,
    User,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from style_core import StyleLockConflict
from video_platform_api.main import create_app


def _png(color: tuple[int, int, int], *, stripes: bool = False) -> bytes:
    image = Image.new("RGB", (96, 96), color)
    if stripes:
        draw = ImageDraw.Draw(image)
        for offset in range(0, 96, 8):
            draw.rectangle((offset, 0, offset + 2, 95), fill=(255, 255, 255))
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    return payload.getvalue()


def _media(container, project_id: str, payload: bytes, name: str):  # type: ignore[no-untyped-def]
    return container.media.register(
        project_id,
        "REFERENCE",
        io.BytesIO(payload),
        filename=name,
        mime_type="image/png",
    )[0]


def _style_version(container, project_id: str, payload: bytes, *, name: str = "锁定画风"):  # type: ignore[no-untyped-def]
    media = _media(container, project_id, payload, "style.png")
    asset = container.asset_registry.create(
        project_id,
        "STYLE",
        name,
        canonical_metadata={
            "constraints": ["muted cyan shadows", "matte illustrated edge treatment"],
            "world_rules": ["all scenes use the locked illustrated rendering language"],
        },
    )
    version = container.asset_registry.add_version(asset.id, primary_media_asset_id=media.id)
    container.asset_registry.promote(asset.id, version.id, reason="user approved style")
    return asset, version, media


def _lock(container, project_id: str, version_id: str):  # type: ignore[no-untyped-def]
    email = f"style-lock-{project_id}-{version_id}@example.com"
    with container.database.session() as session:
        # Reused rather than recreated, so a test may call this twice for the
        # same version — a refused lock followed by a successful retry.
        actor = session.scalar(select(User).where(User.email == email))
        if actor is None:
            actor = User(email=email, display_name="Style Lock Owner")
            session.add(actor)
            session.flush()
        actor_id = actor.id
    return container.styles.lock(
        project_id,
        version_id,
        locked_by_user_id=actor_id,
        reason="用户确认整部作品使用这一版画风",
        explicit_confirmation=True,
    )


def _candidate_with_output(container, project_id: str, payload: bytes) -> str:  # type: ignore[no-untyped-def]
    shot_id = _shot(container, project_id)
    output = _media(container, project_id, payload, "candidate-output.png")
    with container.database.session() as session:
        candidate = GenerationCandidate(
            shot_id=shot_id,
            attempt_number=1,
            output_asset_id=output.id,
            status=CandidateStatus.VALIDATING.value,
        )
        session.add(candidate)
        session.flush()
        return candidate.id


def _shot(container, project_id: str) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(project_id=project_id, title="Style locked", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Rainy platform")
        session.add(scene)
        session.flush()
        start = TimelineState(
            project_id=project_id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_INPUT",
            state_json={"lighting": {"contrast": "soft"}},
        )
        end = TimelineState(
            project_id=project_id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_OUTPUT",
            state_json={"lighting": {"contrast": "soft"}},
        )
        session.add_all([start, end])
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            sequence=1,
            prompt="A woman opens the station door once.",
            user_prompt="A woman opens the station door once.",
            input_state_id=start.id,
            output_state_id=end.id,
            preferred_provider="google_flow",
            preferred_model="flow-veo-3.1",
        )
        session.add(shot)
        session.flush()
        start.shot_id = shot.id
        end.shot_id = shot.id
        return shot.id


def test_style_embedding_is_version_bound_and_project_lock_is_one_time(container, project):  # type: ignore[no-untyped-def]
    asset, version, media = _style_version(container, project.id, _png((20, 50, 90)))
    embedding = container.styles.ensure_embedding(version.id)
    replay = container.styles.ensure_embedding(version.id)
    assert replay.id == embedding.id
    assert embedding.asset_version_id == version.id
    assert embedding.dimension == 64
    assert len(embedding.embedding) == 64
    assert embedding.source_media_ids == [media.id]
    assert len(embedding.embedding_hash) == 64

    with pytest.raises(IntegrityError):
        with container.database.session() as session:
            session.execute(
                update(Project).where(Project.id == project.id).values(canonical_style_version_id=version.id)
            )

    style_lock = _lock(container, project.id, version.id)
    assert style_lock.style_embedding_id == embedding.id
    with container.database.session() as session:
        assert session.get(Project, project.id).canonical_style_version_id == version.id
    with pytest.raises(IntegrityError):
        with container.database.session() as session:
            session.execute(
                update(StyleEmbedding)
                .where(StyleEmbedding.id == embedding.id)
                .values(embedding_hash="f" * 64)
            )

    replacement_media = _media(container, project.id, _png((240, 210, 30)), "replacement.png")
    replacement = container.asset_registry.add_version(
        asset.id,
        primary_media_asset_id=replacement_media.id,
        parent_version_id=version.id,
    )
    container.asset_registry.promote(asset.id, replacement.id, reason="asset library canonical changed")
    with pytest.raises(StyleLockConflict, match="already locked"):
        _lock(container, project.id, replacement.id)
    with container.database.session() as session:
        assert session.get(Project, project.id).canonical_style_version_id == version.id


def test_locked_style_is_inherited_by_prompt_references_and_adapter_payload(container, project):  # type: ignore[no-untyped-def]
    asset, locked_version, locked_media = _style_version(
        container,
        project.id,
        _png((15, 45, 85), stripes=True),
    )
    _lock(container, project.id, locked_version.id)
    replacement_media = _media(container, project.id, _png((240, 210, 30)), "warm-style.png")
    replacement = container.asset_registry.add_version(
        asset.id,
        primary_media_asset_id=replacement_media.id,
        parent_version_id=locked_version.id,
    )
    container.asset_registry.promote(asset.id, replacement.id, reason="library canonical revision")
    shot_id = _shot(container, project.id)

    prepared = container.visual_runtime.prepare_autopilot(
        shot_id,
        idempotency_key="locked-style-inheritance",
        allowed_providers=["google_flow"],
    )

    assert prepared.shot_spec.style_lock["version_id"] == locked_version.id
    assert prepared.request.metadata["style_lock"]["version_id"] == locked_version.id
    assert locked_media.id in prepared.request.reference_asset_ids
    assert replacement_media.id not in prepared.request.reference_asset_ids
    assert prepared.request.provider_payload == prepared.model_request.payload
    style_control = prepared.model_request.payload["style_control"]
    assert style_control["version_id"] == locked_version.id
    assert len(style_control["embedding"]) == 64
    assert "Locked visual style" in prepared.model_request.prompt
    assert "visual style drift" in prepared.model_request.negative_prompt


def test_style_lock_reaches_the_prompt_from_any_caller_of_compile(container, project):  # type: ignore[no-untyped-def]
    """Enforcement lives in the compiler, not in one caller's context dict.

    `prepare_autopilot` used to be the only path that merged `style_lock` into
    its canonical assets, so every other caller of `compile()` produced a spec
    with no style lock at all — a wrong look that passed every check. The
    compiler now resolves the project's lock itself.
    """

    _asset, locked_version, _media = _style_version(container, project.id, _png((15, 45, 85), stripes=True))
    _lock(container, project.id, locked_version.id)
    shot_id = _shot(container, project.id)

    # Compiled directly, with no canonical assets and no style_lock key at all.
    compiled = container.prompts.compile(shot_id)

    assert compiled.spec.style_lock["version_id"] == locked_version.id
    assert any("locked visual style" in item.lower() for item in compiled.spec.constraints)


def test_a_caller_supplied_style_lock_cannot_override_the_authoritative_one(container, project):  # type: ignore[no-untyped-def]
    """A stale lock in a caller's context must never win.

    A prompt compiled against a superseded style would render the wrong look
    while satisfying every downstream check, so the authoritative lock is the
    only one that reaches the spec.
    """

    _asset, locked_version, _media = _style_version(container, project.id, _png((15, 45, 85), stripes=True))
    _lock(container, project.id, locked_version.id)
    shot_id = _shot(container, project.id)

    compiled = container.prompts.compile(
        shot_id,
        canonical_assets=[
            {"id": "stale-asset", "type": "STYLE", "style_lock": {"version_id": "stale-version-id"}}
        ],
    )

    assert compiled.spec.style_lock["version_id"] == locked_version.id


def test_a_project_without_a_lock_compiles_with_no_style_lock(container, project):  # type: ignore[no-untyped-def]
    shot_id = _shot(container, project.id)
    compiled = container.prompts.compile(shot_id)
    assert compiled.spec.style_lock == {}


def test_style_similarity_failure_is_persisted_and_blocks_commit(container, project):  # type: ignore[no-untyped-def]
    _asset, version, _reference = _style_version(container, project.id, _png((10, 30, 70)))
    _lock(container, project.id, version.id)
    shot_id = _shot(container, project.id)
    divergent = _media(container, project.id, _png((245, 220, 35), stripes=True), "divergent.png")
    with container.database.session() as session:
        candidate = GenerationCandidate(
            shot_id=shot_id,
            attempt_number=1,
            output_asset_id=divergent.id,
            status=CandidateStatus.PASSED.value,
        )
        session.add(candidate)
        session.flush()
        qa = QAResult(
            candidate_id=candidate.id,
            decision=QADecision.PASS.value,
            overall_score=1.0,
        )
        session.add(qa)
        session.flush()
        candidate.qa_result_id = qa.id
        candidate_id = candidate.id

    evaluation = container.styles.evaluate_candidate(candidate_id)
    assert evaluation is not None
    assert evaluation.status == "FAIL"
    assert "STYLE_SIMILARITY_TOO_LOW" in evaluation.reason_codes
    with container.database.session() as session:
        persisted = session.scalar(
            select(CandidateStyleEvaluation).where(CandidateStyleEvaluation.candidate_id == candidate_id)
        )
        assert persisted.id == evaluation.id
        assert persisted.sample_scores

    with pytest.raises(CandidateNotCommittable, match="locked-style"):
        container.candidates.commit(candidate_id)


def test_style_embedding_rejects_non_style_versions(container, project):  # type: ignore[no-untyped-def]
    media = _media(container, project.id, _png((20, 40, 80)), "scene.png")
    asset = container.asset_registry.create(project.id, "SCENE", "Not a style")
    version = container.asset_registry.add_version(asset.id, primary_media_asset_id=media.id)
    with pytest.raises(ValueError, match="STYLE asset version"):
        container.styles.ensure_embedding(version.id)
    with container.database.session() as session:
        assert session.scalar(select(AssetVersion).where(AssetVersion.id == version.id)) is not None


def test_authenticated_api_promotes_extracts_and_locks_project_style(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        issued = client.post(
            "/api/auth/register",
            json={
                "email": "style-owner@example.com",
                "password": "correct horse battery staple",
                "display_name": "Style Owner",
            },
        ).json()
        headers = {"Authorization": f"Bearer {issued['access_token']}"}
        project_response = client.post(
            "/v1/projects",
            headers=headers,
            json={"title": "Locked Style API"},
        )
        assert project_response.status_code == 200
        project_id = project_response.json()["id"]
        media = _media(container, project_id, _png((18, 48, 88), stripes=True), "api-style.png")
        asset = client.post(
            "/api/assets",
            headers=headers,
            json={"project_id": project_id, "asset_type": "STYLE", "name": "冷青插画"},
        ).json()
        version = client.post(
            f"/api/assets/{asset['id']}/versions",
            headers=headers,
            json={"primary_media_asset_id": media.id},
        ).json()
        promoted = client.post(
            f"/api/assets/{asset['id']}/versions/{version['id']}/promote",
            headers=headers,
            json={"reason": "用户确认这一版为正式画风"},
        )
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["style_embedding"]["dimension"] == 64

        locked = client.post(
            f"/api/projects/{project_id}/style-lock",
            headers=headers,
            json={
                "style_version_id": version["id"],
                "reason": "用户确认整部作品使用冷青插画风格",
                "explicit_confirmation": True,
            },
        )
        assert locked.status_code == 200, locked.text
        assert locked.json()["locked"] is True
        assert locked.json()["style_embedding"]["dimension"] == 64
        assert (
            client.get(
                f"/api/projects/{project_id}/style-lock",
                headers=headers,
            ).json()["style_version_id"]
            == version["id"]
        )


# --- The second style layer -------------------------------------------------
#
# The deterministic descriptor is a histogram of colour, tone, saturation, edge
# and spatial statistics. It catches a regrade. It cannot distinguish an oil
# painting from a 3D render of the same scene under the same palette, because
# medium lives in texture statistics it never samples. Layer 2 sees exactly
# that, and is correspondingly blind to the regrades layer 1 catches — so both
# run and the worse verdict wins.


class _StubSemanticEmbedder:
    """A deterministic stand-in for the multimodal model, with no network."""

    version = "stub-semantic-v1"

    normalization = "L2"
    distance_metric = "cosine"

    def __init__(self, vectors=None, *, fail: bool = False, model_revision: str = ""):
        self.model = "stub/semantic-style"
        self.provider = "stub"
        self.model_revision = model_revision
        self._vectors = vectors
        self.fail = fail
        self.calls = 0
        self._dimension = 0

    def space_identity(self):  # type: ignore[no-untyped-def]
        from style_core import EmbeddingSpaceIdentity, SemanticStyleUnavailable

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
        from style_core import SemanticStyleUnavailable

        self.calls += 1
        if self.fail:
            raise SemanticStyleUnavailable("stub is offline")
        vectors = (
            [list(self._vectors) for _ in images]
            if self._vectors is not None
            else [[1.0, 0.0, 0.0, 0.0] for _ in images]
        )
        self._dimension = len(vectors[0])
        return vectors


def test_a_project_locked_without_a_semantic_layer_keeps_the_single_gate(container, project):  # type: ignore[no-untyped-def]
    from production_domain.models import ProjectStyleLock
    from sqlalchemy import select as sa_select

    _asset, version, _media = _style_version(container, project.id, _png((12, 40, 80)))
    _lock(container, project.id, version.id)

    with container.database.session() as session:
        lock = session.scalar(sa_select(ProjectStyleLock).where(ProjectStyleLock.project_id == project.id))
        assert lock.semantic_style_embedding_id is None


def test_locking_with_a_semantic_embedder_binds_a_second_reference(container, project):  # type: ignore[no-untyped-def]
    from production_domain.models import ProjectStyleLock, StyleEmbedding
    from sqlalchemy import select as sa_select

    embedder = _StubSemanticEmbedder()
    container.styles.semantic = embedder
    _asset, version, _media_asset = _style_version(container, project.id, _png((12, 40, 80)))
    _lock(container, project.id, version.id)

    with container.database.session() as session:
        lock = session.scalar(sa_select(ProjectStyleLock).where(ProjectStyleLock.project_id == project.id))
        assert lock.semantic_style_embedding_id is not None
        semantic = session.get(StyleEmbedding, lock.semantic_style_embedding_id)
        assert semantic.evidence_kind == "MODEL_SEMANTIC"
        assert semantic.model == "stub/semantic-style"
        # The two layers describe the same version, never different frames.
        assert semantic.asset_version_id == lock.style_version_id
        deterministic = session.get(StyleEmbedding, lock.style_embedding_id)
        assert deterministic.id != semantic.id
        assert deterministic.evidence_kind == "DETERMINISTIC_LOCAL"
    assert embedder.calls == 1


def _lock_two_layer(container, project, embedder):  # type: ignore[no-untyped-def]
    """Lock a project under both layers and return the service that holds them."""

    container.styles.semantic = embedder
    _asset, version, _media_asset = _style_version(container, project.id, _png((12, 40, 80)))
    _lock(container, project.id, version.id)
    return container.styles


def test_a_semantic_mismatch_fails_a_candidate_the_histogram_accepted(container, project):  # type: ignore[no-untyped-def]
    """The case layer 1 structurally cannot see: same palette, wrong medium."""

    service = _lock_two_layer(container, project, _StubSemanticEmbedder([1.0, 0.0, 0.0, 0.0]))
    candidate_id = _candidate_with_output(container, project.id, _png((12, 40, 80)))

    # Same frame, so layer 1 scores near 1.0; the semantic model reports a
    # different rendering medium.
    service.semantic = _StubSemanticEmbedder([0.0, 1.0, 0.0, 0.0])
    evaluation = service.evaluate_candidate(candidate_id)

    assert evaluation.average_similarity is not None and evaluation.average_similarity > 0.9
    assert evaluation.semantic_status == "FAIL"
    assert evaluation.status == "FAIL"
    assert "STYLE_SEMANTIC_SIMILARITY_TOO_LOW" in evaluation.reason_codes
    assert evaluation.evidence_kind == "DETERMINISTIC_LOCAL+MODEL_SEMANTIC"


def test_both_layers_agreeing_passes_and_records_each_verdict(container, project):  # type: ignore[no-untyped-def]
    service = _lock_two_layer(container, project, _StubSemanticEmbedder([1.0, 0.0, 0.0, 0.0]))
    candidate_id = _candidate_with_output(container, project.id, _png((12, 40, 80)))

    evaluation = service.evaluate_candidate(candidate_id)

    assert evaluation.semantic_status == "PASS"
    assert evaluation.status == "PASS"
    assert evaluation.semantic_average_similarity is not None
    assert evaluation.metrics_json["deterministic_status"] == "PASS"


def test_an_unavailable_semantic_model_sends_the_candidate_to_review_not_through(container, project):  # type: ignore[no-untyped-def]
    """A missing second opinion is not a passing one."""

    service = _lock_two_layer(container, project, _StubSemanticEmbedder([1.0, 0.0, 0.0, 0.0]))
    candidate_id = _candidate_with_output(container, project.id, _png((12, 40, 80)))

    service.semantic = _StubSemanticEmbedder(fail=True)
    evaluation = service.evaluate_candidate(candidate_id)

    assert evaluation.semantic_status == "REVIEW_REQUIRED"
    assert evaluation.status == "REVIEW_REQUIRED"
    assert "STYLE_SEMANTIC_MODEL_UNAVAILABLE" in evaluation.reason_codes


def test_a_lock_with_a_semantic_layer_is_not_evaluated_by_layer_one_alone(container, project):  # type: ignore[no-untyped-def]
    """Losing the embedder must not quietly weaken a gate already in force."""

    service = _lock_two_layer(container, project, _StubSemanticEmbedder([1.0, 0.0, 0.0, 0.0]))
    candidate_id = _candidate_with_output(container, project.id, _png((12, 40, 80)))

    service.semantic = None
    evaluation = service.evaluate_candidate(candidate_id)

    assert evaluation.semantic_status == "REVIEW_REQUIRED"
    assert evaluation.status == "REVIEW_REQUIRED"
    assert "STYLE_SEMANTIC_EMBEDDER_NOT_CONFIGURED" in evaluation.reason_codes


def test_a_locked_embedding_records_the_space_it_belongs_to(container, project):  # type: ignore[no-untyped-def]
    """Both layers, because both are compared and either can move."""

    from production_domain.models import ProjectStyleLock, StyleEmbedding
    from style_core import EmbeddingSpaceIdentity

    embedder = _StubSemanticEmbedder(model_revision="stub-2026-08")
    container.styles.semantic = embedder
    _asset, version, _media_asset = _style_version(container, project.id, _png((12, 40, 80)))
    locked = _lock(container, project.id, version.id)

    with container.database.session() as session:
        stored = session.get(ProjectStyleLock, locked.id)
        layer_one = session.get(StyleEmbedding, stored.style_embedding_id)
        layer_two = session.get(StyleEmbedding, stored.semantic_style_embedding_id)

        assert EmbeddingSpaceIdentity.from_embedding(layer_one) == EmbeddingSpaceIdentity(
            provider="LOCAL_DETERMINISTIC",
            model="visual-style-descriptor-64d",
            model_revision="",
            input_schema_version="style-descriptor-v1",
            dimension=64,
            normalization="L2",
            distance_metric="cosine",
        )
        assert EmbeddingSpaceIdentity.from_embedding(layer_two) == EmbeddingSpaceIdentity(
            provider="stub",
            model="stub/semantic-style",
            model_revision="stub-2026-08",
            input_schema_version="stub-semantic-v1",
            dimension=4,
            normalization="L2",
            distance_metric="cosine",
        )


def test_a_candidate_from_a_different_semantic_space_is_never_scored(container, project):  # type: ignore[no-untyped-def]
    """A model swap behind a stable id must not produce a confident verdict.

    Cosine over vectors from two unrelated spaces does not raise; it returns a
    plausible number. The only way that becomes visible is by comparing the
    spaces before comparing the vectors.
    """

    embedder = _StubSemanticEmbedder(vectors=[1.0, 0.0, 0.0, 0.0], model_revision="rev-A")
    service = _lock_two_layer(container, project, embedder)
    candidate_id = _candidate_with_output(container, project.id, _png((12, 40, 80)))

    # Same model id, same dimensions, different revision — the shape a silent
    # provider-side model swap actually has.
    embedder.model_revision = "rev-B"
    evaluation = service.evaluate_candidate(candidate_id)

    assert evaluation.status == "REVIEW_REQUIRED"
    moved = [code for code in evaluation.reason_codes if code.startswith("STYLE_SEMANTIC_EMBEDDING_SPACE")]
    assert moved and moved[0].endswith("model_revision")
    # Refused, not scored low: a meaningless comparison has no score at all.
    assert evaluation.semantic_average_similarity is None


def test_a_moved_local_descriptor_is_refused_rather_than_compared(container, project):  # type: ignore[no-untyped-def]
    """Layer 1 has a space too, and it is the one that ships in every lock."""

    _asset, version, _media_asset = _style_version(container, project.id, _png((12, 40, 80)))
    _lock(container, project.id, version.id)
    candidate_id = _candidate_with_output(container, project.id, _png((12, 40, 80)))

    original = type(container.styles.descriptor).version
    type(container.styles.descriptor).version = "style-descriptor-v2"
    try:
        evaluation = container.styles.evaluate_candidate(candidate_id)
    finally:
        type(container.styles.descriptor).version = original

    assert evaluation.status == "REVIEW_REQUIRED"
    moved = [code for code in evaluation.reason_codes if code.startswith("STYLE_EMBEDDING_SPACE_CHANGED")]
    assert moved and moved[0].endswith("input_schema_version")
    assert evaluation.average_similarity is None
    assert evaluation.sample_scores == []


def test_a_reference_from_a_stale_space_cannot_be_reused_to_lock(container, project):  # type: ignore[no-untyped-def]
    """The lock must bind the space the embedder produces now, not a stored one.

    Reference rows are found by model id, which a revision bump does not change.
    Reusing one would bind layer 2 to a space no candidate will ever be in.
    """

    from style_core import SemanticStyleLayerRequired

    embedder = _StubSemanticEmbedder(model_revision="rev-A")
    container.styles.semantic = embedder
    _asset, version, _media_asset = _style_version(container, project.id, _png((12, 40, 80)))
    container.styles.ensure_semantic_embedding(version.id)

    embedder.model_revision = "rev-B"
    with pytest.raises(SemanticStyleLayerRequired) as refused:
        _lock(container, project.id, version.id)
    assert refused.value.reason.startswith("SEMANTIC_EMBEDDING_SPACE_CHANGED")
    assert refused.value.retryable is False


def test_space_identity_names_every_field_that_moved():
    from style_core import EmbeddingSpaceIdentity

    base = EmbeddingSpaceIdentity(
        provider="openrouter",
        model="google/gemini-embedding-2",
        model_revision="",
        input_schema_version="semantic-style-embedder-v1",
        dimension=1024,
        normalization="L2",
        distance_metric="cosine",
    )
    assert base.differences(base) == []
    from dataclasses import replace

    assert base.differences(replace(base, dimension=3072)) == ["dimension"]
    assert base.differences(replace(base, distance_metric="dot")) == ["distance_metric"]
    assert base.differences(replace(base, model="voyageai/voyage-multimodal-3.5", dimension=1)) == [
        "model",
        "dimension",
    ]


def test_the_worse_layer_verdict_always_wins():
    from style_core.service import _worst_status

    assert _worst_status("PASS", None) == "PASS"
    assert _worst_status("PASS", "PASS") == "PASS"
    assert _worst_status("PASS", "REVIEW_REQUIRED") == "REVIEW_REQUIRED"
    assert _worst_status("PASS", "FAIL") == "FAIL"
    assert _worst_status("FAIL", "PASS") == "FAIL"
    assert _worst_status("REVIEW_REQUIRED", "PASS") == "REVIEW_REQUIRED"


def test_an_enabled_layer_two_that_cannot_run_refuses_the_lock(container, project):  # type: ignore[no-untyped-def]
    """A transient outage must not permanently downgrade a project's gate.

    The lock is append-only and a trigger forbids re-locking, so a single-layer
    lock written while layer 2 happened to be unreachable would be the last word
    on how every candidate in the project is judged — and would look identical
    to one made deliberately with the feature off.

    Refusing costs a retry. Degrading costs the second gate for the life of the
    project.
    """

    from production_domain.models import Project, ProjectStyleLock
    from sqlalchemy import select as sa_select
    from style_core import SemanticStyleLayerRequired

    embedder = _StubSemanticEmbedder(fail=True)
    container.styles.semantic = embedder
    _asset, version, _media_asset = _style_version(container, project.id, _png((12, 40, 80)))

    with pytest.raises(SemanticStyleLayerRequired) as refused:
        _lock(container, project.id, version.id)
    assert refused.value.reason.startswith("SEMANTIC_MODEL_UNAVAILABLE")
    assert refused.value.retryable is True

    # Nothing was written, so the project is still lockable rather than stuck.
    with container.database.session() as session:
        assert (
            session.scalar(sa_select(ProjectStyleLock).where(ProjectStyleLock.project_id == project.id))
            is None
        )
        assert session.get(Project, project.id).canonical_style_version_id is None

    # And the retry, once the model answers, produces the two-layer lock that
    # was asked for in the first place.
    embedder.fail = False
    locked = _lock(container, project.id, version.id)
    assert locked.semantic_style_embedding_id is not None
    with container.database.session() as session:
        stored = session.get(ProjectStyleLock, locked.id)
        assert stored.metadata_json["style_layers"] == 2
        assert stored.metadata_json["semantic_layer_absent_reason"] is None


def test_unreadable_reference_media_is_not_reported_as_retryable(container, project):  # type: ignore[no-untyped-def]
    """Refusal has two causes and only one of them is worth waiting out."""

    from style_core import SemanticStyleLayerRequired
    from style_core.service import SemanticReferenceAttempt

    container.styles.semantic = _StubSemanticEmbedder()
    _asset, version, _media_asset = _style_version(container, project.id, _png((12, 40, 80)))

    original = type(container.styles).semantic_reference
    type(container.styles).semantic_reference = lambda self, version_id: SemanticReferenceAttempt(  # type: ignore[method-assign]
        None, "SEMANTIC_REFERENCE_MEDIA_UNREADABLE"
    )
    try:
        with pytest.raises(SemanticStyleLayerRequired) as refused:
            _lock(container, project.id, version.id)
    finally:
        type(container.styles).semantic_reference = original  # type: ignore[method-assign]
    assert refused.value.reason == "SEMANTIC_REFERENCE_MEDIA_UNREADABLE"
    assert refused.value.retryable is False


def test_the_lock_endpoint_answers_503_only_when_a_retry_could_help(container):  # type: ignore[no-untyped-def]
    """The status code has to tell the user whether waiting is worth anything."""

    from style_core.service import SemanticReferenceAttempt

    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        issued = client.post(
            "/api/auth/register",
            json={
                "email": "style-503@example.com",
                "password": "correct horse battery staple",
                "display_name": "Style Owner",
            },
        ).json()
        headers = {"Authorization": f"Bearer {issued['access_token']}"}
        project_id = client.post(
            "/v1/projects", headers=headers, json={"title": "Fail Closed API"}
        ).json()["id"]
        media = _media(container, project_id, _png((18, 48, 88), stripes=True), "api-style.png")
        asset = client.post(
            "/api/assets",
            headers=headers,
            json={"project_id": project_id, "asset_type": "STYLE", "name": "冷青插画"},
        ).json()
        version = client.post(
            f"/api/assets/{asset['id']}/versions",
            headers=headers,
            json={"primary_media_asset_id": media.id},
        ).json()
        client.post(
            f"/api/assets/{asset['id']}/versions/{version['id']}/promote",
            headers=headers,
            json={"reason": "用户确认这一版为正式画风"},
        )
        body = {
            "style_version_id": version["id"],
            "reason": "用户确认整部作品使用冷青插画风格",
            "explicit_confirmation": True,
        }

        embedder = _StubSemanticEmbedder(fail=True)
        container.styles.semantic = embedder
        unavailable = client.post(f"/api/projects/{project_id}/style-lock", headers=headers, json=body)
        assert unavailable.status_code == 503, unavailable.text
        assert "semantic layer" in unavailable.json()["detail"]

        original = type(container.styles).semantic_reference
        type(container.styles).semantic_reference = (  # type: ignore[method-assign]
            lambda self, version_id: SemanticReferenceAttempt(None, "SEMANTIC_REFERENCE_MEDIA_UNREADABLE")
        )
        try:
            unreadable = client.post(f"/api/projects/{project_id}/style-lock", headers=headers, json=body)
        finally:
            type(container.styles).semantic_reference = original  # type: ignore[method-assign]
        assert unreadable.status_code == 409, unreadable.text

        # Neither refusal consumed the project's one chance to lock.
        embedder.fail = False
        locked = client.post(f"/api/projects/{project_id}/style-lock", headers=headers, json=body)
        assert locked.status_code == 200, locked.text
        assert locked.json()["locked"] is True


def test_the_feature_switched_off_still_makes_a_deliberate_single_layer_lock(container, project):  # type: ignore[no-untyped-def]
    """Fail-closed applies to the layer being *enabled*, not to its absence.

    With `FEATURE_SEMANTIC_STYLE_LOCK=false` there is no embedder at all, a
    single-layer lock is the intended outcome, and the lock says so.
    """

    from production_domain.models import ProjectStyleLock

    assert container.styles.semantic is None
    _asset, version, _media_asset = _style_version(container, project.id, _png((12, 40, 80)))
    locked = _lock(container, project.id, version.id)

    with container.database.session() as session:
        stored = session.get(ProjectStyleLock, locked.id)
        assert stored.semantic_style_embedding_id is None
        assert stored.metadata_json["style_layers"] == 1
        assert stored.metadata_json["semantic_layer_absent_reason"] == "SEMANTIC_EMBEDDER_NOT_CONFIGURED"


def test_a_two_layer_lock_says_so(container, project):  # type: ignore[no-untyped-def]
    from production_domain.models import ProjectStyleLock
    from sqlalchemy import select as sa_select

    _lock_two_layer(container, project, _StubSemanticEmbedder([1.0, 0.0, 0.0, 0.0]))

    with container.database.session() as session:
        lock = session.scalar(sa_select(ProjectStyleLock).where(ProjectStyleLock.project_id == project.id))
        assert lock.metadata_json["style_layers"] == 2
        assert lock.metadata_json["semantic_layer_absent_reason"] is None
