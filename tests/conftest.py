from __future__ import annotations

import io
import os
import uuid

os.environ.setdefault("DEPLOYMENT_ENVIRONMENT", "test")
# Importing the API module constructs its default app. Keep that collection-time
# instance off every developer/user database; individual fixtures supply their
# own isolated database URLs below.
os.environ.setdefault("DATABASE_URL", "sqlite://")

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
    parser.addoption(
        "--database",
        action="store",
        default="sqlite",
        choices=("sqlite", "postgres"),
        help=(
            "engine behind the shared `container` fixture. SQLite is fast and fine for "
            "algorithm-level tests; PostgreSQL is the only one that answers transaction, "
            "savepoint, concurrency and locking questions the way production will "
            "(see docs/OPEN_ISSUES.md 2.12). Needs TEST_POSTGRES_URL or a running "
            "`docker compose up -d postgres`."
        ),
    )


# Tests that pass on SQLite and fail on PostgreSQL because of a defect in the
# code under test, not in the test. Each is a real divergence recorded in
# docs/OPEN_ISSUES.md; strict xfail means the day one is fixed, the PostgreSQL
# run fails until its entry here is removed.
POSTGRES_KNOWN_DIVERGENCES: dict[str, str] = {}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    on_postgres = config.getoption("--database") == "postgres"
    if on_postgres:
        for item in items:
            reason = POSTGRES_KNOWN_DIVERGENCES.get(item.nodeid)
            if reason is not None:
                item.add_marker(pytest.mark.xfail(reason=reason, strict=True))
    else:
        # Tests of behaviour that only exists where transactions genuinely run
        # concurrently. SQLite serialises them, so the situation under test
        # cannot be constructed there — not a gap in coverage but the absence
        # of the phenomenon.
        skip = pytest.mark.skip(reason="needs real concurrent transactions: run with --database=postgres")
        for item in items:
            if item.get_closest_marker("postgres_only") is not None:
                item.add_marker(skip)
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


# The test database is deliberately *not* the development one. A test schema
# whose search_path can also see a populated `public` is not isolated:
# `create_all(checkfirst=True)` finds each table already present and skips it,
# so every test then reads and writes the shared copy and collides with the
# one before it. A dedicated database keeps `public` empty.
TEST_DATABASE_NAME = "video_platform_test"


def _postgres_admin_url() -> str:
    """The server this matrix connects to, on its default database.

    TEST_POSTGRES_URL wins; otherwise the compose service, whose password comes
    from the same POSTGRES_PASSWORD the compose file reads.
    """

    configured = os.environ.get("TEST_POSTGRES_URL")
    if configured:
        return configured
    password = os.environ.get("POSTGRES_PASSWORD", "")
    if not password:
        raise pytest.UsageError(
            "--database=postgres needs TEST_POSTGRES_URL, or POSTGRES_PASSWORD for the "
            "compose service (docker compose up -d postgres)"
        )
    return f"postgresql+psycopg://video_platform:{password}@127.0.0.1:5432/video_platform"


@pytest.fixture(scope="session")
def postgres_test_database() -> str:
    """Create the dedicated test database once per session, and enable pgvector."""

    import sqlalchemy as sa

    admin = sa.create_engine(_postgres_admin_url(), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        exists = connection.execute(
            sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": TEST_DATABASE_NAME},
        ).scalar()
        if not exists:
            connection.execute(sa.text(f'CREATE DATABASE "{TEST_DATABASE_NAME}"'))
    admin.dispose()

    url = sa.engine.make_url(_postgres_admin_url()).set(database=TEST_DATABASE_NAME)
    engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    engine.dispose()
    return url.render_as_string(hide_password=False)


@pytest.fixture
def database_url(request, tmp_path):  # type: ignore[no-untyped-def]
    """A private, empty database for one test, on the engine the matrix selected.

    PostgreSQL gets a throwaway schema rather than a throwaway database: one
    CREATE SCHEMA is cheap where one CREATE DATABASE is not, and search_path
    makes it invisible to everything under test. `public` stays on the path
    only because the `vector` type lives there, and it is empty in this
    database by construction.
    """

    if request.config.getoption("--database") == "sqlite":
        yield f"sqlite:///{tmp_path / 'platform.db'}"
        return

    import sqlalchemy as sa

    base_url = request.getfixturevalue("postgres_test_database")
    schema = "t_" + uuid.uuid4().hex
    admin = sa.create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    separator = "&" if "?" in base_url else "?"
    try:
        yield f"{base_url}{separator}options=-csearch_path%3D{schema},public"
    finally:
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def container(tmp_path, database_url):
    """Build the container, and hand its connections back when the test ends.

    The PostgreSQL half drops the test schema on teardown, and `DROP SCHEMA
    ... CASCADE` contends with any connection still holding objects in it. The
    pool outlives the test unless it is disposed, so dispose it here — before
    the `database_url` fixture, which is set up first and therefore torn down
    last, tries the drop.
    """

    settings = Settings(
        _env_file=None,
        database_url=database_url,
        storage_root=tmp_path / "media",
        public_base_url="http://testserver",
        flow_project_id="flow-project-test",
        worker_heartbeat_timeout_seconds=1,
        browser_command_timeout_seconds=2,
        auth_required=False,
        platform_api_key="test-platform-key",
        deployment_environment="test",
        # Tests run on local disk, which cannot issue an object-storage URL. The
        # signed local route is the development affordance that lets the
        # reference path be exercised at all; a dedicated test covers the
        # fail-closed behaviour when no key is configured.
        local_reference_signing_key="test-reference-signing-key",
    )
    built = build_container(settings)
    try:
        yield built
    finally:
        built.database.engine.dispose()


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
