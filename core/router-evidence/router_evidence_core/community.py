"""Turning a pile of posts into a number that admits how thin it is.

Community evidence is the only layer where the count in front of you is
actively misleading. Twenty posts about Kling losing a face can be twenty
people, or one person and nineteen reposts, or one marketing account and
nineteen replies quoting it. The count says twenty in all three cases.

So nothing here reports a count. It reports an *effective* sample size, after:

* exact duplicates are collapsed by content hash,
* declared duplicates are dropped,
* marketing and suspected automation are dropped,
* second-hand reports are dropped and paraphrases are discounted,
* and repeated reports from the same author on the same key decay
  harmonically — the second post from someone counts for half, the third for a
  third, and so on.

The last one is the important one. A person who is right is still one person,
and someone who posts about the same model every day is not a larger study.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from .keys import EvidenceKey
from .records import CommunityRecord

#: Weight by how directly the author touched the model. "secondhand" and
#: "unclear" never reach here — they are ineligible — so the table only has to
#: separate a person who ran it from a person accurately relaying someone who
#: did.
EXPERIENCE_WEIGHT: dict[str, float] = {"firsthand": 1.0, "paraphrased": 0.35}

#: Weight by how well the venue and the post support checking the claim. This
#: is credibility of the *record*, and it multiplies rather than gates: a grade
#: C community post is still evidence, just quieter.
CREDIBILITY_WEIGHT: dict[str, float] = {"A": 1.0, "B": 0.8, "C": 0.5, "D": 0.25}

#: Signals that mark a post as promotional rather than experiential. Kept as a
#: list so the reason survives into the report; a filtered record is much less
#: useful when nobody can see why it was filtered.
DEFAULT_SPAM_TOKENS: frozenset[str] = frozenset(
    {
        "affiliate",
        "referral",
        "promo code",
        "discount code",
        "sign up with my link",
        "dm for access",
        "cheapest api",
        "resell",
    }
)


@dataclass(frozen=True)
class WeightedRecord:
    record: CommunityRecord
    #: Evidential mass, including the credibility discount. Feeds the prior.
    weight: float
    #: Observation mass, excluding credibility. Feeds the effective sample size.
    #: Two numbers because they answer two questions: "how many independent
    #: first-hand reports is this really" and "how much should they be allowed
    #: to move a prior". Folding them together makes twenty separate people
    #: look like ten observations, which is neither.
    observation_weight: float
    author_rank: int
    reasons: tuple[str, ...]


@dataclass
class CommunityAggregate:
    """What one isolation key's community evidence adds up to.

    ``observation_count`` and ``effective_sample_size`` are both reported
    because the gap between them is the finding. A key with 31 posts and an ESS
    of 2.4 is a key where three people had opinions.

    ``effective_sample_size`` counts observations after deduplication, the
    repeat-author decay and the first-hand discount. ``weight_sum`` additionally
    applies the credibility discount and is what the prior is built from.
    """

    key: EvidenceKey
    observation_count: int = 0
    unique_authors: int = 0
    effective_sample_size: float = 0.0
    weight_sum: float = 0.0
    stance_counts: dict[str, int] = field(default_factory=dict)
    stance_weight: dict[str, float] = field(default_factory=dict)
    failure_modes: dict[str, int] = field(default_factory=dict)
    firsthand_count: int = 0
    paraphrased_count: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    #: Net stance on [-1, 1], weighted. Emphatically *not* a benchmark value —
    #: it is carried on its own scale id and can never be pooled with one.
    stance_score: float | None = None

    @property
    def has_conflict(self) -> bool:
        """Whether the community meaningfully disagrees with itself.

        Both directions carrying real weight is a finding worth surfacing, not
        noise to be averaged away — it usually means the failure is
        conditional on something the posts do not share.
        """

        positive = self.stance_weight.get("positive", 0.0)
        negative = self.stance_weight.get("negative", 0.0)
        total = positive + negative
        if total <= 0:
            return False
        return min(positive, negative) / total >= 0.3


#: The scale community stance is reported on. It exists so that a stance can
#: never be silently compared with a benchmark score: nothing bridges
#: ``community-stance-net`` to any other scale id.
COMMUNITY_STANCE_SCALE_ID = "community-stance-net"


def detect_spam_signals(text: str, tokens: frozenset[str] = DEFAULT_SPAM_TOKENS) -> list[str]:
    lowered = text.lower()
    return sorted(token for token in tokens if token in lowered)


class CommunityAggregator:
    """Deduplicate, filter, weight and count community records.

    Stateless between calls and deterministic: the same records in any order
    produce the same aggregate, because ordering for the author-decay is by
    publication time with the record id as the tiebreak.
    """

    def __init__(
        self,
        *,
        experience_weight: dict[str, float] | None = None,
        credibility_weight: dict[str, float] | None = None,
        author_decay: bool = True,
    ):
        self.experience_weight = dict(experience_weight or EXPERIENCE_WEIGHT)
        self.credibility_weight = dict(credibility_weight or CREDIBILITY_WEIGHT)
        self.author_decay = author_decay

    def _admit(self, record: CommunityRecord) -> tuple[bool, tuple[str, ...]]:
        reasons = record.ineligibility_reasons
        return (not reasons, reasons)

    def weigh(self, records: list[CommunityRecord]) -> tuple[list[WeightedRecord], dict[str, int]]:
        """Apply every filter, then the author decay, in that order.

        Filtering first matters: a marketing account's three posts should not
        consume the first three slots of the decay and push a real user's
        report down to a third of a vote.
        """

        excluded: dict[str, int] = defaultdict(int)
        seen_hashes: set[str] = set()
        admitted: list[CommunityRecord] = []
        for record in sorted(records, key=lambda item: (item.provenance.published_at or "", item.record_id)):
            if record.content_hash in seen_hashes:
                excluded["DUPLICATE_CONTENT_HASH"] += 1
                continue
            ok, reasons = self._admit(record)
            if not ok:
                for reason in reasons:
                    excluded[reason] += 1
                continue
            seen_hashes.add(record.content_hash)
            admitted.append(record)

        author_seen: dict[str, int] = defaultdict(int)
        weighted: list[WeightedRecord] = []
        for record in admitted:
            author_seen[record.author_key] += 1
            rank = author_seen[record.author_key]
            decay = 1.0 / rank if self.author_decay else 1.0
            observation_weight = self.experience_weight.get(record.experience, 0.0) * decay
            weight = observation_weight * self.credibility_weight.get(record.credibility, 0.0)
            notes: list[str] = []
            if rank > 1:
                notes.append(f"REPEAT_AUTHOR_RANK_{rank}")
            if record.experience == "paraphrased":
                notes.append("PARAPHRASED")
            if weight > 0:
                weighted.append(
                    WeightedRecord(record, weight, observation_weight, rank, tuple(notes))
                )
            else:
                excluded["ZERO_WEIGHT"] += 1
        return weighted, dict(excluded)

    def aggregate(self, key: EvidenceKey, records: list[CommunityRecord]) -> CommunityAggregate:
        weighted, excluded = self.weigh(records)
        aggregate = CommunityAggregate(key=key, excluded=excluded)
        aggregate.observation_count = len(weighted)
        aggregate.unique_authors = len({item.record.author_key for item in weighted})
        if not weighted:
            return aggregate

        stance_counts: dict[str, int] = defaultdict(int)
        stance_weight: dict[str, float] = defaultdict(float)
        failure_modes: dict[str, int] = defaultdict(int)
        weights = [item.weight for item in weighted]
        for item in weighted:
            stance_counts[item.record.stance] += 1
            stance_weight[item.record.stance] += item.weight
            for mode in item.record.failure_modes:
                failure_modes[mode] += 1
            if item.record.experience == "firsthand":
                aggregate.firsthand_count += 1
            else:
                aggregate.paraphrased_count += 1

        aggregate.weight_sum = sum(weights)
        # The sum of observation weights, not Kish's design effect. Kish
        # answers "how much variance did unequal weights add", which for
        # twenty posts by one person comes out near eight — a number with no
        # useful reading here. These weights are deliberate discounts, so
        # their sum *is* the count: one person's twenty posts total 1 + 1/2 +
        # ... + 1/20, about 3.6 observations.
        aggregate.effective_sample_size = sum(item.observation_weight for item in weighted)
        aggregate.stance_counts = dict(stance_counts)
        aggregate.stance_weight = dict(stance_weight)
        aggregate.failure_modes = dict(failure_modes)

        positive = stance_weight.get("positive", 0.0)
        negative = stance_weight.get("negative", 0.0)
        mixed = stance_weight.get("mixed", 0.0)
        directional = positive + negative + mixed
        if directional > 0:
            # Mixed reports pull towards zero rather than being discarded; they
            # are the most common honest answer about a video model.
            aggregate.stance_score = (positive - negative) / directional
        return aggregate


def group_by_key(
    records: list[CommunityRecord],
    *,
    key_builder: Callable[[CommunityRecord, int], EvidenceKey],
) -> dict[EvidenceKey, list[CommunityRecord]]:  # pragma: no cover - thin helper, exercised via ingest
    """Split records by isolation key.

    ``key_builder`` takes the record and the index of the measurement inside it,
    because one post can carry more than one scene ("great motion, awful text")
    and each scene is a separate key.
    """

    grouped: dict[EvidenceKey, list[CommunityRecord]] = defaultdict(list)
    for record in records:
        for index in range(len(record.measurements)):
            grouped[key_builder(record, index)].append(record)
    return dict(grouped)


__all__ = [
    "COMMUNITY_STANCE_SCALE_ID",
    "CREDIBILITY_WEIGHT",
    "DEFAULT_SPAM_TOKENS",
    "EXPERIENCE_WEIGHT",
    "CommunityAggregate",
    "CommunityAggregator",
    "WeightedRecord",
    "detect_spam_signals",
    "group_by_key",
]
