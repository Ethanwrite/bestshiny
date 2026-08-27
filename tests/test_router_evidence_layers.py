"""The walls between the four layers, and the schema that refuses invented data."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from external_evidence_core import PRIOR_ELIGIBLE_MATCHES as REGISTRY_ELIGIBLE_MATCHES
from pydantic import ValidationError
from router_evidence_core import (
    DEFAULT_PRIOR_STRENGTHS,
    EXTERNAL_LAYERS,
    BenchmarkLayerFile,
    BenchmarkRecord,
    CommunityRecord,
    EvidenceLayer,
    EvidenceLayerStore,
    Measurement,
    ModelBinding,
    OfficialRecord,
    Provenance,
    ReferenceMode,
    SampleStatistics,
    ScaleKind,
    Scenario,
    SourceClassMismatch,
    TaskType,
    layer_for_source_type,
    write_layer_file,
)
from router_evidence_core.keys import EvidenceKey, MetricScale
from router_evidence_core.records import PRIOR_ELIGIBLE_MATCHES

NOW = datetime(2026, 8, 26, tzinfo=UTC)


def _provenance(source_type: str, quote: str = "the score was 0.87") -> Provenance:
    return Provenance(
        source_url="https://example.invalid/report",
        source_type=source_type,
        publisher="Example",
        published_at="2026-06-01",
        retrieved_at=NOW,
        retrieved_by="test",
        verbatim_quote=quote,
        summary="A summary of the claim.",
    )


def _binding(**overrides: object) -> ModelBinding:
    values: dict[str, object] = {
        "logical_name": "wan-2.7-official",
        "provider": "wan",
        "model_id": "wan-2.7",
        "source_model_name": "Wan 2.7",
        "source_model_version": "2.7",
        "exact_version": "wan-2.7",
        "version_match": "EXACT",
        "mapping_confidence": "HIGH",
        "mapping_rationale": "The report names Wan 2.7 explicitly.",
    }
    values.update(overrides)
    return ModelBinding(**values)  # type: ignore[arg-type]


def _measurement(**overrides: object) -> Measurement:
    values: dict[str, object] = {
        "metric_name": "physics",
        "value": 0.87,
        "metric_scale_id": "vbench2-dimension-0-1",
        "scenario": Scenario.PHYSICS,
        "task_type": TaskType.T2V,
        "scenario_mapping_rationale": "The dimension is named physics.",
    }
    values.update(overrides)
    return Measurement(**values)  # type: ignore[arg-type]


def _benchmark(**overrides: object) -> BenchmarkRecord:
    values: dict[str, object] = {
        "record_id": "bench-1",
        "provenance": _provenance("independent_benchmark"),
        "binding": _binding(),
        "measurements": [_measurement()],
        "credibility": "B",
        "credibility_rationale": "Independent, protocol published.",
        "benchmark_name": "ExampleBench",
        "evaluation_method": "automatic scoring over 300 prompts",
        "evaluator": "Example Lab",
        "human_or_automatic": "automatic",
    }
    values.update(overrides)
    return BenchmarkRecord(**values)  # type: ignore[arg-type]


def test_a_source_type_belongs_to_exactly_one_layer() -> None:
    assert layer_for_source_type("reddit") is EvidenceLayer.COMMUNITY
    assert layer_for_source_type("arena_leaderboard") is EvidenceLayer.BENCHMARK
    assert layer_for_source_type("official_model_card") is EvidenceLayer.OFFICIAL
    with pytest.raises(SourceClassMismatch):
        layer_for_source_type("a_blog_someone_liked")


def test_a_reddit_thread_cannot_be_filed_as_a_benchmark() -> None:
    """The failure mode is not malice, it is a careful write-up that reads like a study."""

    with pytest.raises(ValidationError, match="community_prior"):
        _benchmark(provenance=_provenance("reddit"))


def test_production_is_not_an_external_layer() -> None:
    assert EvidenceLayer.PRODUCTION not in EXTERNAL_LAYERS
    assert EvidenceLayer.PRODUCTION not in DEFAULT_PRIOR_STRENGTHS


def test_this_package_agrees_with_the_frozen_registry_on_eligibility() -> None:
    """Two definitions of the same rule are one definition and one future divergence."""

    assert PRIOR_ELIGIBLE_MATCHES == REGISTRY_ELIGIBLE_MATCHES


def test_a_sample_size_that_the_source_never_stated_is_refused() -> None:
    with pytest.raises(ValidationError, match="sample_size"):
        SampleStatistics(sample_size=1000)
    assert SampleStatistics(sample_size=1000, sample_size_stated_by_source=True).sample_size == 1000


def test_an_unstated_confidence_interval_is_refused() -> None:
    with pytest.raises(ValidationError, match="confidence_interval"):
        SampleStatistics(confidence_interval="+/- 5")


def test_a_community_generation_count_cannot_be_guessed() -> None:
    with pytest.raises(ValidationError, match="reported_generation_count"):
        CommunityRecord(
            record_id="c1",
            provenance=_provenance("reddit", "I ran it a bunch of times"),
            binding=_binding(),
            measurements=[_measurement(value=None)],
            credibility="C",
            credibility_rationale="One user report.",
            author_handle="someone",
            author_key="reddit:someone",
            venue="r/aivideo",
            stance="negative",
            experience="firsthand",
            content_hash="0123456789abcdef",
            reported_generation_count=40,
        )


def test_exact_version_needs_the_source_to_have_named_one() -> None:
    with pytest.raises(ValidationError, match="EXACT"):
        _binding(source_model_version=None)
    relaxed = _binding(source_model_version=None, version_match="EXACT_VERSION_UNSPECIFIED_REVISION")
    assert relaxed.version_match == "EXACT_VERSION_UNSPECIFIED_REVISION"


def test_an_alias_binding_is_kept_and_never_eligible() -> None:
    record = _benchmark(binding=_binding(is_alias=True))
    assert record.prior_eligible is False
    assert "ALIAS_NOT_SNAPSHOT" in record.ineligibility_reasons


def test_ineligibility_reports_every_reason_not_the_first() -> None:
    record = _benchmark(
        credibility="D",
        binding=_binding(version_match="VERSION_MISMATCH", mapping_confidence="LOW"),
    )
    assert set(record.ineligibility_reasons) == {
        "CREDIBILITY_D",
        "VERSION_MISMATCH",
        "MAPPING_CONFIDENCE_LOW",
    }


def test_official_evidence_without_a_url_is_refused() -> None:
    with pytest.raises(ValidationError, match="source_url"):
        Provenance(
            source_url=None,
            source_type="official_model_card",
            publisher="Vendor",
            retrieved_at=NOW,
            retrieved_by="test",
            verbatim_quote="q",
            summary="s",
        )


def test_a_closed_venue_may_be_recorded_without_a_url() -> None:
    provenance = Provenance(
        source_url=None,
        source_type="discord",
        publisher="A server",
        retrieved_at=NOW,
        retrieved_by="test",
        verbatim_quote="identity drifts after two seconds",
        summary="one report",
    )
    assert provenance.source_url is None


def test_the_store_refuses_a_file_whose_declared_layer_is_wrong(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "benchmark-v1.json"
    payload = BenchmarkLayerFile(
        layer_version="benchmark-prior-v1", frozen_at="2026-08-26", records=[_benchmark()]
    )
    write_layer_file(path, payload)
    text = path.read_text("utf-8").replace('"benchmark_prior"', '"official_prior"', 1)
    path.write_text(text, "utf-8")
    store = EvidenceLayerStore(tmp_path)
    with pytest.raises(ValueError, match="not interchangeable"):
        store.benchmark()


def test_a_missing_layer_file_loads_as_an_absent_snapshot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Absent is a state with a name, not an exception and not an empty success."""

    store = EvidenceLayerStore(tmp_path)
    snapshot = store.community()
    assert snapshot.version == "absent"
    assert snapshot.records == ()


def test_the_store_offers_no_way_to_get_two_layers_records_together() -> None:
    """The absence of an ``all_records`` accessor is the design, so assert it."""

    assert not hasattr(EvidenceLayerStore, "all_records")
    assert not hasattr(EvidenceLayerStore, "merged")


def test_layer_files_round_trip_byte_identically(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "benchmark-v1.json"
    payload = BenchmarkLayerFile(
        layer_version="benchmark-prior-v1", frozen_at="2026-08-26", records=[_benchmark()]
    )
    write_layer_file(path, payload)
    first = path.read_bytes()
    write_layer_file(path, payload)
    assert path.read_bytes() == first
    snapshot = EvidenceLayerStore(tmp_path).benchmark()
    assert [item.record_id for item in snapshot.records] == ["bench-1"]


def test_a_scale_places_values_only_within_itself() -> None:
    likert = MetricScale(scale_id="likert-1-5", kind=ScaleKind.LIKERT_1_5, description="d")
    assert likert.to_unit(5.0) == 1.0
    assert likert.to_unit(3.0) == 0.5
    with pytest.raises(ValueError, match="outside the bounds"):
        likert.to_unit(0.0)


def test_an_unbounded_scale_has_no_probability_reading() -> None:
    elo = MetricScale(scale_id="arena-elo", kind=ScaleKind.ELO, description="d")
    assert elo.bounded is False
    with pytest.raises(ValueError, match="unbounded"):
        elo.to_unit(1154.0)


def test_a_lower_is_better_scale_inverts_when_placed_on_the_unit_axis() -> None:
    rank = MetricScale(
        scale_id="leaderboard-rank",
        kind=ScaleKind.PERCENT_0_100,
        higher_is_better=False,
        description="d",
    )
    assert rank.to_unit(0.0) == 1.0


def test_the_router_key_is_narrower_than_the_evidence_key() -> None:
    key = EvidenceKey(
        provider="wan",
        model_id="wan-2.7",
        exact_version="wan-2.7",
        task_type=TaskType.I2V,
        scenario=Scenario.IDENTITY,
        metric_scale_id="prod.accepted-output",
    )
    assert key.router_model_key == "wan:wan-2.7"
    assert "I2V" in key.token and "identity" in key.token


def test_a_scale_id_must_be_a_token_not_a_display_name() -> None:
    with pytest.raises(ValidationError, match="lowercase"):
        MetricScale(scale_id="VBench 2.0 Total", kind=ScaleKind.RATIO_0_1, description="d")


def test_official_records_declare_what_kind_of_claim_they_are() -> None:
    record = OfficialRecord(
        record_id="off-1",
        provenance=_provenance("official_model_card", "maximum duration is 10 seconds"),
        binding=_binding(),
        measurements=[
            _measurement(
                metric_name="max_duration_seconds",
                value=None,
                metric_scale_id="percent-0-100",
                scenario=Scenario.GENERIC,
            )
        ],
        credibility="A",
        credibility_rationale="The vendor's own model card.",
        claim_kind="capability_limit",
    )
    assert record.self_reported is True
    assert record.prior_eligible is True


def test_reference_mode_is_part_of_the_measurement_not_a_footnote() -> None:
    measurement = _measurement(reference_mode=ReferenceMode.FIRST_LAST_FRAME)
    assert measurement.reference_mode is ReferenceMode.FIRST_LAST_FRAME
