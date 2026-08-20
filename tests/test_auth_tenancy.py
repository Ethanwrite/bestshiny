from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from PIL import Image
from platform_shared import Settings
from production_domain.models import (
    Asset,
    AssetCanonicalPromotion,
    AssetVersion,
    AuthSession,
    Character,
    Episode,
    GenerationCandidate,
    LegacyWorkspaceClaim,
    MediaAsset,
    Project,
    QAResult,
    Scene,
    Shot,
    User,
    Workspace,
    WorkspaceMembership,
)
from sqlalchemy import func, select
from starlette.websockets import WebSocketDisconnect
from video_platform_api.container import build_container
from video_platform_api.main import create_app


def _register(client: TestClient, email: str, password: str = "correct horse battery staple") -> dict:
    response = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": password,
            "display_name": email.split("@", 1)[0],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _headers(auth: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth['access_token']}"}


def _png_bytes(color: tuple[int, int, int] = (21, 120, 220)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(output, format="PNG")
    return output.getvalue()


def test_register_login_logout_store_only_password_and_token_hashes(container):
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        unauthenticated = client.post(
            "/api/pricing/estimate",
            json={"provider": "google_flow", "model": "NARWHAL", "media_type": "image"},
        )
        assert unauthenticated.status_code == 401

        issued = _register(client, "Owner@Example.com")
        token = issued["access_token"]
        assert client.get("/api/auth/me", headers=_headers(issued)).status_code == 200

        duplicate = client.post(
            "/api/auth/register",
            json={"email": "owner@example.com", "password": "another secure password"},
        )
        assert duplicate.status_code == 409

        with container.database.session() as session:
            user = session.scalar(select(User).where(User.email == "owner@example.com"))
            auth_session = session.scalar(select(AuthSession).where(AuthSession.user_id == user.id))
            membership = session.scalar(
                select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
            )
            assert user.password_hash != "correct horse battery staple"
            assert user.password_hash.startswith("pbkdf2_sha256$600000$")
            assert auth_session.token_hash == hashlib.sha256(token.encode()).hexdigest()
            assert token not in auth_session.token_hash
            assert membership.role == "OWNER"

        assert client.post("/api/auth/logout", headers=_headers(issued)).status_code == 204
        assert client.get("/api/auth/me", headers=_headers(issued)).status_code == 401

        logged_in = client.post(
            "/api/auth/login",
            json={"email": "OWNER@example.com", "password": "correct horse battery staple"},
        )
        assert logged_in.status_code == 200
        invalid = client.post(
            "/api/auth/login",
            json={"email": "owner@example.com", "password": "not the right password"},
        )
        assert invalid.status_code == 401


def test_project_and_runtime_routes_are_tenant_scoped(container, register_bytes):
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        alice = _register(client, "alice@example.com")
        alice_project = client.post(
            "/v1/projects",
            headers=_headers(alice),
            json={"title": "Alice Project"},
        )
        assert alice_project.status_code == 200

        bob = _register(client, "bob@example.com")
        bob_project = client.post(
            "/v1/projects",
            headers=_headers(bob),
            json={"title": "Bob Project"},
        )
        assert bob_project.status_code == 200
        bob_project_id = bob_project.json()["id"]
        with container.database.session() as session:
            bob_character = Character(project_id=bob_project_id, name="Bob Character")
            alice_episode = Episode(
                project_id=alice_project.json()["id"],
                title="Alice Episode",
                episode_number=1,
            )
            bob_episode = Episode(project_id=bob_project_id, title="Bob Episode", episode_number=1)
            session.add_all([bob_character, alice_episode, bob_episode])
            session.flush()
            alice_scene = Scene(episode_id=alice_episode.id, sequence=1, description="Alice set")
            bob_scene = Scene(episode_id=bob_episode.id, sequence=1, description="Bob set")
            session.add_all([alice_scene, bob_scene])
            session.flush()
            alice_shot = Shot(scene_id=alice_scene.id, sequence=1, prompt="One action")
            bob_shot = Shot(scene_id=bob_scene.id, sequence=1, prompt="One action")
            session.add_all([alice_shot, bob_shot])
            session.flush()
            bob_character_id = bob_character.id
            alice_shot_id = alice_shot.id
            bob_shot_id = bob_shot.id

        alice_projects = client.get("/v1/projects", headers=_headers(alice))
        assert [item["id"] for item in alice_projects.json()] == [alice_project.json()["id"]]
        assert client.get(f"/v1/projects/{bob_project_id}", headers=_headers(alice)).status_code == 403
        denied_generation = client.post(
            "/api/passenger/generate",
            headers=_headers(alice),
            json={
                "project_id": bob_project_id,
                "media_type": "image",
                "provider": "google_flow",
                "model": "NARWHAL",
                "prompt": "A product photograph",
                "idempotency_key": "cross-tenant-attempt",
            },
        )
        assert denied_generation.status_code == 403
        character_injection = client.post(
            f"/v1/shots/{alice_shot_id}/generate",
            headers=_headers(alice),
            json={"idempotency_key": "foreign-character", "character_ids": [bob_character_id]},
        )
        assert character_injection.status_code == 404

        lineage_injection = client.post(
            "/v1/assets",
            headers=_headers(alice),
            data={
                "project_id": alice_project.json()["id"],
                "asset_type": "REFERENCE",
                "shot_id": bob_shot_id,
            },
            files={"file": ("lineage.png", io.BytesIO(b"lineage"), "image/png")},
        )
        assert lineage_injection.status_code == 409

        alice_media = register_bytes(
            container,
            alice_project.json()["id"],
            "IMAGE",
            b"private-alice-media",
        )
        storage_path = f"/v1/storage/{alice_media.storage_key}"
        assert client.get(storage_path, headers=_headers(alice)).status_code == 200
        assert client.get(storage_path, headers=_headers(bob)).status_code == 403

        # Content-addressed keys can legitimately be shared by two tenant rows.
        bob_copy = register_bytes(
            container,
            bob_project_id,
            "IMAGE",
            b"private-alice-media",
        )
        assert bob_copy.storage_key == alice_media.storage_key
        assert client.get(storage_path, headers=_headers(bob)).status_code == 200

        reference_injection = client.post(
            f"/api/prompt/correct?project_id={alice_project.json()['id']}",
            headers=_headers(alice),
            json={"prompt": "A product", "reference_assets": [bob_copy.id]},
        )
        assert reference_injection.status_code == 409


def test_viewer_can_read_but_cannot_modify_project(container):
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        owner = _register(client, "owner-viewer-test@example.com")
        project = client.post(
            "/v1/projects",
            headers=_headers(owner),
            json={"title": "Shared Project"},
        ).json()
        viewer = _register(client, "viewer@example.com")
        with container.database.session() as session:
            owner_project = session.get(Project, project["id"])
            viewer_user = session.scalar(select(User).where(User.email == "viewer@example.com"))
            viewer_workspace = session.scalar(
                select(Workspace).where(Workspace.owner_user_id == viewer_user.id)
            )
            viewer_workspace.status = "SUSPENDED"
            session.add(
                WorkspaceMembership(
                    workspace_id=owner_project.workspace_id,
                    user_id=viewer_user.id,
                    role="VIEWER",
                )
            )

        assert client.get(f"/v1/projects/{project['id']}", headers=_headers(viewer)).status_code == 200
        denied = client.post(
            "/v1/episodes",
            headers=_headers(viewer),
            json={"project_id": project["id"], "title": "Pilot", "episode_number": 1},
        )
        assert denied.status_code == 403
        denied_project = client.post(
            "/v1/projects",
            headers=_headers(viewer),
            json={"title": "Viewer Must Not Create"},
        )
        assert denied_project.status_code == 403


def test_suspended_workspace_or_project_revokes_existing_session(container) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        owner = _register(client, "suspension-owner@example.com")
        project = client.post(
            "/v1/projects",
            headers=_headers(owner),
            json={"title": "Billable Project"},
        ).json()
        with container.database.session() as session:
            stored = session.get(Project, project["id"])
            stored.status = "SUSPENDED"
        assert client.get(f"/v1/projects/{project['id']}", headers=_headers(owner)).status_code == 403

        with container.database.session() as session:
            stored = session.get(Project, project["id"])
            workspace = session.get(Workspace, stored.workspace_id)
            stored.status = "ACTIVE"
            workspace.status = "SUSPENDED"
        assert client.get(f"/v1/projects/{project['id']}", headers=_headers(owner)).status_code == 403
        me = client.get("/api/auth/me", headers=_headers(owner))
        assert me.status_code == 200
        assert me.json()["workspaces"] == []
        denied_project = client.post(
            "/v1/projects",
            headers=_headers(owner),
            json={"title": "Suspended Must Not Create"},
        )
        assert denied_project.status_code == 403


def test_authenticated_upload_limit_returns_413_without_persisting_media(container) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    container.storage.max_object_bytes = 8  # type: ignore[attr-defined]
    with TestClient(create_app(container)) as client:
        owner = _register(client, "upload-limit-owner@example.com")
        project = client.post(
            "/v1/projects",
            headers=_headers(owner),
            json={"title": "Upload Limit"},
        ).json()
        response = client.post(
            "/v1/assets",
            headers=_headers(owner),
            data={"project_id": project["id"], "asset_type": "REFERENCE"},
            files={"file": ("too-large.png", io.BytesIO(b"123456789"), "image/png")},
        )

    assert response.status_code == 413
    with container.database.session() as session:
        assert session.scalar(select(func.count(MediaAsset.id))) == 0
    assert not list(Path(container.settings.storage_root).glob(".upload-*.tmp"))


def test_asset_audit_actor_is_server_derived_and_source_cannot_be_spoofed(container) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        owner = _register(client, "asset-audit-owner@example.com")
        project = client.post(
            "/v1/projects",
            headers=_headers(owner),
            json={"title": "Asset Audit"},
        ).json()
        uploaded = client.post(
            "/v1/assets",
            headers=_headers(owner),
            data={"project_id": project["id"], "asset_type": "REFERENCE"},
            files={"file": ("reference.png", io.BytesIO(_png_bytes()), "image/png")},
        ).json()
        logical = client.post(
            "/api/assets",
            headers=_headers(owner),
            json={
                "project_id": project["id"],
                "asset_type": "SCENE",
                "name": "酒店大堂",
            },
        ).json()
        spoofed = client.post(
            f"/api/assets/{logical['id']}/versions",
            headers=_headers(owner),
            json={
                "primary_media_asset_id": uploaded["id"],
                "source": "PASSENGER_GENERATION",
            },
        )
        assert spoofed.status_code == 422
        version = client.post(
            f"/api/assets/{logical['id']}/versions",
            headers=_headers(owner),
            json={"primary_media_asset_id": uploaded["id"], "source": "USER_UPLOAD"},
        ).json()
        promoted = client.post(
            f"/api/assets/{logical['id']}/versions/{version['id']}/promote",
            headers=_headers(owner),
            json={"reason": "用户明确选为当前版本"},
        )
        assert promoted.status_code == 200

    with container.database.session() as session:
        user = session.scalar(select(User).where(User.email == "asset-audit-owner@example.com"))
        asset = session.get(Asset, logical["id"])
        stored_version = session.get(AssetVersion, version["id"])
        promotion = session.scalar(
            select(AssetCanonicalPromotion).where(AssetCanonicalPromotion.to_version_id == stored_version.id)
        )
        assert asset.created_by_user_id == user.id
        assert stored_version.created_by_user_id == user.id
        assert stored_version.source == "USER_UPLOAD"
        assert promotion.promoted_by_user_id == user.id


def test_user_cannot_forge_qc_pass_but_internal_judge_can_write_evidence(container) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        owner = _register(client, "qc-owner@example.com")
        project = client.post(
            "/v1/projects",
            headers=_headers(owner),
            json={"title": "QC Trust Boundary"},
        ).json()
        image = io.BytesIO()
        Image.new("RGB", (32, 32), color="navy").save(image, format="PNG")
        image.seek(0)
        asset, _ = container.media.register(
            project["id"],
            "IMAGE",
            image,
            filename="candidate.png",
            mime_type="image/png",
        )
        with container.database.session() as session:
            episode = Episode(project_id=project["id"], title="Pilot", episode_number=1)
            session.add(episode)
            session.flush()
            scene = Scene(episode_id=episode.id, sequence=1)
            session.add(scene)
            session.flush()
            shot = Shot(scene_id=scene.id, sequence=1, prompt="One action")
            session.add(shot)
            session.flush()
            candidate = GenerationCandidate(
                shot_id=shot.id,
                attempt_number=1,
                output_asset_id=asset.id,
                status="VALIDATING",
            )
            session.add(candidate)
            session.flush()
            shot_id = shot.id
            candidate_id = candidate.id

        forged = client.post(
            f"/v1/shots/{shot_id}/candidates/{candidate_id}/validate",
            headers=_headers(owner),
            json={"evidence": {"character_score": 1, "camera_score": 1, "action_score": 1}},
        )
        assert forged.status_code == 403
        with container.database.session() as session:
            assert session.get(GenerationCandidate, candidate_id).qa_result_id is None

        judged = client.post(
            f"/internal/shots/{shot_id}/candidates/{candidate_id}/validate",
            headers={"Authorization": "Bearer test-platform-key"},
            json={
                "evidence": {
                    "identity_samples": [{"face_similarity": 0.95}] * 6,
                    "character_score": 0.95,
                    "scene_score": 0.95,
                    "composition_score": 0.95,
                    "action_score": 0.95,
                    "camera_score": 0.95,
                    "lighting_score": 0.95,
                    "narrative_score": 0.95,
                }
            },
        )
        assert judged.status_code == 200
        assert judged.json()["status"] == "PASSED"

    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        qa = session.get(QAResult, candidate.qa_result_id)
        assert qa.decision == "PASS"
        assert qa.metrics_json["evidence_source"] == "INTERNAL_QC"


def test_internal_routes_fail_closed_without_service_key(container):
    container.settings.auth_required = True
    container.settings.platform_api_key = ""
    with TestClient(create_app(container)) as client:
        issued = _register(client, "member@example.com")
        response = client.get("/internal/benchmarks", headers=_headers(issued))
        assert response.status_code == 503

    container.settings.platform_api_key = "internal-test-key"
    with TestClient(create_app(container)) as client:
        response = client.get(
            "/internal/benchmarks",
            headers={"Authorization": "Bearer internal-test-key"},
        )
        assert response.status_code == 200


def test_worker_websocket_rejects_missing_credentials_and_query_secrets(container):
    container.settings.auth_required = True
    container.settings.platform_api_key = ""
    with TestClient(create_app(container)) as client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("/v1/workers/ws/worker-1"):
                pass
        assert closed.value.code == 4401

    container.settings.platform_api_key = "worker-secret"
    with TestClient(create_app(container)) as client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("/v1/workers/ws/worker-1?token=worker-secret"):
                pass
        assert closed.value.code == 4401


def test_registration_cannot_claim_legacy_data_and_internal_claim_is_audited_and_idempotent(
    container,
):
    container.settings.auth_required = True
    with container.database.session() as session:
        legacy = User(email="local@ai-director.invalid", display_name="Local Director")
        session.add(legacy)
        session.flush()
        workspace = Workspace(owner_user_id=legacy.id, name="Director Workspace")
        session.add(workspace)
        session.flush()
        legacy_project = Project(
            workspace_id=workspace.id,
            title="Existing Commercial Work",
            name="Existing Commercial Work",
        )
        orphan_project = Project(title="Older Unassigned Work", name="Older Unassigned Work")
        session.add_all([legacy_project, orphan_project])
        session.flush()
        workspace_id = workspace.id
        project_ids = {legacy_project.id, orphan_project.id}

    with TestClient(create_app(container)) as client:
        attacker = _register(client, "first-real-attacker@example.com")
        target = _register(client, "legacy-owner@example.com")
        assert client.get("/v1/projects", headers=_headers(attacker)).json() == []
        assert client.get("/v1/projects", headers=_headers(target)).json() == []

        target_user_id = target["user"]["id"]
        claim_body = {
            "target_user_id": target_user_id,
            "idempotency_key": "legacy-claim-release-1",
        }
        ordinary_user = client.post(
            "/internal/auth/legacy-workspaces/claim",
            headers=_headers(attacker),
            json=claim_body,
        )
        assert ordinary_user.status_code == 401
        assert (
            client.post(
                "/internal/auth/legacy-workspaces/claim",
                json=claim_body,
            ).status_code
            == 401
        )
        container.settings.platform_api_key = ""
        no_configured_key = client.post(
            "/internal/auth/legacy-workspaces/claim",
            headers={"Authorization": "Bearer anything"},
            json=claim_body,
        )
        assert no_configured_key.status_code == 503
        container.settings.platform_api_key = "test-platform-key"
        wrong_target = client.post(
            "/internal/auth/legacy-workspaces/claim",
            headers={"Authorization": "Bearer test-platform-key"},
            json={
                "target_user_id": "missing-target-user",
                "idempotency_key": "legacy-claim-missing-target",
            },
        )
        assert wrong_target.status_code == 404
        assert client.get("/v1/projects", headers=_headers(target)).json() == []

        claimed = client.post(
            "/internal/auth/legacy-workspaces/claim",
            headers={"Authorization": "Bearer test-platform-key"},
            json=claim_body,
        )
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["replayed"] is False
        assert claimed.json()["target_user_id"] == target_user_id
        assert set(claimed.json()["workspace_ids"]) == {workspace_id}
        assert set(claimed.json()["project_ids"]) == project_ids

        replayed = client.post(
            "/internal/auth/legacy-workspaces/claim",
            headers={"Authorization": "Bearer test-platform-key"},
            json=claim_body,
        )
        assert replayed.status_code == 200
        assert replayed.json() == {**claimed.json(), "replayed": True}
        reused_key_for_attacker = client.post(
            "/internal/auth/legacy-workspaces/claim",
            headers={"Authorization": "Bearer test-platform-key"},
            json={**claim_body, "target_user_id": attacker["user"]["id"]},
        )
        assert reused_key_for_attacker.status_code == 409

        assert client.get("/v1/projects", headers=_headers(attacker)).json() == []
        projects = client.get("/v1/projects", headers=_headers(target)).json()
        assert {item["id"] for item in projects} == project_ids

    with container.database.session() as session:
        real_owner = session.scalar(select(User).where(User.email == "legacy-owner@example.com"))
        workspace = session.get(Workspace, workspace_id)
        assert workspace.owner_user_id == real_owner.id
        assert (
            session.scalar(
                select(func.count(WorkspaceMembership.id)).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == real_owner.id,
                    WorkspaceMembership.role == "OWNER",
                )
            )
            == 1
        )
        assert (
            set(session.scalars(select(Project.id).where(Project.workspace_id == workspace_id)))
            == project_ids
        )
        audit = session.scalar(select(LegacyWorkspaceClaim))
        assert audit.target_user_id == real_owner.id
        assert audit.actor_type == "PLATFORM_API_KEY"
        assert audit.idempotency_key == "legacy-claim-release-1"
        assert set(audit.workspace_ids) == {workspace_id}
        assert set(audit.project_ids) == project_ids
        assert audit.status == "COMPLETED"


def test_v1_project_upgrade_stays_isolated_until_explicit_internal_claim(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    database_path = tmp_path / "legacy-v1.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "0001_platform_v1")

    legacy_project_id = "11111111-1111-1111-1111-111111111111"
    now = datetime.now(UTC)
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """INSERT INTO projects (id, title, description, status, created_at, updated_at)
                VALUES (:id, 'Legacy Project', '', 'ACTIVE', :now, :now)"""
            ),
            {"id": legacy_project_id, "now": now},
        )
    command.upgrade(config, "head")

    settings = Settings(
        _env_file=None,
        database_url=database_url,
        storage_root=tmp_path / "media",
        public_base_url="http://testserver",
        auth_required=True,
        platform_api_key="internal-test-key",
    )
    migrated = build_container(settings)
    with TestClient(create_app(migrated)) as client:
        owner = _register(client, "first-upgrade-owner@example.com")
        before_claim = client.get("/v1/projects", headers=_headers(owner))
        assert before_claim.status_code == 200
        assert before_claim.json() == []
        claim = client.post(
            "/internal/auth/legacy-workspaces/claim",
            headers={"Authorization": "Bearer internal-test-key"},
            json={
                "target_user_id": owner["user"]["id"],
                "idempotency_key": "v1-upgrade-explicit-claim",
            },
        )
        projects = client.get("/v1/projects", headers=_headers(owner))

    assert claim.status_code == 200, claim.text
    assert claim.json()["replayed"] is False
    assert projects.status_code == 200
    assert {item["id"] for item in projects.json()} == {legacy_project_id}
