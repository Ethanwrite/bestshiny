from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from platform_contracts import GenerationRequest
from production_domain.models import AssetType
from video_platform_api.main import create_app


def test_media_registry_deduplicates_same_reference_twenty_times(container, project):
    ids = []
    reused = []
    for _ in range(20):
        asset, was_reused = container.media.register(
            project.id,
            AssetType.CHARACTER_REFERENCE.value,
            io.BytesIO(b"same-character-reference"),
            filename="character.png",
            mime_type="image/png",
        )
        ids.append(asset.id)
        reused.append(was_reused)
    assert len(set(ids)) == 1
    assert reused == [False] + [True] * 19


def test_idempotency_replays_same_payload_and_conflicts_on_change(container, project):
    original = GenerationRequest(
        project_id=project.id,
        type="video",
        prompt="A single action",
        idempotency_key="paid-request-1",
    )
    first, replayed = container.gateway.create(original)
    second, replayed_again = container.gateway.create(original)
    assert replayed is False
    assert replayed_again is True
    assert second.id == first.id

    changed = original.model_copy(update={"prompt": "A different paid action"})
    with pytest.raises(RuntimeError, match="different request"):
        container.gateway.create(changed)


def test_api_returns_409_for_reused_key_with_different_payload(container, project):
    with TestClient(create_app(container)) as client:
        base = {
            "project_id": project.id,
            "type": "video",
            "prompt": "First prompt",
            "idempotency_key": "conflict-key",
        }
        assert client.post("/v1/generations", json=base).status_code == 202
        assert client.post("/v1/generations", json={**base, "prompt": "Changed"}).status_code == 409
