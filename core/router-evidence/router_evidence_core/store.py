"""Loading the three external layers, each from its own file.

Physical separation, not a convention: ``official-v1.json``,
``benchmark-v1.json`` and ``community-v1.json`` are three files with three
schemas and three loaders, and a loader refuses a file whose declared layer is
not the one it was asked for. There is no query that can return records from
two layers, because there is no object that holds two layers' records.

Production observations are not here at all. They live in the database, are
written by the API, and never appear in this module — which is the physical
separation between external evidence and production observation the mandate
asks for.

The files are frozen artefacts with a version in their name. Research adds a
new version; it does not edit a published one. That is what makes a posterior
computed last week reproducible this week.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .layers import EvidenceLayer
from .records import BenchmarkRecord, CommunityRecord, ExternalRecord, OfficialRecord

DEFAULT_EVIDENCE_ROOT = Path(__file__).resolve().parents[3] / "config" / "router-evidence"

LAYER_FILENAMES: dict[EvidenceLayer, str] = {
    EvidenceLayer.OFFICIAL: "official-v1.json",
    EvidenceLayer.BENCHMARK: "benchmark-v1.json",
    EvidenceLayer.COMMUNITY: "community-v1.json",
}


class LayerConflict(BaseModel):
    """Two records that cannot both be right, kept rather than resolved.

    A conflict is a finding. Picking a winner at ingest destroys the only
    signal that says "the public record disagrees about this model", which is
    usually more useful than either number.
    """

    model_config = ConfigDict(frozen=True)

    conflict_id: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=80)
    record_ids: list[str] = Field(min_length=2)
    description: str = Field(min_length=1, max_length=2000)
    resolution: Literal[
        "KEEP_BOTH_INDEPENDENT",
        "PREFER_HIGHER_GRADE",
        "PREFER_EXACT_VERSION",
        "UNRESOLVED",
    ] = "KEEP_BOTH_INDEPENDENT"


class LayerGap(BaseModel):
    """Somewhere the search looked and found nothing.

    Recorded because "no evidence" and "not looked for" are different states,
    and a coverage report that cannot tell them apart is not a coverage report.
    """

    model_config = ConfigDict(frozen=True)

    scope: str = Field(min_length=1, max_length=200)
    scenario: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=1000)
    searched_at: str = Field(min_length=1, max_length=40)


class LayerFile(BaseModel):
    """The envelope every layer file shares."""

    model_config = ConfigDict(frozen=True)

    layer: EvidenceLayer
    layer_version: str = Field(min_length=1, max_length=80)
    frozen_at: str = Field(min_length=1, max_length=40)
    compiled_from: list[dict[str, str]] = Field(default_factory=list)
    research_tool: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=4000)
    conflicts: list[LayerConflict] = Field(default_factory=list)
    gaps: list[LayerGap] = Field(default_factory=list)
    #: Declared on the envelope so the loader can read it without knowing which
    #: subclass it holds. Each subclass narrows it to its own record type, which
    #: is what actually enforces that a benchmark file cannot hold a Reddit post.
    records: list[ExternalRecord] = Field(default_factory=list)


class OfficialLayerFile(LayerFile):
    layer: Literal[EvidenceLayer.OFFICIAL] = EvidenceLayer.OFFICIAL
    records: list[OfficialRecord] = Field(default_factory=list)  # type: ignore[assignment]


class BenchmarkLayerFile(LayerFile):
    layer: Literal[EvidenceLayer.BENCHMARK] = EvidenceLayer.BENCHMARK
    records: list[BenchmarkRecord] = Field(default_factory=list)  # type: ignore[assignment]


class CommunityLayerFile(LayerFile):
    layer: Literal[EvidenceLayer.COMMUNITY] = EvidenceLayer.COMMUNITY
    records: list[CommunityRecord] = Field(default_factory=list)  # type: ignore[assignment]

    @model_validator(mode="after")
    def _unique_record_ids(self) -> CommunityLayerFile:
        ids = [record.record_id for record in self.records]
        if len(ids) != len(set(ids)):
            duplicates = sorted({item for item in ids if ids.count(item) > 1})
            raise ValueError(f"duplicate community record_id(s): {duplicates}")
        return self


_LAYER_MODELS: dict[EvidenceLayer, type[LayerFile]] = {
    EvidenceLayer.OFFICIAL: OfficialLayerFile,
    EvidenceLayer.BENCHMARK: BenchmarkLayerFile,
    EvidenceLayer.COMMUNITY: CommunityLayerFile,
}


@dataclass(frozen=True)
class LayerSnapshot:
    layer: EvidenceLayer
    version: str
    frozen_at: str
    path: Path
    records: tuple[ExternalRecord, ...]
    conflicts: tuple[LayerConflict, ...]
    gaps: tuple[LayerGap, ...]

    def eligible(self) -> tuple[ExternalRecord, ...]:
        return tuple(record for record in self.records if record.prior_eligible)


class EvidenceLayerStore:
    """Read-only access to the frozen external layers.

    Each accessor returns exactly one layer. There is no ``all_records()``:
    the absence is the design, because the one thing this store must never make
    easy is a list with a benchmark score and a Reddit comment in it.
    """

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else DEFAULT_EVIDENCE_ROOT
        self._cache: dict[EvidenceLayer, LayerSnapshot] = {}

    def path_for(self, layer: EvidenceLayer) -> Path:
        return self.root / LAYER_FILENAMES[layer]

    def available(self, layer: EvidenceLayer) -> bool:
        return self.path_for(layer).exists()

    def load(self, layer: EvidenceLayer) -> LayerSnapshot:
        if layer in self._cache:
            return self._cache[layer]
        path = self.path_for(layer)
        if not path.exists():
            snapshot = LayerSnapshot(layer, "absent", "never", path, (), (), ())
            self._cache[layer] = snapshot
            return snapshot
        payload = json.loads(path.read_text("utf-8"))
        declared = payload.get("layer")
        if declared != layer.value:
            raise ValueError(
                f"{path.name} declares layer {declared!r} but was loaded as {layer.value}; "
                "layer files are not interchangeable"
            )
        parsed = _LAYER_MODELS[layer].model_validate(payload)
        snapshot = LayerSnapshot(
            layer=layer,
            version=parsed.layer_version,
            frozen_at=parsed.frozen_at,
            path=path,
            records=tuple(parsed.records),
            conflicts=tuple(parsed.conflicts),
            gaps=tuple(parsed.gaps),
        )
        self._cache[layer] = snapshot
        return snapshot

    def official(self) -> LayerSnapshot:
        return self.load(EvidenceLayer.OFFICIAL)

    def benchmark(self) -> LayerSnapshot:
        return self.load(EvidenceLayer.BENCHMARK)

    def community(self) -> LayerSnapshot:
        return self.load(EvidenceLayer.COMMUNITY)

    def snapshots(self) -> tuple[LayerSnapshot, LayerSnapshot, LayerSnapshot]:
        """The three layers, still separate — a tuple, not a merge."""

        return (self.official(), self.benchmark(), self.community())

    def source_distribution(self) -> dict[str, dict[str, int]]:
        """Record counts by layer and source type, for the coverage report."""

        distribution: dict[str, dict[str, int]] = {}
        for snapshot in self.snapshots():
            per_type: dict[str, int] = {}
            for record in snapshot.records:
                per_type[record.provenance.source_type] = per_type.get(record.provenance.source_type, 0) + 1
            distribution[snapshot.layer.value] = dict(sorted(per_type.items()))
        return distribution


def write_layer_file(path: Path, payload: LayerFile) -> None:
    """Persist a layer file, validated, sorted and newline-terminated.

    Sorting keys makes a re-ingest of unchanged research produce a byte
    identical file, so a diff on these files only ever shows real change.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        payload.model_dump(mode="json"),
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    )
    path.write_text(serialized + "\n", "utf-8")


def records_by_model(snapshot: LayerSnapshot) -> dict[str, list[ExternalRecord]]:
    grouped: dict[str, list[ExternalRecord]] = {}
    for record in snapshot.records:
        grouped.setdefault(record.binding.logical_name, []).append(record)
    return grouped


def unique_sources(records: Sequence[ExternalRecord]) -> int:
    return len({record.provenance.source_url or record.provenance.publisher for record in records})


__all__ = [
    "DEFAULT_EVIDENCE_ROOT",
    "LAYER_FILENAMES",
    "BenchmarkLayerFile",
    "CommunityLayerFile",
    "EvidenceLayerStore",
    "LayerConflict",
    "LayerFile",
    "LayerGap",
    "LayerSnapshot",
    "OfficialLayerFile",
    "records_by_model",
    "unique_sources",
    "write_layer_file",
]
