"""Community evidence: deduplicated, filtered, and counted as fewer than it looks."""

from __future__ import annotations

from datetime import UTC, datetime

from router_evidence_core import (
    CommunityAggregator,
    CommunityRecord,
    EvidenceKey,
    Measurement,
    ModelBinding,
    Provenance,
    Scenario,
    TaskType,
    content_hash,
)
from router_evidence_core.community import detect_spam_signals

NOW = datetime(2026, 8, 26, tzinfo=UTC)
KEY = EvidenceKey(
    provider="openrouter",
    model_id="kwaivgi/kling-v3.0-pro",
    exact_version="kling-v3.0-pro",
    task_type=TaskType.I2V,
    scenario=Scenario.IDENTITY,
    metric_scale_id="community-stance-net",
)


def _post(
    record_id: str,
    author: str,
    *,
    stance: str = "negative",
    experience: str = "firsthand",
    credibility: str = "C",
    published: str = "2026-07-01",
    text: str | None = None,
    marketing: bool = False,
    bot: bool = False,
    duplicate_of: str | None = None,
    failure_modes: tuple[str, ...] = ("identity_drift",),
) -> CommunityRecord:
    body = text or f"{record_id} face changes after two seconds"
    return CommunityRecord(
        record_id=record_id,
        provenance=Provenance(
            source_url=f"https://reddit.invalid/{record_id}",
            source_type="reddit",
            publisher="r/aivideo",
            published_at=published,
            retrieved_at=NOW,
            retrieved_by="test",
            verbatim_quote=body,
            summary=body,
        ),
        binding=ModelBinding(
            logical_name="kling-3-pro-openrouter",
            provider="openrouter",
            model_id="kwaivgi/kling-v3.0-pro",
            source_model_name="Kling 3.0 Pro",
            source_model_version="3.0",
            exact_version="kling-v3.0-pro",
            version_match="EXACT",
            mapping_confidence="MEDIUM",
            mapping_rationale="Author names Kling 3.0 Pro.",
        ),
        measurements=[
            Measurement(
                metric_name="identity retention",
                value=None,
                metric_scale_id="community-stance-net",
                scenario=Scenario.IDENTITY,
                task_type=TaskType.I2V,
                scenario_mapping_rationale="The complaint is about a face changing.",
            )
        ],
        credibility=credibility,  # type: ignore[arg-type]
        credibility_rationale="A user report.",
        author_handle=author,
        author_key=f"reddit:{author}",
        venue="r/aivideo",
        stance=stance,  # type: ignore[arg-type]
        experience=experience,  # type: ignore[arg-type]
        failure_modes=list(failure_modes),
        is_marketing=marketing,
        is_bot_suspected=bot,
        duplicate_of=duplicate_of,
        content_hash=content_hash(body),
    )


def test_twenty_posts_by_one_person_are_not_twenty_observations() -> None:
    """The headline number this layer exists to avoid producing."""

    records = [_post(f"p{i}", "loud_user", text=f"take {i}: the face changes") for i in range(20)]
    aggregate = CommunityAggregator().aggregate(KEY, records)
    assert aggregate.observation_count == 20
    assert aggregate.unique_authors == 1
    # 1 + 1/2 + ... + 1/20 — the harmonic decay, not twenty and not one.
    assert 3.0 < aggregate.effective_sample_size < 4.0


def test_twenty_posts_by_twenty_people_count_as_twenty() -> None:
    records = [_post(f"p{i}", f"user{i}", text=f"user {i}: the face changes") for i in range(20)]
    aggregate = CommunityAggregator().aggregate(KEY, records)
    assert aggregate.unique_authors == 20
    assert aggregate.effective_sample_size == 20.0


def test_identical_text_collapses_however_many_venues_carried_it() -> None:
    records = [_post(f"p{i}", f"user{i}", text="the same crossposted paragraph") for i in range(5)]
    aggregate = CommunityAggregator().aggregate(KEY, records)
    assert aggregate.observation_count == 1
    assert aggregate.excluded["DUPLICATE_CONTENT_HASH"] == 4


def test_marketing_and_bots_are_recorded_then_excluded() -> None:
    records = [
        _post("real", "someone"),
        _post("ad", "brand_account", marketing=True, text="ad copy"),
        _post("bot", "auto_poster", bot=True, text="bot copy"),
    ]
    aggregate = CommunityAggregator().aggregate(KEY, records)
    assert aggregate.observation_count == 1
    assert aggregate.excluded["MARKETING"] == 1
    assert aggregate.excluded["BOT_SUSPECTED"] == 1


def test_secondhand_reports_are_dropped_and_paraphrases_discounted() -> None:
    firsthand = CommunityAggregator().aggregate(KEY, [_post("a", "u1")])
    paraphrased = CommunityAggregator().aggregate(
        KEY, [_post("b", "u2", experience="paraphrased", text="someone said the face changes")]
    )
    secondhand = CommunityAggregator().aggregate(
        KEY, [_post("c", "u3", experience="secondhand", text="I heard it drifts")]
    )
    assert firsthand.weight_sum > paraphrased.weight_sum > 0
    assert secondhand.observation_count == 0
    assert secondhand.excluded["EXPERIENCE_SECONDHAND"] == 1


def test_a_marketing_account_does_not_consume_the_author_decay_slots() -> None:
    """Filtering has to happen before the decay or a real user is silenced by an ad."""

    records = [
        _post(f"ad{i}", "brand", marketing=True, text=f"ad {i}") for i in range(3)
    ] + [_post("real", "brand", text="and here is my actual test")]
    aggregate = CommunityAggregator().aggregate(KEY, records)
    assert aggregate.observation_count == 1
    # Rank 1, not rank 4: the three filtered posts never entered the ordering.
    alone = CommunityAggregator().aggregate(
        KEY, [_post("real", "brand", text="and here is my actual test")]
    )
    assert aggregate.weight_sum == alone.weight_sum


def test_stance_is_reported_as_a_score_on_its_own_scale() -> None:
    records = [_post(f"n{i}", f"neg{i}", stance="negative") for i in range(3)]
    records += [_post(f"p{i}", f"pos{i}", stance="positive", text=f"positive {i}") for i in range(1)]
    aggregate = CommunityAggregator().aggregate(KEY, records)
    assert aggregate.stance_score is not None
    assert -1.0 <= aggregate.stance_score < 0.0
    assert KEY.metric_scale_id == "community-stance-net"


def test_an_even_split_is_reported_as_a_conflict_not_averaged_away() -> None:
    records = [_post(f"n{i}", f"neg{i}", stance="negative") for i in range(4)]
    records += [_post(f"p{i}", f"pos{i}", stance="positive", text=f"good {i}") for i in range(4)]
    aggregate = CommunityAggregator().aggregate(KEY, records)
    assert aggregate.has_conflict is True


def test_a_one_sided_key_is_not_a_conflict() -> None:
    records = [_post(f"n{i}", f"neg{i}", stance="negative") for i in range(6)]
    assert CommunityAggregator().aggregate(KEY, records).has_conflict is False


def test_failure_modes_are_counted_per_key() -> None:
    records = [
        _post("a", "u1", failure_modes=("identity_drift", "hand_artifacts")),
        _post("b", "u2", failure_modes=("identity_drift",), text="also drifting"),
    ]
    aggregate = CommunityAggregator().aggregate(KEY, records)
    assert aggregate.failure_modes == {"identity_drift": 2, "hand_artifacts": 1}


def test_engagement_is_recorded_and_never_used_as_weight() -> None:
    quiet = _post("quiet", "u1")
    viral = _post("viral", "u2", text="a very popular post").model_copy(
        update={"engagement": {"upvotes": 40_000}}
    )
    aggregate = CommunityAggregator().aggregate(KEY, [quiet, viral])
    assert aggregate.effective_sample_size == 2.0


def test_spam_tokens_are_detected_from_the_text() -> None:
    assert detect_spam_signals("dm for access, cheapest api around") == ["cheapest api", "dm for access"]
    assert detect_spam_signals("the physics look wrong to me") == []


def test_an_empty_key_reports_zero_rather_than_failing() -> None:
    aggregate = CommunityAggregator().aggregate(KEY, [])
    assert aggregate.effective_sample_size == 0.0
    assert aggregate.stance_score is None


def test_the_aggregate_is_order_independent() -> None:
    records = [_post(f"p{i}", f"user{i % 3}", text=f"post {i}") for i in range(9)]
    forward = CommunityAggregator().aggregate(KEY, records)
    backward = CommunityAggregator().aggregate(KEY, list(reversed(records)))
    assert forward.effective_sample_size == backward.effective_sample_size
    assert forward.stance_weight == backward.stance_weight
