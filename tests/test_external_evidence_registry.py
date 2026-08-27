"""The External Evidence Registry, and the rules it exists to enforce.

The registry is a data asset, so most of what can go wrong with it is a data
error rather than a code error: a number attached to the wrong model version, a
Likert score averaged with an Elo, a capability claim promoted to a quality
measurement. These tests are the gate on the data, not on the loader.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from external_evidence_core import (
    PRIOR_ELIGIBLE_GRADES,
    PRIOR_ELIGIBLE_MATCHES,
    ExternalEvidenceRegistry,
    ExternalEvidenceService,
)

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "external-evidence" / "registry-v1.json"
MODEL_DEFAULTS = ROOT / "config" / "model-registry" / "defaults.json"


@pytest.fixture(scope="module")
def service() -> ExternalEvidenceService:
    return ExternalEvidenceService.load(REGISTRY_PATH)


@pytest.fixture(scope="module")
def registered_models() -> dict:
    config = json.loads(MODEL_DEFAULTS.read_text("utf-8"))
    return {item["logical_name"]: item for item in config["models"]}


def test_the_registry_loads_and_is_referentially_whole(service) -> None:  # type: ignore[no-untyped-def]
    """Schema validation covers source and evidence cross-references."""

    assert service.version == "external-evidence-v1"
    assert service.registry.frozen_at == "2026-08-25"
    assert len(service.registry.compiled_from) == 2


def test_every_active_binding_names_a_model_this_platform_actually_runs(  # type: ignore[no-untyped-def]
    service, registered_models
) -> None:
    """A binding to a model that is not in the model registry is dead evidence.

    Retired entries are exempt, and deliberately so. Execution and provenance are
    separate facts: a model can stop being routable without its verdict changing.
    A retired row keeps the source, provider and version identity it was recorded
    under — rewriting or deleting it to match the current routing table would
    make the registry a description of today rather than a record of what was
    checked, which is the one thing it exists to be.
    """

    retired = {
        item.logical_name
        for item in service.registry.unbacked_models
        if item.lifecycle == "RETIRED"
    }
    bound = {b.logical_name: b.provider_model_id for b in service.registry.bindings}
    bound.update({u.logical_name: u.provider_model_id for u in service.registry.unbacked_models})
    for logical_name, provider_model_id in sorted(bound.items()):
        if logical_name in retired:
            continue
        assert logical_name in registered_models, f"{logical_name} is not a registered model"
        assert registered_models[logical_name]["provider_model_id"] == provider_model_id, (
            f"{logical_name} is registered as "
            f"{registered_models[logical_name]['provider_model_id']}, not {provider_model_id}"
        )


def test_a_retired_entry_says_when_why_and_what_took_over(service, registered_models) -> None:  # type: ignore[no-untyped-def]
    """Retirement is a claim, and a claim with no detail is just a deletion.

    The successor has to be a model that really is in the registry, so a retired
    row points somewhere real rather than into a gap.
    """

    retired = [
        item for item in service.registry.unbacked_models if item.lifecycle == "RETIRED"
    ]
    assert retired, "expected at least one retired evidence entry"
    for item in retired:
        assert item.retired_on
        assert item.retirement_reason
        assert item.superseded_by is not None
        successor = registered_models.get(item.superseded_by.logical_name)
        assert successor is not None, f"{item.logical_name} is superseded by an unknown model"
        assert successor["provider_model_id"] == item.superseded_by.provider_model_id
        # The verdict and what it was recorded against are untouched.
        assert item.status == "NO_EXTERNAL_EVIDENCE"
        assert item.provider_model_id


def test_a_retired_model_is_not_expected_to_still_be_registered(service, registered_models) -> None:  # type: ignore[no-untyped-def]
    """The whole point: these two are gone from model_definitions, on purpose."""

    retired = {
        item.logical_name
        for item in service.registry.unbacked_models
        if item.lifecycle == "RETIRED"
    }
    assert retired == {"grok-video-official", "veo-3.1-quality-official"}
    assert not (retired & set(registered_models))


def test_every_registered_video_or_image_model_has_a_verdict(service, registered_models) -> None:  # type: ignore[no-untyped-def]
    """Silence is the failure mode. A model with no evidence must say so.

    Otherwise "we have no public evidence for this model" is indistinguishable
    from "nobody has looked yet", and a hand-authored prior keeps its unearned
    air of authority.
    """

    covered = {b.logical_name for b in service.registry.bindings}
    covered |= service.unbacked_model_names()
    generative = {
        name
        for name, item in registered_models.items()
        if item["modality"] in {"video", "image"} and item.get("enabled", False)
    }
    assert not generative - covered, (
        "enabled generative models with no entry in the evidence registry: "
        + ", ".join(sorted(generative - covered))
    )


def test_only_exact_version_matches_from_a_or_b_sources_can_move_a_score(service) -> None:  # type: ignore[no-untyped-def]
    for name in sorted({b.logical_name for b in service.registry.bindings}):
        for item in service.prior_items_for(name):
            assert item.grade in PRIOR_ELIGIBLE_GRADES
            assert item.binding.version_match in PRIOR_ELIGIBLE_MATCHES
            assert item.metric.mapping_confidence != "LOW"


def test_a_records_grade_is_the_weakest_source_it_cites(service) -> None:  # type: ignore[no-untyped-def]
    """One A source does not launder a C source it is cited beside."""

    grades = {s.source_id: s.grade for s in service.registry.sources}
    for evidence in service.registry.evidence:
        expected = max(grades[name] for name in evidence.source_ids)
        assert service._grade_for(evidence) == expected


# --- the version lock, stated as tests --------------------------------------
#
# Each of these is a number that a reasonable person would otherwise attach to
# the wrong model, because the model name is nearly the same and the number is
# the most useful one available.


@pytest.mark.parametrize(
    ("logical_name", "forbidden_evidence", "what_it_would_be"),
    [
        ("wan-2.7-official", "E-WAN21-WB", "Wan 2.1's Physical Plausibility .939 and Camera Control .527"),
        ("wan-2.7-official", "E-WAN22-OSC", "Wan 2.2's OSCBench scores"),
        ("wan-3.0-official", "E-WAN21-WB", "Wan 2.1's Wan-Bench diagnostics"),
        ("seedance-2.5-official", "E-SD20-CN-AV", "Seedance 2.0's Chinese multi-speaker dialogue scores"),
        ("seedance-2.5-official", "E-SD20-FINE", "Seedance 2.0's physics and camera sub-scores"),
        ("kling-3-pro-openrouter", "E-K25-OSC", "Kling 2.5 Turbo's OSCBench Action .826"),
        ("veo-3.1-openrouter", "E-VEO31F-OSC", "Veo 3.1 Fast's OSCBench Action .908"),
        ("veo-3.1-openrouter", "E-VEO3-MGB", "Veo 3's MovieGenBench sample sizes"),
        ("gpt-image-2-openrouter", "E-GPT4O-GENEVAL", "GPT-4o's GenEval Overall 0.84"),
    ],
)
def test_a_near_miss_version_is_recorded_and_never_a_prior(
    service, logical_name, forbidden_evidence, what_it_would_be
) -> None:  # type: ignore[no-untyped-def]
    """Recorded, not deleted: deleting it invites someone to re-derive it."""

    on_file = [item for item in service.items_for(logical_name) if item.evidence_id == forbidden_evidence]
    assert on_file, f"{what_it_would_be} should be recorded against {logical_name} as a near miss"
    assert all(not item.prior_eligible for item in on_file), (
        f"{what_it_would_be} must never be prior-eligible for {logical_name}"
    )
    # The mismatch is always reported, even when a weak source grade would
    # also have been enough to exclude it.
    assert all(
        {"VERSION_MISMATCH", "VARIANT_MISMATCH", "MODEL_MISMATCH"} & set(item.ineligibility_reasons)
        for item in on_file
    )


def test_veo_31_fast_is_the_only_video_model_with_diagnostic_external_evidence(service) -> None:  # type: ignore[no-untyped-def]
    """The headline finding, asserted so it cannot rot quietly.

    Every other video model this platform runs has holistic Arena preference and
    nothing else. If that changes — a new benchmark, a version we now run — this
    test fails and the claim gets restated rather than silently outliving its
    truth.
    """

    diagnostic = {"prompt_adherence", "temporal_consistency", "identity_consistency"}
    video_models = [
        name
        for name in {b.logical_name for b in service.registry.bindings}
        if not name.startswith("gpt-image")
    ]
    with_diagnostics = {
        name for name in video_models if service.capabilities_with_prior(name) & diagnostic
    }
    assert with_diagnostics == {"veo-3.1-fast-openrouter"}

    fast = service.capabilities_with_prior("veo-3.1-fast-openrouter")
    assert diagnostic <= fast
    others = {n: service.capabilities_with_prior(n) for n in video_models if n != "veo-3.1-fast-openrouter"}
    for name, capabilities in others.items():
        assert capabilities <= {"visual_quality"}, f"{name} unexpectedly gained a diagnostic prior"


def test_no_scene_the_gap_register_calls_insufficient_has_a_prior(service) -> None:  # type: ignore[no-untyped-def]
    """The gap register and the prior set must not contradict each other."""

    for gap in service.registry.gaps:
        if "," in gap.scope or gap.scope.startswith("every"):
            continue
        scenes = {s.strip() for s in gap.canonical_scene.split(",")}
        with_prior = {item.metric.canonical_scene for item in service.prior_items_for(gap.scope)}
        assert not scenes & with_prior, (
            f"{gap.scope} is recorded as INSUFFICIENT for {scenes & with_prior} yet has a prior there"
        )


def test_a_low_confidence_mapping_cannot_become_a_prior(service) -> None:  # type: ignore[no-untyped-def]
    """Wan-Bench's Weighted Score and Qwen-Image-Bench's Overall are aggregates.

    Both are real published numbers and both are recorded. Neither may stand in
    for a capability, because an aggregate that replaces a dimension is exactly
    how a model ends up with a physics score it never earned.
    """

    aggregates = [
        item
        for name in {b.logical_name for b in service.registry.bindings}
        for item in service.items_for(name)
        if item.metric.mapping_confidence == "LOW"
    ]
    assert aggregates, "the aggregate metrics should still be on file"
    assert all(not item.prior_eligible for item in aggregates)


def test_a_source_with_no_number_stores_no_number(service) -> None:  # type: ignore[no-untyped-def]
    """Veo 3's model card says "best" and publishes no win-rate table."""

    veo3 = next(e for e in service.registry.evidence if e.evidence_id == "E-VEO3-MGB")
    assert all(metric.value is None for metric in veo3.metrics)
    assert veo3.sample_size_prompts is not None


def test_dynamic_sources_carry_a_snapshot_date(service) -> None:  # type: ignore[no-untyped-def]
    """An Arena Elo is a reading, not a constant."""

    for source in service.registry.sources:
        if source.dynamic:
            assert source.snapshot_at
            assert "leaderboard" in source.source_type or "methodology" in source.source_type


def test_the_registry_refuses_a_binding_to_unknown_evidence(tmp_path) -> None:  # type: ignore[no-untyped-def]
    raw = json.loads(REGISTRY_PATH.read_text("utf-8"))
    raw["bindings"].append(
        {
            "logical_name": "wan-2.7-official",
            "provider_model_id": "wan-2.7",
            "evidence_id": "E-DOES-NOT-EXIST",
            "version_match": "EXACT",
            "rationale": "invented",
        }
    )
    with pytest.raises(ValueError, match="unknown evidence"):
        ExternalEvidenceRegistry.model_validate(raw)


def test_the_registry_refuses_a_model_that_is_both_backed_and_unbacked() -> None:
    raw = json.loads(REGISTRY_PATH.read_text("utf-8"))
    raw["unbacked_models"].append(
        {
            "logical_name": "wan-2.7-official",
            "provider_model_id": "wan-2.7",
            "status": "NO_EXTERNAL_EVIDENCE",
            "note": "contradicts an existing binding",
        }
    )
    with pytest.raises(ValueError, match="both backed and unbacked"):
        ExternalEvidenceRegistry.model_validate(raw)


def test_the_operator_endpoint_reports_exclusions_not_just_priors(container) -> None:  # type: ignore[no-untyped-def]
    """The useful question is why a model has no prior, not what its prior is."""

    from fastapi.testclient import TestClient
    from video_platform_api.main import create_app

    container.settings.platform_api_key = "external-evidence-test-key"
    headers = {"Authorization": "Bearer external-evidence-test-key"}
    with TestClient(create_app(container)) as client:
        overview = client.get("/internal/models/external-evidence", headers=headers)
        assert overview.status_code == 200, overview.text
        body = overview.json()
        assert body["registry_version"] == "external-evidence-v1"
        assert body["external_prior_enabled"] is False
        assert body["coverage"]["seedance-2.5-official"]["prior_eligible_metrics"] == 0

        detail = client.get(
            "/internal/models/external-evidence",
            headers=headers,
            params={"logical_name": "wan-2.7-official"},
        )
        assert detail.status_code == 200, detail.text
        metrics = detail.json()["metrics"]
        # Wan 2.1's Physical Plausibility is visible, and visible as excluded.
        physical = next(m for m in metrics if m["metric_name"] == "Physical Plausibility")
        assert physical["value"] == 0.939
        assert physical["model_version"] == "2.1"
        assert physical["prior_eligible"] is False
        assert "VERSION_MISMATCH" in physical["ineligibility_reasons"]
        # And the one thing that is eligible is the holistic Arena reading.
        eligible = [m for m in metrics if m["prior_eligible"]]
        assert [m["metric_name"] for m in eligible] == ["Elo"]
        assert eligible[0]["sample_size_runs"] == 16424
        assert eligible[0]["sources"][0]["dynamic"] is True

        assert client.get(
            "/internal/models/external-evidence",
            headers=headers,
            params={"logical_name": "not-a-model"},
        ).status_code == 404
