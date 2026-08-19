from __future__ import annotations

import io

import pytest
from platform_shared import Settings
from production_domain.models import BrowserWorker, Project, ProviderAccount
from video_platform_api.container import build_container


@pytest.fixture
def container(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'platform.db'}",
        storage_root=tmp_path / "media",
        public_base_url="http://testserver",
        flow_project_id="flow-project-test",
        worker_heartbeat_timeout_seconds=1,
        browser_command_timeout_seconds=2,
    )
    return build_container(settings)


@pytest.fixture
def project(container):
    with container.database.session() as session:
        item = Project(title="Episode One")
        session.add(item)
        session.flush()
        return item


@pytest.fixture
def account_worker(container):
    with container.database.session() as session:
        account = ProviderAccount(
            provider="google_flow",
            account_identifier="flow@example.com",
            tier="PRO",
            credits=100,
            image_capacity=2,
            video_capacity=2,
            supported_models=["veo", "NARWHAL"],
            metadata_json={"project_id": "flow-project-test"},
        )
        session.add(account)
        session.flush()
        worker = BrowserWorker(
            id="worker-1",
            provider="google_flow",
            account_id=account.id,
            connection_id="connection-1",
            capabilities=["image", "video", "upload", "poll"],
            max_jobs=2,
        )
        session.add(worker)
        account.worker_id = worker.id
        session.flush()
        return account.id, worker.id


@pytest.fixture
def register_bytes():
    def register(container, project_id: str, asset_type: str, data: bytes = b"reference-bytes"):
        return container.media.register(
            project_id,
            asset_type,
            io.BytesIO(data),
            filename="reference.png",
            mime_type="image/png",
        )[0]

    return register
