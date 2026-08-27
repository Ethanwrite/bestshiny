"""Validate, normalise, proof and file the research output.

Reads the raw Grok responses under ``data/router-evidence/raw/``, runs every
record through :class:`~router_evidence_core.ingest.EvidenceIngestor`, and
writes the three frozen layer files under ``config/router-evidence/``.

The rejection report is printed in full and written next to the layer files.
That is not a debugging convenience: a research pass whose rejections are
invisible cannot be distinguished from one that had nothing to reject, and the
whole reason this step is separate from the search is that most of what a
search returns should not survive it.

Usage::

    .venv/bin/python scripts/ingest_router_evidence.py
    .venv/bin/python scripts/ingest_router_evidence.py --layer community_prior --report-only
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core" / "router-evidence"))

from router_evidence_core.ingest import (  # noqa: E402
    EvidenceIngestor,
    IngestReport,
    TargetIdentity,
)
from router_evidence_core.layers import EvidenceLayer  # noqa: E402
from router_evidence_core.store import (  # noqa: E402
    LAYER_FILENAMES,
    BenchmarkLayerFile,
    CommunityLayerFile,
    LayerGap,
    OfficialLayerFile,
    write_layer_file,
)

RAW_ROOT = ROOT / "data" / "router-evidence" / "raw"
CONFIG_ROOT = ROOT / "config" / "router-evidence"
REPORT_PATH = ROOT / "data" / "router-evidence" / "ingest-report.json"

_LAYER_FILES = {
    EvidenceLayer.OFFICIAL: OfficialLayerFile,
    EvidenceLayer.BENCHMARK: BenchmarkLayerFile,
    EvidenceLayer.COMMUNITY: CommunityLayerFile,
}

LAYER_VERSIONS = {
    EvidenceLayer.OFFICIAL: "official-prior-v1",
    EvidenceLayer.BENCHMARK: "benchmark-prior-v1",
    EvidenceLayer.COMMUNITY: "community-prior-v1",
}


def _records_from(envelope: dict[str, object]) -> tuple[list[dict[str, object]], str]:
    """Pull the structured records out of one raw response.

    Returns the records and the researcher's closing note. A response that
    failed, or that the second stage could not structure, yields no records and
    a note explaining which — both of which become gaps rather than silence.
    """

    if not envelope.get("ok"):
        return [], f"research call failed: {str(envelope.get('raw', ''))[:400]}"
    raw = envelope.get("raw")
    if not isinstance(raw, str):
        return [], "no raw payload"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return [], "raw payload was not JSON"
    structured = payload.get("structuredOutput")
    if not isinstance(structured, dict):
        error = payload.get("structuredOutputError")
        return [], f"no structured output ({error or payload.get('stopReason')})"
    records = structured.get("records")
    note = str(structured.get("note", ""))
    if not isinstance(records, list):
        return [], note or "structured output carried no record list"
    return [record for record in records if isinstance(record, dict)], note


def ingest_layer(layer: EvidenceLayer, *, now: datetime) -> tuple[IngestReport, list[LayerGap], list[str]]:
    directory = RAW_ROOT / layer.value
    ingestor = EvidenceIngestor(now=now)
    combined = IngestReport(layer=layer)
    gaps: list[LayerGap] = []
    notes: list[str] = []
    if not directory.exists():
        return combined, gaps, ["no raw research on disk for this layer"]

    for path in sorted(directory.glob("*.json")):
        envelope = json.loads(path.read_text("utf-8"))
        records, note = _records_from(envelope)
        logical_name = str(envelope.get("logical_name", path.stem))
        if note:
            notes.append(f"{logical_name}: {note}")
        if not records:
            gaps.append(
                LayerGap(
                    scope=logical_name,
                    scenario="all",
                    reason=note or "the search returned no admissible source",
                    searched_at=str(envelope.get("requested_at", now.isoformat()))[:10],
                )
            )
            continue
        identity = TargetIdentity(
            logical_name=logical_name,
            provider=str(envelope["provider"]),
            model_id=str(envelope["model_id"]),
            exact_version=str(envelope["exact_version"]),
        )
        report = ingestor.ingest(layer, records, identity=identity)
        combined.considered += report.considered
        combined.accepted.extend(report.accepted)
        combined.rejected.extend(report.rejected)
        combined.marked.extend(report.marked)
        if not report.accepted:
            gaps.append(
                LayerGap(
                    scope=logical_name,
                    scenario="all",
                    reason=(
                        f"{report.considered} candidate record(s) were all refused: "
                        + ", ".join(sorted({item.reason for item in report.rejected}))
                    ),
                    searched_at=str(envelope.get("requested_at", now.isoformat()))[:10],
                )
            )
    return combined, gaps, notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layer", action="append", choices=[layer.value for layer in _LAYER_FILES])
    parser.add_argument("--report-only", action="store_true", help="print the report, write nothing")
    arguments = parser.parse_args()

    now = datetime.now(UTC)
    layers = [EvidenceLayer(value) for value in (arguments.layer or [layer.value for layer in _LAYER_FILES])]
    summary: dict[str, object] = {"generated_at": now.isoformat(), "layers": {}}
    accepted_total = 0
    considered_total = 0

    for layer in layers:
        report, gaps, notes = ingest_layer(layer, now=now)
        accepted_total += len(report.accepted)
        considered_total += report.considered
        by_reason: dict[str, int] = defaultdict(int)
        for item in report.rejected:
            by_reason[item.reason] += 1
        print(f"\n=== {layer.value} ===")
        print(
            f"  considered {report.considered}   accepted {len(report.accepted)}"
            f"   rejected {len(report.rejected)}"
        )
        for reason, count in sorted(by_reason.items()):
            print(f"    reject {reason:28s} {count}")
        for reason, count in sorted(_counts(report.marked).items()):
            print(f"    mark   {reason:28s} {count}")
        print(f"  gaps {len(gaps)}")

        summary["layers"][layer.value] = {  # type: ignore[index]
            "considered": report.considered,
            "accepted": len(report.accepted),
            "rejected": len(report.rejected),
            "reject_reasons": dict(sorted(by_reason.items())),
            "mark_reasons": _counts(report.marked),
            "gaps": len(gaps),
            "rejections": [
                {"record_id": item.record_id, "reason": item.reason, "detail": item.detail}
                for item in report.rejected
            ],
            "notes": notes,
        }

        if arguments.report_only:
            continue
        payload = _LAYER_FILES[layer](
            layer_version=LAYER_VERSIONS[layer],
            frozen_at=now.date().isoformat(),
            research_tool="grok-cli (two stage: search, then structure with the web disabled)",
            compiled_from=[{"source": "data/router-evidence/raw/" + layer.value, "at": now.isoformat()}],
            notes="; ".join(notes)[:4000],
            records=report.accepted,  # type: ignore[arg-type]
            gaps=gaps,
        )
        destination = CONFIG_ROOT / LAYER_FILENAMES[layer]
        write_layer_file(destination, payload)
        print(f"  wrote {destination.relative_to(ROOT)}")

    if not arguments.report_only:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", "utf-8")
        print(f"\nreport {REPORT_PATH.relative_to(ROOT)}")
    if considered_total and not accepted_total:
        # Every candidate refused is a research regression, not a clean run, and
        # a wrapper that keys off the exit status should be able to see it.
        print("\nno record survived ingest", file=sys.stderr)
        return 1
    return 0


def _counts(items: list) -> dict[str, int]:  # type: ignore[type-arg]
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        counts[item.reason] += 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main())
