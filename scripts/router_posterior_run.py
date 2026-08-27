"""Compute the offline posterior, replay history against it, and report.

One command, four steps, none of which touch routing:

1. read every production observation from ``router_observations``;
2. build the hierarchical posterior and save it as an immutable run;
3. replay history — fit on the earlier window, score on the later one — and
   save the result with its verdict;
4. print the coverage, conflict and contamination report.

Step 3 is the gate. ``feature_router_lcb`` may only be switched on for a
deployment whose latest replay for the outcome in question is on file and
passed, and this is the command that puts one there. The exit code says so:
0 when the replay passed, 3 when it ran and did not. A non-zero exit is a
result, not an error.

The external evidence layers are read too, but only to be reported. The
production posterior is built from production observations alone: every
external prior is offered to it and refused for want of a calibration bridge,
and the refusals are printed so that the emptiness is visible rather than
implied.

Usage::

    .venv/bin/python scripts/router_posterior_run.py
    .venv/bin/python scripts/router_posterior_run.py --dry-run --outcome accepted_output
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package in (
    "core/router-evidence",
    "core/model-registry",
    "core/external-evidence",
    "packages/domain",
    "packages/database",
    "packages/shared",
    "packages/provider-sdk",
):
    sys.path.insert(0, str(ROOT / package))

from platform_database import Database  # noqa: E402
from platform_shared import Settings  # noqa: E402
from router_evidence_core import (  # noqa: E402
    EvidenceLayer,
    EvidenceLayerStore,
    HierarchicalPosteriorEngine,
    OutcomeName,
    ReplayHarness,
    attach_community_effective_sizes,
    audit_contamination,
    build_coverage,
    build_layer_priors,
    find_conflicts,
    prior_summary,
    production_contributions,
)
from router_evidence_core.calibration import BRIDGES  # noqa: E402
from router_evidence_core.replay import fixed_order_policy  # noqa: E402
from router_evidence_core.service import (  # noqa: E402
    RouterObservationService,
    summarize_observations,
)

REPORT_PATH = ROOT / "data" / "router-evidence" / "posterior-report.json"


def _baseline_order(observations) -> list[str]:  # type: ignore[no-untyped-def]
    """The baseline policy's preference order, taken from history itself.

    The router as it stands is driven by hand-authored capability priors that
    this script cannot evaluate without building the whole container. What it
    can do faithfully is reproduce the *choice* the platform actually made:
    the model that was picked most often is what the incumbent policy prefers.
    That makes the baseline arm the one history really used, which is the
    comparison that matters.
    """

    counts: dict[str, int] = {}
    for observation in observations:
        key = f"{observation.provider}:{observation.model_id}"
        counts[key] = counts.get(key, 0) + 1
    return [key for key, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outcome", default=OutcomeName.ACCEPTED_OUTPUT.value)
    parser.add_argument("--fit-fraction", type=float, default=0.6)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--dry-run", action="store_true", help="compute and print, save nothing")
    parser.add_argument("--strict-isolation", action="store_true", help="disable all partial pooling")
    arguments = parser.parse_args()

    outcome = OutcomeName(arguments.outcome)
    settings = Settings()
    database = Database(settings.database_url)
    service = RouterObservationService(database)
    observations = service.observations()
    run_id = arguments.run_id or f"posterior-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"

    store = EvidenceLayerStore()
    snapshots = store.snapshots()
    priors_by_layer = {snapshot.layer: build_layer_priors(snapshot) for snapshot in snapshots}
    all_priors = [prior for priors in priors_by_layer.values() for prior in priors]
    admitted, refused = production_contributions(all_priors, outcome)

    engine = HierarchicalPosteriorEngine(
        strict_isolation=arguments.strict_isolation,
        prior_version="+".join(f"{snapshot.layer.value}:{snapshot.version}" for snapshot in snapshots),
    )
    run = engine.compute(observations, run_id=run_id, external_priors=admitted)
    contamination = audit_contamination(run)

    coverage = build_coverage(snapshots, posterior_run=run)
    attach_community_effective_sizes(coverage, priors_by_layer.get(EvidenceLayer.COMMUNITY, []))
    conflicts = find_conflicts(snapshots, priors_by_layer)

    replay = None
    if len(observations) >= 20:
        replay = ReplayHarness(engine=HierarchicalPosteriorEngine()).run(
            observations,
            run_id=f"{run_id}-replay",
            baseline_policy=fixed_order_policy(_baseline_order(observations)),
            outcome=outcome,
            fit_fraction=arguments.fit_fraction,
        )

    report = {
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "observations": summarize_observations(observations),
        "quarantined": run.quarantined,
        "posterior_rows": len(run.records),
        "sufficient_cells": sum(1 for record in run.scenario_records() if record.sufficient),
        "layers": {
            snapshot.layer.value: {
                "version": snapshot.version,
                "records": len(snapshot.records),
                "prior_eligible": len(snapshot.eligible()),
                "prior_summary": prior_summary(priors_by_layer[snapshot.layer]),
                "gaps": len(snapshot.gaps),
            }
            for snapshot in snapshots
        },
        "source_distribution": store.source_distribution(),
        "external_priors_admitted": sum(len(value) for value in admitted.values()),
        "external_priors_refused": len(refused),
        "external_refusal_reasons": sorted({item.reason for item in refused}),
        "calibration_bridges": len(BRIDGES),
        "insufficient_models": coverage.insufficient_models,
        "unconfirmed_versions": coverage.unconfirmed_versions,
        "conflicts": [
            {"id": item.conflict_id, "kind": item.kind, "key": item.key.token} for item in conflicts
        ],
        "contamination": [
            {"kind": item.kind, "detail": item.detail} for item in contamination
        ],
    }
    if replay is not None:
        report["replay"] = {
            "run_id": replay.run_id,
            "outcome": replay.outcome.value,
            "fit_observations": replay.fit_observations,
            "eval_observations": replay.eval_observations,
            "contexts": replay.contexts,
            "unscored_contexts": replay.unscored_contexts,
            "baseline_mean_regret": replay.baseline.mean_regret,
            "posterior_mean_regret": replay.posterior.mean_regret,
            "baseline_failure_rate": replay.baseline.failure_rate,
            "posterior_failure_rate": replay.posterior.failure_rate,
            "baseline_cost_credits_mean": replay.baseline.cost_credits_mean,
            "posterior_cost_credits_mean": replay.posterior.cost_credits_mean,
            "baseline_quality_mean": replay.baseline.quality_mean,
            "posterior_quality_mean": replay.posterior.quality_mean,
            "coverage_nominal": replay.coverage.nominal,
            "coverage_observed": replay.coverage.observed,
            "coverage_cells_checked": replay.coverage.cells_checked,
            "passed": replay.passed,
            "failure_reasons": replay.failure_reasons(),
            "notes": replay.notes,
        }

    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))

    if not arguments.dry_run:
        service.save_posterior_run(run)
        if replay is not None:
            service.save_replay(replay, posterior_run_id=run_id)
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", "utf-8"
        )
        print(f"\nsaved posterior run {run_id} and report {REPORT_PATH.relative_to(ROOT)}")

    if contamination:
        print("\nCONTAMINATION FOUND — do not enable the LCB flag", file=sys.stderr)
        return 4
    if replay is None:
        print("\nno replay: fewer than 20 production observations on file", file=sys.stderr)
        return 2
    return 0 if replay.passed else 3


if __name__ == "__main__":
    raise SystemExit(main())
