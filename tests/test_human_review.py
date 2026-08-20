from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from production_domain.models import (
    CandidateStatus,
    DecisionRecord,
    Episode,
    GenerationCandidate,
    Project,
    QADecision,
    QAResult,
    Scene,
    Shot,
    TimelineState,
    User,
    Workspace,
    WorkspaceMembership,
)
from sqlalchemy import func, select
from video_platform_api.main import create_app


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (3, 3), color).save(output, format="PNG")
    return output.getvalue()


def _register(client: TestClient, email: str) -> dict:
    response = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "correct horse battery staple",
            "display_name": email.split("@", 1)[0],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _headers(auth: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth['access_token']}"}


def _setup_candidate(container, project_id: str):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(project_id=project_id, title="Pilot", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Room")
        session.add(scene)
        session.flush()
        input_state = TimelineState(
            project_id=project_id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_INPUT",
        )
        output_state = TimelineState(
            project_id=project_id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_OUTPUT",
            state_json={},
        )
        session.add_all([input_state, output_state])
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            sequence=1,
            prompt="Lin turns once.",
            user_prompt="Lin turns once.",
            compiled_prompt="Lin turns once.",
            input_state_id=input_state.id,
            output_state_id=output_state.id,
        )
        session.add(shot)
        session.flush()
        shot_id = shot.id

    output_asset = container.media.register(
        project_id,
        "VIDEO",
        io.BytesIO(_png_bytes((20, 100, 210))),
        filename="candidate.png",
        mime_type="image/png",
        shot_id=shot_id,
    )[0]
    with container.database.session() as session:
        candidate = GenerationCandidate(
            shot_id=shot_id,
            attempt_number=1,
            output_asset_id=output_asset.id,
            status=CandidateStatus.VALIDATING.value,
        )
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id
    end_frame = container.media.register(
        project_id,
        "END_FRAME",
        io.BytesIO(_png_bytes((21, 101, 211))),
        filename="end-frame.png",
        mime_type="image/png",
        shot_id=shot_id,
        parent_asset_id=output_asset.id,
        generation_candidate_id=candidate_id,
    )[0]
    return shot_id, candidate_id, end_frame


def _mark_review_required(client: TestClient, auth: dict, shot_id: str, candidate_id: str) -> None:
    response = client.post(
        f"/v1/shots/{shot_id}/candidates/{candidate_id}/validate",
        headers=_headers(auth),
        json={"evidence": {}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == CandidateStatus.USER_REVIEW_REQUIRED.value


def test_real_editor_can_approve_human_review_then_must_commit_separately(container, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        owner = _register(client, "human-review-owner@example.com")
        project_id = client.post(
            "/v1/projects",
            headers=_headers(owner),
            json={"title": "Human review"},
        ).json()["id"]
        shot_id, candidate_id, end_frame = _setup_candidate(container, project_id)
        _mark_review_required(client, owner, shot_id, candidate_id)

        response = client.post(
            f"/v1/shots/{shot_id}/candidates/{candidate_id}/human-review",
            headers=_headers(owner),
            json={
                "reason": "我已查看完整结果，人物、场景和主要动作符合本镜要求。",
                "explicit_confirmation": True,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == CandidateStatus.PASSED.value
        assert response.json()["qa"]["decision"] == QADecision.PASS.value
        assert response.json()["qa"]["profile"] == "HUMAN_REVIEW"
        assert response.json()["qa"]["human_review"]["source"] == "USER_EXPLICIT_CONFIRMATION"

        repeated = client.post(
            f"/v1/shots/{shot_id}/candidates/{candidate_id}/human-review",
            headers=_headers(owner),
            json={"reason": "尝试重复批准", "explicit_confirmation": True},
        )
        assert repeated.status_code == 409

        with container.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            shot = session.get(Shot, shot_id)
            reviews = list(
                session.scalars(
                    select(QAResult)
                    .where(QAResult.candidate_id == candidate_id)
                    .order_by(QAResult.created_at)
                )
            )
            decision = session.scalar(
                select(DecisionRecord).where(
                    DecisionRecord.shot_id == shot_id,
                    DecisionRecord.decision_type == "HUMAN_REVIEW",
                )
            )
            assert [item.decision for item in reviews] == [
                QADecision.USER_REVIEW_REQUIRED.value,
                QADecision.PASS.value,
            ]
            assert reviews[-1].id == candidate.qa_result_id
            assert reviews[-1].metrics_json["reviewer_user_id"] == owner["user"]["id"]
            assert reviews[-1].metrics_json["reason"] == response.json()["qa"]["human_review"]["reason"]
            assert decision.input_features["reviewer_user_id"] == owner["user"]["id"]
            assert decision.input_features["source"] == "USER_EXPLICIT_CONFIRMATION"
            assert decision.selected_action == "APPROVE_FOR_COMMIT"
            assert shot.committed_candidate_id is None
            assert candidate.accepted_by is None

        monkeypatch.setattr(
            container.continuity,
            "extract_end_frame",
            lambda _shot_id, _asset_id: end_frame,
        )
        committed = client.post(
            f"/v1/shots/{shot_id}/candidates/{candidate_id}/commit",
            headers=_headers(owner),
            json={},
        )
        assert committed.status_code == 200, committed.text
        assert committed.json()["status"] == CandidateStatus.COMMITTED.value
        with container.database.session() as session:
            assert session.get(GenerationCandidate, candidate_id).accepted_by == owner["user"]["id"]


def test_human_review_requires_reason_confirmation_real_user_and_write_role(container) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    app = create_app(container)
    with TestClient(app) as client:
        owner = _register(client, "review-rbac-owner@example.com")
        project_id = client.post(
            "/v1/projects", headers=_headers(owner), json={"title": "Review RBAC"}
        ).json()["id"]
        shot_id, candidate_id, _end_frame = _setup_candidate(container, project_id)
        _mark_review_required(client, owner, shot_id, candidate_id)

        for body in (
            {"reason": "", "explicit_confirmation": True},
            {"reason": "   ", "explicit_confirmation": True},
            {"reason": "我已完成检查", "explicit_confirmation": False},
            {"reason": "我已完成检查", "explicit_confirmation": "true"},
            {
                "reason": "尝试同时上报分数",
                "explicit_confirmation": True,
                "character_score": 1,
            },
        ):
            response = client.post(
                f"/v1/shots/{shot_id}/candidates/{candidate_id}/human-review",
                headers=_headers(owner),
                json=body,
            )
            assert response.status_code in {400, 422}

        viewer = _register(client, "review-viewer@example.com")
        with container.database.session() as session:
            project = session.get(Project, project_id)
            viewer_user = session.get(User, viewer["user"]["id"])
            viewer_workspace = session.scalar(
                select(Workspace).where(Workspace.owner_user_id == viewer_user.id)
            )
            viewer_workspace.status = "SUSPENDED"
            session.add(
                WorkspaceMembership(
                    workspace_id=project.workspace_id,
                    user_id=viewer_user.id,
                    role="VIEWER",
                )
            )
        denied = client.post(
            f"/v1/shots/{shot_id}/candidates/{candidate_id}/human-review",
            headers=_headers(viewer),
            json={"reason": "我无写权限", "explicit_confirmation": True},
        )
        assert denied.status_code == 403
        unauthenticated = client.post(
            f"/v1/shots/{shot_id}/candidates/{candidate_id}/human-review",
            json={"reason": "未登录", "explicit_confirmation": True},
        )
        assert unauthenticated.status_code == 401

    container.settings.auth_required = False
    with TestClient(create_app(container)) as development_client:
        bypassed = development_client.post(
            f"/v1/shots/{shot_id}/candidates/{candidate_id}/human-review",
            json={"reason": "开发模式不应代替真人复核", "explicit_confirmation": True},
        )
        assert bypassed.status_code == 403

    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        assert candidate.status == CandidateStatus.USER_REVIEW_REQUIRED.value
        assert (
            session.scalar(
                select(func.count(QAResult.id)).where(
                    QAResult.candidate_id == candidate_id,
                    QAResult.profile == "HUMAN_REVIEW",
                )
            )
            == 0
        )


@pytest.mark.parametrize(
    ("status", "decision"),
    [
        (CandidateStatus.SOFT_FAILED.value, QADecision.SOFT_FAIL.value),
        (CandidateStatus.HARD_FAILED.value, QADecision.HARD_FAIL.value),
        (CandidateStatus.REJECTED.value, QADecision.USER_REVIEW_REQUIRED.value),
        (CandidateStatus.COMMITTED.value, QADecision.PASS.value),
    ],
)
def test_human_review_never_overrides_forbidden_candidate_states(
    container,
    status: str,
    decision: str,
) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        owner = _register(client, f"forbidden-{status.lower()}@example.com")
        project_id = client.post(
            "/v1/projects", headers=_headers(owner), json={"title": f"Forbidden {status}"}
        ).json()["id"]
        shot_id, candidate_id, _end_frame = _setup_candidate(container, project_id)
        with container.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            prior = QAResult(
                candidate_id=candidate_id,
                profile="DIALOGUE",
                decision=decision,
                hard_failures=["CRITICAL_FAILURE"] if status == CandidateStatus.HARD_FAILED.value else [],
                metrics_json={},
            )
            session.add(prior)
            session.flush()
            candidate.qa_result_id = prior.id
            candidate.status = status

        response = client.post(
            f"/v1/shots/{shot_id}/candidates/{candidate_id}/human-review",
            headers=_headers(owner),
            json={"reason": "尝试越过不可批准状态", "explicit_confirmation": True},
        )
        assert response.status_code == 409

    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        assert candidate.status == status
        assert (
            session.scalar(
                select(func.count(QAResult.id)).where(
                    QAResult.candidate_id == candidate_id,
                    QAResult.profile == "HUMAN_REVIEW",
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(DecisionRecord.id)).where(
                    DecisionRecord.shot_id == shot_id,
                    DecisionRecord.decision_type == "HUMAN_REVIEW",
                )
            )
            == 0
        )
