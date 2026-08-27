"""The three shipped layer files, and the claims made about them.

These are assertions about *data*, not code. They exist because the layer files
are the one part of this work that a later research pass rewrites wholesale,
and a rewrite that quietly drops the isolation properties would otherwise pass
every other test in the suite.
"""

from __future__ import annotations

from collections import Counter

from router_evidence_core import (
    KNOWN_EXTERNAL_SCALES,
    CommunityRecord,
    EvidenceLayer,
    EvidenceLayerStore,
    attach_community_effective_sizes,
    build_coverage,
    build_layer_priors,
    find_conflicts,
    production_contributions,
)
from router_evidence_core.calibration import BRIDGES
from router_evidence_core.community import COMMUNITY_STANCE_SCALE_ID
from router_evidence_core.observations import OutcomeName
from router_evidence_core.priors import SCORING_SCALES


def _store() -> EvidenceLayerStore:
    return EvidenceLayerStore()


def test_all_three_layers_are_on_disk_and_carry_records() -> None:
    for snapshot in _store().snapshots():
        assert snapshot.version != "absent", snapshot.layer
        assert snapshot.records, snapshot.layer


def test_every_record_sits_in_the_layer_its_source_class_belongs_to() -> None:
    """The schema enforces it per record; this asserts the shipped files obey it."""

    for snapshot in _store().snapshots():
        for record in snapshot.records:
            assert record.layer is snapshot.layer, record.record_id


def test_no_source_type_appears_in_two_layers() -> None:
    seen: dict[str, EvidenceLayer] = {}
    for snapshot in _store().snapshots():
        for record in snapshot.records:
            source_type = record.provenance.source_type
            assert seen.setdefault(source_type, snapshot.layer) is snapshot.layer


def test_every_measurement_is_on_a_registered_scale() -> None:
    for snapshot in _store().snapshots():
        for record in snapshot.records:
            for measurement in record.measurements:
                assert measurement.metric_scale_id in KNOWN_EXTERNAL_SCALES


def test_every_numeric_value_lies_inside_its_own_scale() -> None:
    """A percentage filed as a 0-1 ratio is the shape this catches."""

    for snapshot in _store().snapshots():
        for record in snapshot.records:
            for measurement in record.measurements:
                scale = KNOWN_EXTERNAL_SCALES[measurement.metric_scale_id]
                if measurement.value is not None and scale.bounded:
                    scale.to_unit(measurement.value)


def test_every_record_carries_provenance_a_quote_and_a_retrieval_time() -> None:
    for snapshot in _store().snapshots():
        for record in snapshot.records:
            provenance = record.provenance
            assert provenance.verbatim_quote.strip()
            assert provenance.summary.strip()
            assert provenance.retrieved_at is not None
            if provenance.source_type not in {"discord", "forum"}:
                assert provenance.source_url


def test_every_numeric_claim_can_point_at_itself_in_its_quote() -> None:
    """The ingest check, re-asserted against what actually shipped."""

    from router_evidence_core.ingest import _quote_supports

    unsupported: list[str] = []
    for snapshot in _store().snapshots():
        for record in snapshot.records:
            for measurement in record.measurements:
                if measurement.value is None:
                    continue
                if not _quote_supports(measurement.value, record.provenance.verbatim_quote):
                    unsupported.append(f"{record.record_id}:{measurement.metric_name}")
    assert unsupported == []


def test_no_statistic_is_present_without_being_attributed_to_the_source() -> None:
    snapshot = _store().benchmark()
    for record in snapshot.records:
        statistics = getattr(record, "statistics", None)
        if statistics is None:
            continue
        if statistics.sample_size is not None:
            assert statistics.sample_size_stated_by_source
        if statistics.confidence_interval is not None:
            assert statistics.confidence_interval_stated_by_source


def test_no_community_generation_count_is_present_without_attribution() -> None:
    for record in _store().community().records:
        assert isinstance(record, CommunityRecord)
        if record.reported_generation_count is not None:
            assert record.reported_generation_count_stated_by_source


def test_alias_bindings_exist_and_none_of_them_is_prior_eligible() -> None:
    """Near-miss evidence is kept on purpose; kept is not the same as used."""

    aliases = [
        record
        for snapshot in _store().snapshots()
        for record in snapshot.records
        if record.binding.is_alias
    ]
    assert aliases, "no alias-bound records survived; the marking path is untested by the data"
    assert all(not record.prior_eligible for record in aliases)


def test_version_mismatched_evidence_is_kept_and_never_eligible() -> None:
    mismatched = [
        record
        for snapshot in _store().snapshots()
        for record in snapshot.records
        if record.binding.version_match in {"VERSION_MISMATCH", "MODEL_MISMATCH", "UNKNOWN"}
    ]
    assert all(not record.prior_eligible for record in mismatched)


def test_the_community_layer_produces_only_stance_priors() -> None:
    """A stance is not a reading of whatever scale the post happened to quote."""

    priors = build_layer_priors(_store().community())
    assert {prior.key.metric_scale_id for prior in priors} == {COMMUNITY_STANCE_SCALE_ID}


def test_the_community_stance_scale_bridges_to_nothing() -> None:
    """A stance can never be compared with a benchmark score, structurally."""

    from router_evidence_core import may_pool

    assert BRIDGES == ()
    assert COMMUNITY_STANCE_SCALE_ID in SCORING_SCALES
    others = set(KNOWN_EXTERNAL_SCALES) - {COMMUNITY_STANCE_SCALE_ID}
    assert others
    assert not any(may_pool(COMMUNITY_STANCE_SCALE_ID, scale_id) for scale_id in others)


def test_no_external_prior_reaches_a_production_outcome() -> None:
    """The headline property: three layers of evidence, none of it in the posterior."""

    snapshots = _store().snapshots()
    priors = [prior for snapshot in snapshots for prior in build_layer_priors(snapshot)]
    for outcome in OutcomeName:
        admitted, refused = production_contributions(priors, outcome)
        assert admitted == {}
        assert all(item.reason == "NO_CALIBRATION_BRIDGE" for item in refused)


def test_coverage_names_the_models_with_too_little_evidence() -> None:
    snapshots = _store().snapshots()
    coverage = build_coverage(snapshots)
    attach_community_effective_sizes(
        coverage, build_layer_priors(_store().load(EvidenceLayer.COMMUNITY))
    )
    assert coverage.models
    # Every covered model is either backed or explicitly named as insufficient.
    for token, entry in coverage.models.items():
        assert entry.insufficient == (token in coverage.insufficient_models)


def test_conflicts_are_marked_rather_than_resolved() -> None:
    snapshots = _store().snapshots()
    priors = {snapshot.layer: build_layer_priors(snapshot) for snapshot in snapshots}
    conflicts = find_conflicts(snapshots, priors)
    assert conflicts, "the public record disagrees about these models; the report should say so"
    kinds = Counter(item.kind for item in conflicts)
    assert set(kinds) <= {"WITHIN_SCALE_DISAGREEMENT", "COMMUNITY_STANCE_SPLIT"}
    # A conflict only ever compares numbers on one scale for one key.
    for conflict in conflicts:
        assert conflict.key.metric_scale_id


def test_community_records_are_deduplicated_in_the_shipped_file() -> None:
    hashes = [record.content_hash for record in _store().community().records]
    assert len(hashes) == len(set(hashes))


def test_gaps_are_recorded_where_the_search_found_nothing() -> None:
    """"No evidence" and "not looked for" are different states."""

    total = sum(len(snapshot.gaps) for snapshot in _store().snapshots())
    assert total > 0
    for snapshot in _store().snapshots():
        for gap in snapshot.gaps:
            assert gap.reason.strip() and gap.searched_at.strip()
