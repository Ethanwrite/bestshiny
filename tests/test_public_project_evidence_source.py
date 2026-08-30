"""Public project traces are useful evidence without becoming local outcomes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from router_evidence_core import (
    HiggsfieldPublicProjectClient,
    PublicProjectSource,
    PublicProjectSourceStore,
)
from video_platform_api.main import create_app

from scripts.ingest_public_project_evidence import _display_path, _write_json_atomic


def _source() -> PublicProjectSource:
    return PublicProjectSourceStore().source("higgsfield-oneiric-2026-08")


def _response(payload: object) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    cursor = request.url.params.get("cursor")
    if path.endswith("/fnf-series/series/317fb649-95f9-42c7-9d08-26b1a6cfb3a2"):
        return _response(
            {
                "id": "series-1",
                "slug": "oneiric",
                "name": "ONEIRIC",
                "episodes": [
                    {
                        "id": "episode-1",
                        "duration_seconds": 1189,
                        "project_publication": {
                            "publication_id": "pub-1",
                            "show_prompts": True,
                            "stats": {"generations_count": 41096},
                            "gallery_media": [
                                {
                                    "id": "final-film",
                                    "url": "https://cdn.example/final.m3u8",
                                    "type": "video",
                                }
                            ],
                        },
                    }
                ],
            }
        )
    if path.endswith("/fnf/folders/85a0f627-285b-4c8a-ac20-bcfd309d54c5"):
        return _response(
            {
                "id": "root",
                "name": "ONEIRIC",
                "count": 41118,
                "subfolders_count": 2,
                "is_snapshot": True,
                "publication": {"state": "published"},
            }
        )
    if path.endswith("/fnf/folders/85a0f627-285b-4c8a-ac20-bcfd309d54c5/children"):
        return _response(
            {
                "items": [
                    {
                        "id": "regen",
                        "parent_id": "root",
                        "name": "regenerations",
                        "count": 1,
                        "subfolders_count": 0,
                    },
                    {
                        "id": "scene-2",
                        "parent_id": "root",
                        "name": "SCENE 2 - LIVINGROOM",
                        "count": 2,
                        "subfolders_count": 1,
                    },
                ]
            }
        )
    if path.endswith("/fnf/folders/scene-2/children"):
        return _response(
            {
                "items": [
                    {
                        "id": "shot-1",
                        "parent_id": "scene-2",
                        "name": "shot 1",
                        "count": 2,
                        "subfolders_count": 0,
                    }
                ]
            }
        )
    if path.endswith("/fnf/folders/regen/items/v2"):
        assert request.url.params["include_subfolders"] == "false"
        return _response(
            {
                "items": [
                    {
                        "type": "job",
                        "job": {
                            "id": "regen-job",
                            "status": "completed",
                            "created_at": 1,
                            "job_set_id": "set-r",
                            "job_set_type": "seedance_2_5",
                            "params": {
                                "prompt": "creative rework prompt",
                                "duration": 8,
                                "resolution": "720p",
                                "aspect_ratio": "21:9",
                            },
                            "result": None,
                        },
                    }
                ],
                "cursor": None,
            }
        )
    if path.endswith("/fnf/folders/shot-1/items/v2") and cursor is None:
        assert request.url.params["include_subfolders"] == "false"
        return _response(
            {
                "items": [
                    {
                        "type": "job",
                        "job": {
                            "id": "shot-job-1",
                            "status": "failed",
                            "created_at": 2,
                            "job_set_id": "set-1",
                            "job_set_type": "seedance_2_0",
                            "params": {
                                "prompt": "first shot prompt",
                                "duration": 12,
                                "resolution": "4k",
                                "aspect_ratio": "21:9",
                                "reference_elements": [
                                    {
                                        "id": "character-1",
                                        "name": "char_ON_Sam_s2_v1",
                                        "category": "character",
                                        "medias": [
                                            {
                                                "id": "media-1",
                                                "url": "https://cdn.example/sam.png",
                                                "type": "media_input",
                                                "width": 100,
                                                "height": 100,
                                            }
                                        ],
                                    }
                                ],
                            },
                        },
                    }
                ],
                "cursor": "next-page",
            }
        )
    if path.endswith("/fnf/folders/shot-1/items/v2") and cursor == "next-page":
        assert request.url.params["include_subfolders"] == "false"
        return _response(
            {
                "items": [
                    {
                        "type": "job",
                        "job": {
                            "id": "shot-job-2",
                            "status": "completed",
                            "created_at": 3,
                            "job_set_id": "set-2",
                            "job_set_type": "seedance_2_0",
                            "params": {
                                "prompt": "revised shot prompt",
                                "duration": 12,
                                "resolution": "4k",
                                "aspect_ratio": "21:9",
                            },
                            "result": {
                                "type": "video",
                                "url": "https://cdn.example/generated.mp4",
                            },
                            "results": {
                                "raw": {
                                    "type": "video",
                                    "url": "https://cdn.example/generated.mp4",
                                }
                            },
                        },
                    }
                ],
                "cursor": None,
            }
        )
    if path.endswith("/children"):
        return _response({"items": []})
    raise AssertionError(f"unexpected request {request.url}")


def _snapshot():  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport) as http:
        return HiggsfieldPublicProjectClient(_source(), client=http).snapshot(max_items_per_folder=5)


def test_committed_oneiric_source_is_reporting_only() -> None:
    source = _source()
    assert source.posterior_eligible is False
    assert source.observed_stats.generations_count == 41096
    assert source.observed_stats.root_item_count == 41118
    assert source.observed_stats.regeneration_bucket_count == 292
    assert "NO_FINAL_SELECTION_LINK" in source.router_exclusion_reasons
    assert source.router_view()["posterior_eligible"] is False


def test_a_completed_regeneration_is_not_mislabelled_as_provider_failure() -> None:
    snapshot = _snapshot()
    regen = next(item for item in snapshot.generations if item.generation_id == "regen-job")
    assert regen.status == "completed"
    assert regen.outcome_class == "CREATIVE_REWORK_CANDIDATE"


def test_prompt_generation_assets_and_folder_are_exactly_linked() -> None:
    snapshot = _snapshot()
    failed = next(item for item in snapshot.generations if item.generation_id == "shot-job-1")
    assert failed.folder_path == "SCENE 2 - LIVINGROOM/shot 1"
    assert failed.prompt_sha256 == hashlib.sha256(b"first shot prompt").hexdigest()
    assert [item.asset_id for item in failed.reference_assets] == ["character-1"]
    assert failed.task_type == "R2V"
    assert failed.outcome_class == "PROVIDER_FAILURE"

    completed = next(item for item in snapshot.generations if item.generation_id == "shot-job-2")
    assert len(completed.output_assets) == 1  # result and results.raw are the same asset
    assert completed.output_assets[0].asset_id == "generated"
    assert completed.final_selection == "UNOBSERVED"


def test_lineage_keeps_chronology_but_refuses_to_invent_the_final_edit() -> None:
    snapshot = _snapshot()
    lineage = next(item for item in snapshot.lineages() if item.generation_id == "shot-job-2")
    assert lineage.final_shot_link == "UNOBSERVED"
    assert lineage.previous_candidate_ids == ["shot-job-1"]
    assert lineage.previous_explicit_failure_ids == ["shot-job-1"]
    assert lineage.previous_creative_rework_ids == []
    assert snapshot.production_evidence_view(_source())["warning"].startswith(
        "Every final_shot_link is UNOBSERVED"
    )


def test_router_projection_counts_the_sample_without_creating_a_posterior() -> None:
    snapshot = _snapshot()
    view = snapshot.router_evidence_view(_source())
    assert view["posterior_eligible"] is False
    assert view["sample"]["generation_jobs"] == 3
    assert view["sample"]["outcome_class_counts"] == {
        "COMPLETED_CANDIDATE": 1,
        "CREATIVE_REWORK_CANDIDATE": 1,
        "PROVIDER_FAILURE": 1,
    }
    assert not hasattr(snapshot, "to_production_observations")


def test_registry_can_be_loaded_from_an_explicit_path(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    path.write_text(PublicProjectSourceStore().path.read_text("utf-8"), "utf-8")
    assert PublicProjectSourceStore(path).sources()[0].source_id == "higgsfield-oneiric-2026-08"


def test_default_registry_is_available_in_the_active_runtime() -> None:
    store = PublicProjectSourceStore()
    assert store.path.is_file()
    assert store.registry().registry_version == "public-project-sources-v1"


def test_snapshot_writer_refuses_to_overwrite_existing_evidence(tmp_path: Path) -> None:
    destination = tmp_path / "snapshot.json"
    destination.write_text('{"preserved": true}\n', "utf-8")

    with pytest.raises(FileExistsError):
        _write_json_atomic(destination, {"replacement": True}, overwrite=False)

    assert json.loads(destination.read_text("utf-8")) == {"preserved": True}


def test_snapshot_writer_can_atomically_replace_when_explicit(tmp_path: Path) -> None:
    destination = tmp_path / "snapshot.json"
    destination.write_text('{"old": true}\n', "utf-8")

    _write_json_atomic(destination, {"new": True}, overwrite=True)

    assert json.loads(destination.read_text("utf-8")) == {"new": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_external_output_path_is_printable_without_becoming_an_error(tmp_path: Path) -> None:
    destination = (tmp_path / "snapshot.json").resolve()
    assert _display_path(destination) == str(destination)


def test_router_and_production_evidence_expose_the_same_registered_source(container) -> None:  # type: ignore[no-untyped-def]
    headers = {"Authorization": f"Bearer {container.settings.platform_api_key}"}
    with TestClient(create_app(container)) as client:
        router = client.get("/internal/models/router-evidence", headers=headers)
        production = client.get("/internal/production-evidence/sources", headers=headers)

    assert router.status_code == 200, router.text
    assert production.status_code == 200, production.text
    router_source = router.json()["external_production_case_studies"][0]
    production_source = production.json()["sources"][0]
    assert router_source["source_id"] == production_source["source_id"]
    assert router_source["posterior_eligible"] is False
    assert production_source["lineage_support"]["output_asset_to_final_edit"] == "UNOBSERVED"
