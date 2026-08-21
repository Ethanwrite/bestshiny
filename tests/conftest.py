from __future__ import annotations

import io
import os

os.environ.setdefault("DEPLOYMENT_ENVIRONMENT", "test")

import pytest
from platform_shared import Settings
from production_domain.models import BrowserWorker, Project, ProviderAccount
from video_platform_api.container import build_container

_PROVIDER_GATE_ENVIRONMENT = (
    "PROVIDER_MODE",
    "ALLOW_LIVE_PROVIDER_CALLS",
    "LIVE_PROVIDER_CONFIRMATION",
    "ALLOW_RUNAPI_EDGE_CALLS",
)
_ORIGINAL_PROVIDER_GATE_ENVIRONMENT = {name: os.environ.get(name) for name in _PROVIDER_GATE_ENVIRONMENT}
_SAFE_PROVIDER_GATE_ENVIRONMENT = {
    "PROVIDER_MODE": "mock",
    "ALLOW_LIVE_PROVIDER_CALLS": "false",
    "LIVE_PROVIDER_CONFIRMATION": "",
    "ALLOW_RUNAPI_EDGE_CALLS": "false",
}

# Test modules may create Settings while they are imported, before fixtures run.
# Make collection offline too; explicitly enabled live tests restore the invoking
# process values in their own fixture scope below.
os.environ.update(_SAFE_PROVIDER_GATE_ENVIRONMENT)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live-provider",
        action="store_true",
        default=False,
        help="run tests marked live_provider; provider live gates are still required",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-live-provider"):
        return
    skipped = pytest.mark.skip(reason="live provider test requires the explicit --run-live-provider switch")
    for item in items:
        if item.get_closest_marker("live_provider") is not None:
            item.add_marker(skipped)


@pytest.fixture(autouse=True)
def isolate_provider_gate_environment(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    is_enabled_live_test = request.node.get_closest_marker(
        "live_provider"
    ) is not None and request.config.getoption("--run-live-provider")
    if is_enabled_live_test:
        for name, value in _ORIGINAL_PROVIDER_GATE_ENVIRONMENT.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        return
    for name, value in _SAFE_PROVIDER_GATE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


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
        auth_required=False,
        platform_api_key="test-platform-key",
        deployment_environment="test",
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
