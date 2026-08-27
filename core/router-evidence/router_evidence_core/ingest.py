"""Taking research output and refusing most of it.

The division of labour is fixed: a research assistant searches the public
record and returns candidate records; this module decides which of them are
admissible. It never adds a number, never fills a blank, and never resolves an
ambiguous version. Its output is a validated layer file plus a rejection list
long enough to be informative.

The rejections are the product as much as the acceptances are. A research pass
that produces forty records and no rejections has almost certainly had its
gaps filled in for it somewhere, and the rejection list is where that would
show.

**What gets refused, and why each rule exists**

``MISSING_PROVENANCE``
    No URL on an open-web source. Unverifiable is indistinguishable from
    invented.

``UNQUOTED_NUMBER``
    A numeric value whose ``verbatim_quote`` does not contain the number. The
    single highest-yield check available: a fabricated score almost never
    survives being asked to point at itself in the source text.

``UNATTRIBUTED_STATISTIC``
    A sample size, interval or generation count present without the assertion
    that the source stated it. Enforced by the schema; caught again here so
    the rejection is reported rather than raised.

``VERSION_UNCONFIRMED``
    A binding to an exact version that the source never named, at HIGH
    confidence. Confidence about a mapping nobody published is not confidence.

``ALIAS_AS_SNAPSHOT``
    The bound version is an alias the provider can repoint. The record is kept
    and marked; it may never become a prior.

``SOURCE_TYPE_UNRESOLVED``
    The record named no valid source type and its URL does not settle one.
    Repairing a mislabelled type from an unambiguous host is normalisation;
    deciding what kind of source an unfamiliar blog is would not be.

``SCALE_UNKNOWN``
    A ``metric_scale_id`` outside the registered set. Adding a scale is a
    deliberate act, because a scale is a claim about what a number means.

``FUTURE_TIMESTAMP`` / ``RETRIEVED_BEFORE_PUBLISHED``
    Impossible dates. Cheap to check and a reliable marker of a record that was
    assembled rather than read.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

from pydantic import ValidationError

from .community import detect_spam_signals
from .layers import LAYER_SOURCE_TYPES, EvidenceLayer, LayerIsolationError, layer_for_source_type
from .priors import KNOWN_EXTERNAL_SCALES
from .records import BenchmarkRecord, CommunityRecord, ExternalRecord, OfficialRecord

_SOURCE_TYPES: frozenset[str] = frozenset(
    source_type for types in LAYER_SOURCE_TYPES.values() for source_type in types
)

_LAYER_RECORD_TYPES: dict[EvidenceLayer, type[ExternalRecord]] = {
    EvidenceLayer.OFFICIAL: OfficialRecord,
    EvidenceLayer.BENCHMARK: BenchmarkRecord,
    EvidenceLayer.COMMUNITY: CommunityRecord,
}

_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)*%?")

#: Hosts whose source type is a fact about the URL rather than a judgement
#: about the content. Used only to repair a record that named no valid source
#: type at all — a research pass that writes the *layer* name into
#: `source_type` is making a labelling mistake, not offering a different
#: classification, and rejecting 68 real posts over it would discard evidence
#: for a formatting error.
#:
#: Deliberately short. An unrecognised host stays unresolved and the record is
#: rejected: deciding that some blog is a "creator comparison" rather than a
#: "forum" is exactly the kind of guess this pipeline does not make.
_HOST_SOURCE_TYPES: dict[str, str] = {
    "arxiv.org": "academic_paper",
    "reddit.com": "reddit",
    "x.com": "x",
    "twitter.com": "x",
    "huggingface.co": "huggingface_discussion",
    "discord.com": "discord",
    "discord.gg": "discord",
}


def source_type_from_url(url: str | None) -> str | None:
    """The source type a URL settles on its own, or ``None``.

    GitHub is split by path because an issue and a discussion are different
    venues with different signal, and the URL says which.
    """

    if not url:
        return None
    host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("old.")
    if host in {"github.com"}:
        if "/issues/" in url:
            return "github_issue"
        if "/discussions/" in url:
            return "github_discussion"
        return None
    return _HOST_SOURCE_TYPES.get(host)


@dataclass(frozen=True)
class TargetIdentity:
    """Who we asked about, in our own names rather than the source's.

    A researcher describes a model the way the public record does — "Seedance
    2.5", provider "ByteDance", version "2.5". Those are the source's words and
    they belong in ``source_model_name`` and ``source_model_version``. The
    binding to a model *this platform runs* uses our identifiers, and they come
    from the research target, not from the answer: letting the answer choose
    them is how evidence ends up attached to a model nobody here operates.

    This is normalisation, not judgement. ``version_match`` and
    ``mapping_confidence`` — the two fields that say how well the source's
    model matches ours — are never touched.
    """

    logical_name: str
    provider: str
    model_id: str
    exact_version: str


@dataclass
class Rejection:
    index: int
    record_id: str
    reason: str
    detail: str


@dataclass
class IngestReport:
    layer: EvidenceLayer
    accepted: list[ExternalRecord] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    marked: list[Rejection] = field(default_factory=list)
    considered: int = 0

    @property
    def acceptance_rate(self) -> float:
        return len(self.accepted) / self.considered if self.considered else 0.0

    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in (*self.rejected, *self.marked):
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return dict(sorted(counts.items()))


def _quote_supports(value: float, quote: str) -> bool:
    """Whether the quoted text plausibly contains the number claimed.

    Deliberately generous about formatting and strict about the digits: 0.939
    may appear as "0.939", "93.9%" or "93.9", and each of those is the same
    measurement written differently. It will not accept a quote that contains
    no number at all, which is the case that matters.
    """

    candidates = _NUMBER.findall(quote)
    if not candidates:
        return False
    targets = {
        f"{value:g}",
        f"{value:.1f}",
        f"{value:.2f}",
        f"{value:.3f}",
        f"{value * 100:g}",
        f"{value * 100:.1f}",
        f"{value / 100:g}",
    }
    normalised = {item.replace(",", "").rstrip("%") for item in candidates}
    for target in targets:
        if target in normalised:
            return True
        # Trailing-zero differences: "3.75" quoted as "3.750".
        for item in normalised:
            try:
                if abs(float(item) - float(target)) < 1e-9:
                    return True
            except ValueError:  # pragma: no cover - regex already constrained this
                continue
    return False


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


class EvidenceIngestor:
    """Validate, normalise and proof candidate records for one layer."""

    version = "router-evidence-ingest-v1"

    def __init__(self, *, now: datetime | None = None, strict_quotes: bool = True):
        self.now = now or datetime.now(UTC)
        self.strict_quotes = strict_quotes

    def ingest(
        self,
        layer: EvidenceLayer,
        payload: Sequence[dict[str, object]],
        *,
        identity: TargetIdentity | None = None,
    ) -> IngestReport:
        report = IngestReport(layer=layer, considered=len(payload))
        model = _LAYER_RECORD_TYPES[layer]
        for index, raw in enumerate(payload):
            record_id = str(raw.get("record_id", f"<index {index}>"))
            normalised = self._normalise(layer, dict(raw), identity)
            try:
                record = model.model_validate(normalised)
            except ValidationError as error:
                report.rejected.append(
                    Rejection(index, record_id, "SCHEMA_INVALID", _first_error(error))
                )
                continue
            except LayerIsolationError as error:  # pragma: no cover - defence in depth
                report.rejected.append(
                    Rejection(index, record_id, "SOURCE_CLASS_MISMATCH", str(error))
                )
                continue
            problems, marks = self._proof(record)
            if problems:
                report.rejected.append(
                    Rejection(
                        index,
                        record.record_id,
                        problems[0][0],
                        "; ".join(item[1] for item in problems),
                    )
                )
                continue
            for reason, detail in marks:
                report.marked.append(Rejection(index, record.record_id, reason, detail))
            report.accepted.append(record)
        return report

    def _normalise(
        self,
        layer: EvidenceLayer,
        raw: dict[str, object],
        identity: TargetIdentity | None = None,
    ) -> dict[str, object]:
        """Fill in only what can be derived, never what must be observed.

        Four things are derived: the layer (from the source type, so a mismatch
        is caught by the record's own validator rather than trusted from the
        payload), the binding's identifiers (from the research target — see
        :class:`TargetIdentity`), the retrieval timestamp (this run's clock,
        not the assistant's claim about it), and a community post's content
        hash and spam signals (both pure functions of text already present).
        """

        provenance_raw = raw.get("provenance")
        if isinstance(provenance_raw, dict):
            declared = str(provenance_raw.get("source_type", ""))
            if declared not in _SOURCE_TYPES:
                resolved = source_type_from_url(provenance_raw.get("source_url"))  # type: ignore[arg-type]
                if resolved is not None:
                    provenance_raw["source_type"] = resolved

        binding = raw.get("binding")
        if identity is not None and isinstance(binding, dict):
            binding["logical_name"] = identity.logical_name
            binding["provider"] = identity.provider
            binding["model_id"] = identity.model_id
            binding["exact_version"] = identity.exact_version

        raw.setdefault("layer", layer.value)
        provenance = raw.get("provenance")
        if isinstance(provenance, dict):
            source_type = str(provenance.get("source_type", ""))
            if source_type:
                try:
                    raw["layer"] = layer_for_source_type(source_type).value
                except Exception:  # noqa: BLE001 - reported as a schema failure below
                    pass
            provenance.setdefault("retrieved_at", self.now.isoformat())
            provenance.setdefault("retrieved_by", self.version)
        if layer is EvidenceLayer.COMMUNITY and isinstance(provenance, dict):
            text = str(provenance.get("verbatim_quote", "")) + "\n" + str(provenance.get("summary", ""))
            raw.setdefault("content_hash", content_hash(text.strip().lower()))
            detected = detect_spam_signals(text)
            if detected and not raw.get("spam_signals"):
                raw["spam_signals"] = detected
        return raw

    def _proof(self, record: ExternalRecord) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Checks that need a whole validated record. Returns (fatal, marks)."""

        fatal: list[tuple[str, str]] = []
        marks: list[tuple[str, str]] = []
        provenance = record.provenance

        if provenance.source_url is None and provenance.source_type not in {"discord", "forum"}:
            fatal.append(("MISSING_PROVENANCE", "open-web evidence without a URL"))

        if provenance.retrieved_at > self.now:
            fatal.append(
                (
                    "FUTURE_TIMESTAMP",
                    f"retrieved_at {provenance.retrieved_at.isoformat()} is ahead of now",
                )
            )
        if provenance.published_at:
            published = _parse_date(provenance.published_at)
            if published is not None and published > provenance.retrieved_at:
                fatal.append(
                    (
                        "RETRIEVED_BEFORE_PUBLISHED",
                        f"published {provenance.published_at} after retrieval "
                        f"{provenance.retrieved_at.date()}",
                    )
                )

        for measurement in record.measurements:
            scale = KNOWN_EXTERNAL_SCALES.get(measurement.metric_scale_id)
            if scale is None:
                fatal.append(
                    (
                        "SCALE_UNKNOWN",
                        f"{measurement.metric_scale_id} is not a registered scale",
                    )
                )
            elif measurement.value is not None and scale.bounded:
                # A value outside its own declared scale means the scale is
                # wrong, the number is wrong, or both — most often a
                # percentage filed as a 0-1 ratio. Whichever it is, the record
                # no longer says what it appears to say.
                try:
                    scale.to_unit(measurement.value)
                except ValueError as error:
                    fatal.append(("VALUE_OUT_OF_SCALE", str(error)))
            if measurement.value is not None and self.strict_quotes:
                if not _quote_supports(measurement.value, provenance.verbatim_quote):
                    fatal.append(
                        (
                            "UNQUOTED_NUMBER",
                            f"{measurement.metric_name}={measurement.value} does not appear "
                            "in the quoted source text",
                        )
                    )

        binding = record.binding
        if binding.mapping_confidence == "HIGH" and binding.source_model_version is None:
            fatal.append(
                (
                    "VERSION_UNCONFIRMED",
                    "HIGH mapping confidence without the source naming a version",
                )
            )
        if binding.is_alias:
            marks.append(("ALIAS_AS_SNAPSHOT", f"{binding.model_id} is an alias; kept, never a prior"))
        if binding.version_match == "UNKNOWN":
            marks.append(("VERSION_UNCONFIRMED", "version match could not be established"))

        if isinstance(record, BenchmarkRecord):
            statistics = record.statistics
            if statistics.sample_size is None:
                marks.append(("NO_SAMPLE_SIZE", f"{record.benchmark_name} published no sample size"))
            if record.human_or_automatic == "unstated":
                marks.append(("EVALUATION_METHOD_UNSTATED", record.benchmark_name))
        if isinstance(record, CommunityRecord):
            if record.spam_signals:
                marks.append(("SPAM_SIGNALS", ",".join(record.spam_signals)))
            if record.is_marketing:
                marks.append(("MARKETING", record.author_handle))
            if record.is_bot_suspected:
                marks.append(("BOT_SUSPECTED", record.author_handle))
        return fatal, marks


def deduplicate(records: Sequence[CommunityRecord]) -> tuple[list[CommunityRecord], list[Rejection]]:
    """Collapse identical community posts, keeping the earliest.

    Crossposts and quote-reposts carry the same text to a new venue. The
    earliest is the observation; the rest are its distribution.
    """

    seen: dict[str, CommunityRecord] = {}
    duplicates: list[Rejection] = []
    for index, record in enumerate(
        sorted(records, key=lambda item: (item.provenance.published_at or "", item.record_id))
    ):
        original = seen.get(record.content_hash)
        if original is None:
            seen[record.content_hash] = record
            continue
        duplicates.append(
            Rejection(index, record.record_id, "DUPLICATE_CONTENT", f"same text as {original.record_id}")
        )
    return list(seen.values()), duplicates


def _parse_date(value: str) -> datetime | None:
    for pattern in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _first_error(error: ValidationError) -> str:
    first = error.errors()[0]
    location = ".".join(str(part) for part in first["loc"])
    return f"{location}: {first['msg']}"


__all__ = [
    "EvidenceIngestor",
    "TargetIdentity",
    "source_type_from_url",
    "IngestReport",
    "Rejection",
    "content_hash",
    "deduplicate",
]
