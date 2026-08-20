from __future__ import annotations


def test_production_metrics_create_adaptive_capability_evidence(container):
    for _ in range(50):
        container.model_metrics.record(
            provider="kling",
            model_id="kling-3.0",
            metric="generation_success",
        )
        container.model_metrics.record(
            provider="kling",
            model_id="kling-3.0",
            metric="user_accept",
        )
    for _ in range(5):
        container.model_metrics.record(
            provider="kling",
            model_id="kling-3.0",
            metric="physics_failure",
        )

    adjustments, counts = container.model_metrics.production_adjustments()

    assert counts["kling:kling-3.0"] == 50
    assert adjustments["kling:kling-3.0"]["visual_quality"] == 1.0
    assert adjustments["kling:kling-3.0"]["physical_plausibility"] == 0.9


def test_benchmark_suite_includes_rear_view_and_updates_dimensions(container):
    cases = {item["key"] for item in container.benchmarks.manifest()}
    assert {
        "portrait_consistency",
        "profile_preservation",
        "rear_view_ending",
        "two_character_dialogue",
        "product_logo",
        "long_take",
    }.issubset(cases)

    container.benchmarks.record(
        provider="grok",
        model_id="grok-video",
        model_version="current",
        case_key="rear_view_ending",
        scores={"character_consistency": 0.72, "camera_control": 0.35},
        passed=False,
    )
    adjustment = container.benchmarks.adjustments()["grok:grok-video"]
    assert adjustment["character_consistency"] == 0.72
    assert adjustment["camera_control"] == 0.35
