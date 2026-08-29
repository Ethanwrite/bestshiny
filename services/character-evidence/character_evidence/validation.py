from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

METRIC_FIELDS = {
    "person_detection_miss_rate",
    "face_detection_miss_rate",
    "face_false_positive_rate",
    "identity_false_match_rate",
    "identity_false_reject_rate",
    "tracking_id_switch_rate",
    "track_fragmentation_rate",
    "appearance_false_accept_rate",
    "appearance_false_reject_rate",
    "abstain_rate",
}


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 8) if denominator else None


def calculate_metrics(examples: Iterable[dict[str, Any]]) -> dict[str, float | int | None]:
    rows = list(examples)
    person_present = [row for row in rows if row["person_present"]]
    face_present = [row for row in rows if row["face_present"]]
    face_absent = [row for row in rows if not row["face_present"]]
    identity_different = [row for row in rows if row["identity_same"] is False]
    identity_same = [row for row in rows if row["identity_same"] is True]
    appearance_different = [row for row in rows if row["appearance_same"] is False]
    appearance_same = [row for row in rows if row["appearance_same"] is True]
    tracking_opportunities = sum(int(row["tracking_opportunities"]) for row in rows)
    ground_truth_tracks = sum(int(row["ground_truth_tracks"]) for row in rows)
    excess_fragments = sum(
        max(0, int(row["predicted_fragments"]) - int(row["ground_truth_tracks"]))
        for row in rows
    )
    return {
        "example_count": len(rows),
        "person_detection_miss_rate": _rate(
            sum(not row["person_detected"] for row in person_present), len(person_present)
        ),
        "face_detection_miss_rate": _rate(
            sum(not row["face_detected"] for row in face_present), len(face_present)
        ),
        "face_false_positive_rate": _rate(
            sum(row["face_detected"] for row in face_absent), len(face_absent)
        ),
        "identity_false_match_rate": _rate(
            sum(row["identity_decision"] == "MATCH" for row in identity_different),
            len(identity_different),
        ),
        "identity_false_reject_rate": _rate(
            sum(row["identity_decision"] == "NON_MATCH" for row in identity_same),
            len(identity_same),
        ),
        "tracking_id_switch_rate": _rate(
            sum(int(row["id_switches"]) for row in rows), tracking_opportunities
        ),
        "track_fragmentation_rate": _rate(excess_fragments, ground_truth_tracks),
        "appearance_false_accept_rate": _rate(
            sum(row["appearance_decision"] == "MATCH" for row in appearance_different),
            len(appearance_different),
        ),
        "appearance_false_reject_rate": _rate(
            sum(row["appearance_decision"] == "NON_MATCH" for row in appearance_same),
            len(appearance_same),
        ),
        "abstain_rate": _rate(sum(row["decision"] == "ABSTAIN" for row in rows), len(rows)),
    }


def evaluate_promotion(
    examples: list[dict[str, Any]], acceptance_path: Path
) -> dict[str, Any]:
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    required_slices = set(acceptance["required_slices"])
    thresholds = acceptance["promotion"]["SHADOW_TO_ADVISORY"]
    validation_plan = acceptance["validation_plan"]
    counts = Counter(slice_name for row in examples for slice_name in row["slices"])
    global_metrics = calculate_metrics(examples)
    per_slice = {
        slice_name: calculate_metrics(
            row for row in examples if slice_name in set(row["slices"])
        )
        for slice_name in sorted(required_slices)
    }
    failures: list[str] = []
    if validation_plan["status"] != "APPROVED":
        failures.append("VALIDATION_PLAN_NOT_APPROVED")
        return {
            "acceptance_criteria_version": acceptance["version"],
            "promotion": "SHADOW_TO_ADVISORY",
            "eligible": False,
            "failures": failures,
            "global": global_metrics,
            "per_slice": per_slice,
            "slice_counts": dict(sorted(counts.items())),
        }
    minimum_examples = int(validation_plan["minimum_authorized_examples"])
    minimum_per_slice = int(validation_plan["minimum_examples_per_required_slice"])
    if len(examples) < minimum_examples:
        failures.append("INSUFFICIENT_AUTHORIZED_EXAMPLES")
    for slice_name in sorted(required_slices):
        if counts[slice_name] < minimum_per_slice:
            failures.append(f"INSUFFICIENT_SLICE:{slice_name}")
            continue
        for threshold_name, limit in thresholds.items():
            metric_name = threshold_name.removesuffix("_max")
            value = per_slice[slice_name][metric_name]
            if value is None:
                failures.append(f"UNDEFINED_METRIC:{slice_name}:{metric_name}")
            elif value > float(limit):
                failures.append(f"THRESHOLD_FAILED:{slice_name}:{metric_name}")
    return {
        "acceptance_criteria_version": acceptance["version"],
        "promotion": "SHADOW_TO_ADVISORY",
        "eligible": not failures,
        "failures": failures,
        "global": global_metrics,
        "per_slice": per_slice,
        "slice_counts": dict(sorted(counts.items())),
    }


__all__ = ["METRIC_FIELDS", "calculate_metrics", "evaluate_promotion"]
