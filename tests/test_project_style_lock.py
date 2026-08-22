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
    with container.database.session() as session:
        actor = User(
            email=f"style-lock-{project_id}-{version_id}@example.com",
            display_name="Style Lock Owner",
        )
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
    style_control = prepared.model_request.payload["style_control"]
    assert style_control["version_id"] == locked_version.id
    assert len(style_control["embedding"]) == 64
    assert "Locked visual style" in prepared.model_request.prompt
    assert "visual style drift" in prepared.model_request.negative_prompt


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
