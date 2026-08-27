"""Ingestion, which exists to refuse things.

Every test here is a way research output can be wrong. The acceptance path gets
one test; the rest are the reasons a plausible-looking record does not survive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from router_evidence_core import EvidenceLayer, content_hash, deduplicate
from router_evidence_core.ingest import EvidenceIngestor, TargetIdentity

NOW = datetime(2026, 8, 26, tzinfo=UTC)
IDENTITY = TargetIdentity(
    logical_name="wan-2.7-official",
    provider="wan",
    model_id="wan-2.7",
    exact_version="wan-2.7",
)


def _candidate(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "record_id": "bench-1",
        "provenance": {
            "source_url": "https://example.invalid/bench",
            "source_type": "independent_benchmark",
            "publisher": "Example Lab",
            "published_at": "2026-05-01",
            "retrieved_at": NOW.isoformat(),
            "retrieved_by": "grok",
            "verbatim_quote": "Wan 2.7 scores 0.939 on the physics dimension.",
            "summary": "Independent physics benchmark result.",
        },
        "binding": {
            "logical_name": "Wan",
            "provider": "Alibaba",
            "model_id": "wan",
            "source_model_name": "Wan 2.7",
            "source_model_version": "2.7",
            "exact_version": "2.7",
            "version_match": "EXACT",
            "mapping_confidence": "HIGH",
            "mapping_rationale": "The paper names Wan 2.7.",
        },
        "measurements": [
            {
                "metric_name": "physics",
                "value": 0.939,
                "metric_scale_id": "vbench2-dimension-0-1",
                "scenario": "physics",
                "task_type": "T2V",
                "scenario_mapping_rationale": "The dimension is named physics.",
            }
        ],
        "credibility": "B",
        "credibility_rationale": "Independent, protocol published.",
        "benchmark_name": "ExampleBench",
        "evaluation_method": "automatic scoring",
        "evaluator": "Example Lab",
        "human_or_automatic": "automatic",
    }
    record.update(overrides)
    return record


def test_a_well_formed_record_is_accepted_and_bound_to_our_identifiers() -> None:
    report = EvidenceIngestor(now=NOW).ingest(
        EvidenceLayer.BENCHMARK, [_candidate()], identity=IDENTITY
    )
    assert len(report.accepted) == 1
    binding = report.accepted[0].binding
    # Our names for the model, the source's names kept beside them.
    assert (binding.provider, binding.model_id, binding.exact_version) == ("wan", "wan-2.7", "wan-2.7")
    assert binding.source_model_name == "Wan 2.7"
    assert binding.version_match == "EXACT"


def test_the_number_must_appear_in_the_quote() -> None:
    """The highest-yield check there is: a fabricated score cannot point at itself."""

    payload = _candidate()
    payload["provenance"]["verbatim_quote"] = "Wan 2.7 performed very well on physics."  # type: ignore[index]
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, [payload], identity=IDENTITY)
    assert not report.accepted
    assert report.rejected[0].reason == "UNQUOTED_NUMBER"


def test_a_percentage_form_of_the_same_number_still_counts() -> None:
    payload = _candidate()
    payload["provenance"]["verbatim_quote"] = "Wan 2.7 reaches 93.9% on physics."  # type: ignore[index]
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, [payload], identity=IDENTITY)
    assert len(report.accepted) == 1


def test_a_claim_with_no_number_needs_no_number_in_the_quote() -> None:
    payload = _candidate()
    payload["measurements"][0]["value"] = None  # type: ignore[index]
    payload["measurements"][0]["metric_scale_id"] = "unscored"  # type: ignore[index]
    payload["provenance"]["verbatim_quote"] = "Wan 2.7 is the strongest at physics."  # type: ignore[index]
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, [payload], identity=IDENTITY)
    assert len(report.accepted) == 1
    assert report.accepted[0].measurements[0].value is None


def test_an_unregistered_scale_is_refused() -> None:
    payload = _candidate()
    payload["measurements"][0]["metric_scale_id"] = "vibes-out-of-ten"  # type: ignore[index]
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, [payload], identity=IDENTITY)
    assert report.rejected[0].reason == "SCALE_UNKNOWN"


def test_high_mapping_confidence_without_a_stated_version_is_refused() -> None:
    payload = _candidate()
    payload["binding"]["source_model_version"] = None  # type: ignore[index]
    payload["binding"]["version_match"] = "EXACT_VERSION_UNSPECIFIED_REVISION"  # type: ignore[index]
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, [payload], identity=IDENTITY)
    assert report.rejected[0].reason == "VERSION_UNCONFIRMED"


def test_a_record_retrieved_before_it_was_published_is_refused() -> None:
    payload = _candidate()
    payload["provenance"]["published_at"] = "2027-01-01"  # type: ignore[index]
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, [payload], identity=IDENTITY)
    assert report.rejected[0].reason == "RETRIEVED_BEFORE_PUBLISHED"


def test_a_record_retrieved_in_the_future_is_refused() -> None:
    payload = _candidate()
    payload["provenance"]["retrieved_at"] = (NOW + timedelta(days=2)).isoformat()  # type: ignore[index]
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, [payload], identity=IDENTITY)
    assert report.rejected[0].reason == "FUTURE_TIMESTAMP"


def test_an_unattributed_sample_size_fails_the_schema() -> None:
    payload = _candidate()
    payload["statistics"] = {"sample_size": 1000}
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, [payload], identity=IDENTITY)
    assert report.rejected[0].reason == "SCHEMA_INVALID"
    assert "sample_size" in report.rejected[0].detail


def test_a_benchmark_with_no_sample_size_is_marked_not_refused() -> None:
    """Kept and quieter, because most published numbers do not state one."""

    report = EvidenceIngestor(now=NOW).ingest(
        EvidenceLayer.BENCHMARK, [_candidate()], identity=IDENTITY
    )
    assert len(report.accepted) == 1
    assert "NO_SAMPLE_SIZE" in report.reasons()


def test_an_alias_binding_is_marked_and_kept() -> None:
    payload = _candidate()
    payload["binding"]["is_alias"] = True  # type: ignore[index]
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, [payload], identity=IDENTITY)
    assert len(report.accepted) == 1
    assert "ALIAS_AS_SNAPSHOT" in report.reasons()
    assert report.accepted[0].prior_eligible is False


def test_a_community_post_gets_its_hash_and_spam_signals_derived() -> None:
    payload = {
        "record_id": "c-1",
        "provenance": {
            "source_url": "https://reddit.invalid/1",
            "source_type": "reddit",
            "publisher": "r/aivideo",
            "published_at": "2026-07-01",
            "retrieved_at": NOW.isoformat(),
            "retrieved_by": "grok",
            "verbatim_quote": "dm for access, cheapest api around",
            "summary": "promotional",
        },
        "binding": _candidate()["binding"],
        "measurements": [
            {
                "metric_name": "stance",
                "value": None,
                "metric_scale_id": "community-stance-net",
                "scenario": "identity",
                "task_type": "I2V",
                "scenario_mapping_rationale": "About identity.",
            }
        ],
        "credibility": "C",
        "credibility_rationale": "One post.",
        "author_handle": "seller",
        "author_key": "reddit:seller",
        "venue": "r/aivideo",
        "stance": "positive",
        "experience": "firsthand",
    }
    report = EvidenceIngestor(now=NOW).ingest(
        EvidenceLayer.COMMUNITY, [payload], identity=IDENTITY
    )
    assert len(report.accepted) == 1
    record = report.accepted[0]
    assert record.content_hash
    assert record.spam_signals == ["cheapest api", "dm for access"]
    assert "SPAM_SIGNALS" in report.reasons()


def test_the_layer_is_derived_from_the_source_type_not_trusted_from_the_payload() -> None:
    payload = _candidate(layer="official_prior")
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, [payload], identity=IDENTITY)
    assert len(report.accepted) == 1
    assert report.accepted[0].layer is EvidenceLayer.BENCHMARK


def test_a_source_from_the_wrong_class_is_refused_for_this_layer() -> None:
    payload = _candidate()
    payload["provenance"]["source_type"] = "reddit"  # type: ignore[index]
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, [payload], identity=IDENTITY)
    assert report.rejected[0].reason == "SCHEMA_INVALID"


def test_the_acceptance_rate_is_reported() -> None:
    payloads = [_candidate(), _candidate(record_id="bench-2")]
    payloads[1]["measurements"][0]["metric_scale_id"] = "nonsense"  # type: ignore[index]
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, payloads, identity=IDENTITY)
    assert report.considered == 2
    assert report.acceptance_rate == 0.5


def test_crossposts_collapse_to_the_earliest() -> None:
    from router_evidence_core.records import CommunityRecord

    def _post(record_id: str, published: str) -> CommunityRecord:
        text = "identical crosspost text"
        return CommunityRecord.model_validate(
            {
                "record_id": record_id,
                "provenance": {
                    "source_url": f"https://x.invalid/{record_id}",
                    "source_type": "x",
                    "publisher": "x",
                    "published_at": published,
                    "retrieved_at": NOW.isoformat(),
                    "retrieved_by": "grok",
                    "verbatim_quote": text,
                    "summary": text,
                },
                "binding": _candidate()["binding"],
                "measurements": [
                    {
                        "metric_name": "stance",
                        "value": None,
                        "metric_scale_id": "community-stance-net",
                        "scenario": "identity",
                        "task_type": "I2V",
                        "scenario_mapping_rationale": "About identity.",
                    }
                ],
                "credibility": "C",
                "credibility_rationale": "One post.",
                "author_handle": record_id,
                "author_key": f"x:{record_id}",
                "venue": "x",
                "stance": "negative",
                "experience": "firsthand",
                "content_hash": content_hash(text),
            }
        )

    kept, duplicates = deduplicate([_post("late", "2026-07-05"), _post("early", "2026-07-01")])
    assert [item.record_id for item in kept] == ["early"]
    assert duplicates[0].record_id == "late"


def test_a_sample_size_in_the_quote_does_not_validate_a_score() -> None:
    """The check the design leans on hardest, and the way it used to be fooled.

    `value * 100` expanded 0.87 to "87", and the quote's "87 prompts" matched —
    so a fabricated score validated against a sentence about sample size. The
    percent form is still offered (0.939 really is published as "93.9"), but a
    bare integer that only matches after scaling no longer passes on its own.
    """

    payload = _candidate()
    payload["measurements"][0]["value"] = 0.87  # type: ignore[index]
    payload["provenance"]["verbatim_quote"] = "We ran 87 prompts across four models."  # type: ignore[index]
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, [payload], identity=IDENTITY)
    assert not report.accepted
    assert report.rejected[0].reason == "UNQUOTED_NUMBER"


def test_the_percent_form_of_a_ratio_still_validates() -> None:
    payload = _candidate()
    payload["measurements"][0]["value"] = 0.939  # type: ignore[index]
    payload["provenance"]["verbatim_quote"] = "Wan 2.7 reaches 93.9 on the physics dimension."  # type: ignore[index]
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, [payload], identity=IDENTITY)
    assert len(report.accepted) == 1


def test_a_value_outside_its_own_scale_is_refused() -> None:
    """A percentage filed as a 0-1 ratio: the scale, the number, or both are wrong."""

    payload = _candidate()
    payload["measurements"][0]["value"] = 57.0  # type: ignore[index]
    payload["measurements"][0]["metric_scale_id"] = "pairwise-win-rate"  # type: ignore[index]
    payload["provenance"]["verbatim_quote"] = "It won 57 of the head-to-head comparisons."  # type: ignore[index]
    report = EvidenceIngestor(now=NOW).ingest(EvidenceLayer.BENCHMARK, [payload], identity=IDENTITY)
    assert report.rejected[0].reason == "VALUE_OUT_OF_SCALE"
