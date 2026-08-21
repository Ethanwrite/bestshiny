from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from platform_shared import Settings


def test_ordinary_suite_forces_provider_gates_offline() -> None:
    assert os.environ["PROVIDER_MODE"] == "mock"
    assert os.environ["ALLOW_LIVE_PROVIDER_CALLS"] == "false"
    assert os.environ["LIVE_PROVIDER_CONFIRMATION"] == ""
    assert os.environ["ALLOW_RUNAPI_EDGE_CALLS"] == "false"

    settings = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        deployment_environment="test",
        auth_required=False,
    )
    assert settings.provider_mode == "mock"
    assert settings.allow_live_provider_calls is False
    assert settings.live_provider_confirmation == ""
    assert settings.allow_runapi_edge_calls is False


def test_external_live_environment_is_overridden_for_ordinary_tests() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "PROVIDER_MODE": "live",
            "ALLOW_LIVE_PROVIDER_CALLS": "true",
            "LIVE_PROVIDER_CONFIRMATION": "I_UNDERSTAND_THIS_COSTS_MONEY",
            "ALLOW_RUNAPI_EDGE_CALLS": "true",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    environment.pop("PYTEST_CURRENT_TEST", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(Path(__file__).resolve()),
            "-k",
            "ordinary_suite_forces_provider_gates_offline",
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
