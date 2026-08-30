from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
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

# Mirrors validation/dataset.schema.json. The schema file remains the published
# contract; this module enforces it without a JSON-Schema dependency, and
# test_character_evidence_service pins the two against each other so they
# cannot drift apart silently.
DATASET_REQUIRED_FIELDS = ("dataset_version", "authorization_record", "examples")
AUTHORIZATION_REQUIRED_FIELDS = ("owner", "approved_at", "purpose", "retention_policy")
AUTHORIZATION_PURPOSE = "CHARACTER_EVIDENCE_VALIDATION"
EXAMPLE_REQUIRED_FIELDS = (
    "example_id",
    "media_asset_id",
    "media_sha256",
    "consent_record_id",
    "annotator_record_id",
    "slices",
    "person_present",
    "person_detected",
    "face_present",
    "face_detected",
    "identity_same",
    "identity_decision",
    "tracking_opportunities",
    "id_switches",
    "ground_truth_tracks",
    "predicted_fragments",
    "appearance_same",
    "appearance_decision",
    "decision",
)
_MATCH_DECISIONS = {"MATCH", "NON_MATCH", "ABSTAIN", "NOT_APPLICABLE"}
_EXAMPLE_DECISIONS = {"PASS", "FAIL", "ABSTAIN"}
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 0


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_authorization_record(record: Any, failures: list[str]) -> None:
    if not isinstance(record, dict):
        failures.append("AUTHORIZATION_RECORD_INVALID:not_an_object")
        return
    for field in AUTHORIZATION_REQUIRED_FIELDS:
        if field not in record:
            failures.append(f"AUTHORIZATION_RECORD_INVALID:{field}_missing")
    for extra in sorted(set(record) - set(AUTHORIZATION_REQUIRED_FIELDS)):
        failures.append(f"AUTHORIZATION_RECORD_INVALID:unknown_field:{extra}")
    if "owner" in record and not _is_nonempty_string(record["owner"]):
        failures.append("AUTHORIZATION_RECORD_INVALID:owner")
    if "retention_policy" in record and not _is_nonempty_string(record["retention_policy"]):
        failures.append("AUTHORIZATION_RECORD_INVALID:retention_policy")
    if "purpose" in record and record["purpose"] != AUTHORIZATION_PURPOSE:
        failures.append("AUTHORIZATION_RECORD_INVALID:purpose")
    if "approved_at" in record:
        approved_at = record["approved_at"]
        try:
            datetime.fromisoformat(str(approved_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            failures.append("AUTHORIZATION_RECORD_INVALID:approved_at")


def _validate_example(index: int, example: Any, failures: list[str]) -> None:
    label = (
        example.get("example_id")
        if isinstance(example, dict) and _is_nonempty_string(example.get("example_id"))
        else str(index)
    )
    if not isinstance(example, dict):
        failures.append(f"EXAMPLE_INVALID:{label}:not_an_object")
        return
    for field in EXAMPLE_REQUIRED_FIELDS:
        if field not in example:
            failures.append(f"EXAMPLE_INVALID:{label}:{field}_missing")
    for extra in sorted(set(example) - set(EXAMPLE_REQUIRED_FIELDS)):
        failures.append(f"EXAMPLE_INVALID:{label}:unknown_field:{extra}")
    checks: tuple[tuple[str, Any], ...] = (
        ("example_id", _is_nonempty_string),
        ("media_asset_id", _is_nonempty_string),
        ("annotator_record_id", _is_nonempty_string),
        ("person_present", _is_bool),
        ("person_detected", _is_bool),
        ("face_present", _is_bool),
        ("face_detected", _is_bool),
        ("tracking_opportunities", _is_nonnegative_int),
        ("id_switches", _is_nonnegative_int),
        ("ground_truth_tracks", _is_nonnegative_int),
        ("predicted_fragments", _is_nonnegative_int),
    )
    for field, check in checks:
        if field in example and not check(example[field]):
            failures.append(f"EXAMPLE_INVALID:{label}:{field}")
    if "consent_record_id" in example and not _is_nonempty_string(example["consent_record_id"]):
        failures.append(f"EXAMPLE_CONSENT_MISSING:{label}")
    if "media_sha256" in example and not (
        isinstance(example["media_sha256"], str)
        and _SHA256_PATTERN.fullmatch(example["media_sha256"])
    ):
        failures.append(f"EXAMPLE_INVALID:{label}:media_sha256")
    if "slices" in example:
        slices = example["slices"]
        if (
            not isinstance(slices, list)
            or not slices
            or len(set(map(str, slices))) != len(slices)
            or not all(_is_nonempty_string(item) for item in slices)
        ):
            failures.append(f"EXAMPLE_INVALID:{label}:slices")
    for field in ("identity_same", "appearance_same"):
        if field in example and example[field] is not None and not _is_bool(example[field]):
            failures.append(f"EXAMPLE_INVALID:{label}:{field}")
    for field in ("identity_decision", "appearance_decision"):
        if field in example and example[field] not in _MATCH_DECISIONS:
            failures.append(f"EXAMPLE_INVALID:{label}:{field}")
    if "decision" in example and example["decision"] not in _EXAMPLE_DECISIONS:
        failures.append(f"EXAMPLE_INVALID:{label}:decision")


def validate_dataset(dataset: Any) -> list[str]:
    """Enforce validation/dataset.schema.json on a candidate dataset document.

    Returns machine-readable failure codes; an empty list means the document
    satisfies the schema, including the top-level ``authorization_record`` and
    every example's ``consent_record_id``.
    """

    failures: list[str] = []
    if not isinstance(dataset, dict):
        return ["DATASET_NOT_AN_OBJECT"]
    for field in DATASET_REQUIRED_FIELDS:
        if field not in dataset:
            failures.append(f"DATASET_FIELD_MISSING:{field}")
    for extra in sorted(set(dataset) - set(DATASET_REQUIRED_FIELDS)):
        failures.append(f"DATASET_FIELD_INVALID:unknown_field:{extra}")
    if "dataset_version" in dataset and not _is_nonempty_string(dataset["dataset_version"]):
        failures.append("DATASET_FIELD_INVALID:dataset_version")
    if "authorization_record" in dataset:
        _validate_authorization_record(dataset["authorization_record"], failures)
    examples = dataset.get("examples")
    if "examples" in dataset:
        if not isinstance(examples, list):
            failures.append("DATASET_FIELD_INVALID:examples")
        else:
            for index, example in enumerate(examples):
                _validate_example(index, example, failures)
    return failures


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


def evaluate_promotion(dataset: dict[str, Any], acceptance_path: Path) -> dict[str, Any]:
    """Judge SHADOW→ADVISORY eligibility from a full, authorized dataset document.

    The input is the dataset document defined by ``validation/dataset.schema.json``
    — ``dataset_version``, ``authorization_record``, ``examples`` — never a bare
    example list. Schema violations, a missing or malformed authorization
    record, or any example without a ``consent_record_id`` make the dataset
    ineligible before a single metric is computed; an approved validation plan
    cannot override that.
    """

    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    required_slices = set(acceptance["required_slices"])
    thresholds = acceptance["promotion"]["SHADOW_TO_ADVISORY"]
    validation_plan = acceptance["validation_plan"]
    failures: list[str] = validate_dataset(dataset)
    if failures:
        return {
            "acceptance_criteria_version": acceptance["version"],
            "promotion": "SHADOW_TO_ADVISORY",
            "eligible": False,
            "failures": failures,
            "global": None,
            "per_slice": {},
            "slice_counts": {},
        }
    examples = dataset["examples"]
    counts = Counter(slice_name for row in examples for slice_name in row["slices"])
    global_metrics = calculate_metrics(examples)
    per_slice = {
        slice_name: calculate_metrics(
            row for row in examples if slice_name in set(row["slices"])
        )
        for slice_name in sorted(required_slices)
    }
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


__all__ = [
    "AUTHORIZATION_REQUIRED_FIELDS",
    "DATASET_REQUIRED_FIELDS",
    "EXAMPLE_REQUIRED_FIELDS",
    "METRIC_FIELDS",
    "calculate_metrics",
    "evaluate_promotion",
    "validate_dataset",
]
