"""Series-level continuity: what is true, who may know it, what is still owed."""

from __future__ import annotations

import pytest
from narrative_ledger_core import AUDIENCE, KnowledgeViolation, NarrativeLedgerService


@pytest.fixture
def ledger(container):  # type: ignore[no-untyped-def]
    return NarrativeLedgerService(container.database)


def test_a_fact_defaults_to_audience_only_knowledge(ledger, project) -> None:  # type: ignore[no-untyped-def]
    """Establishing a fact tells the viewer, not the cast."""

    ledger.establish_fact(
        project.id,
        fact_key="lin_is_adopted",
        summary="Lin was adopted; the family hid it.",
        episode=7,
    )
    assert ledger.may_know(project.id, holder_key=AUDIENCE, fact_key="lin_is_adopted", episode=7)
    assert not ledger.may_know(project.id, holder_key="lin", fact_key="lin_is_adopted", episode=7)


def test_knowledge_is_not_available_before_it_is_disclosed(ledger, project) -> None:  # type: ignore[no-untyped-def]
    ledger.establish_fact(
        project.id,
        fact_key="lin_is_adopted",
        summary="Lin was adopted.",
        episode=7,
    )
    ledger.disclose(project.id, fact_key="lin_is_adopted", holder_key="lin", episode=31)

    assert not ledger.may_know(project.id, holder_key="lin", fact_key="lin_is_adopted", episode=30)
    assert ledger.may_know(project.id, holder_key="lin", fact_key="lin_is_adopted", episode=31)
    assert ledger.may_know(project.id, holder_key="lin", fact_key="lin_is_adopted", episode=60)


def test_acting_on_undisclosed_knowledge_fails_closed(ledger, project) -> None:  # type: ignore[no-untyped-def]
    """The classic 60-episode break, now representable and therefore catchable."""

    ledger.establish_fact(
        project.id,
        fact_key="lin_is_adopted",
        summary="Lin was adopted.",
        episode=7,
    )
    ledger.disclose(project.id, fact_key="lin_is_adopted", holder_key="lin", episode=31)

    with pytest.raises(KnowledgeViolation, match="never disclosed"):
        ledger.assert_may_act_on(
            project.id,
            holder_key="lin",
            fact_keys=["lin_is_adopted"],
            episode=20,
        )
    ledger.assert_may_act_on(
        project.id,
        holder_key="lin",
        fact_keys=["lin_is_adopted"],
        episode=40,
    )


def test_audience_knowledge_alone_never_authorises_a_character(ledger, project) -> None:  # type: ignore[no-untyped-def]
    ledger.establish_fact(
        project.id,
        fact_key="the_letter_was_forged",
        summary="The letter was forged.",
        episode=3,
    )
    with pytest.raises(KnowledgeViolation):
        ledger.assert_may_act_on(
            project.id,
            holder_key="mira",
            fact_keys=["the_letter_was_forged"],
            episode=59,
        )


def test_a_fact_cannot_be_learned_before_it_exists(ledger, project) -> None:  # type: ignore[no-untyped-def]
    ledger.establish_fact(project.id, fact_key="f", summary="s", episode=10)
    with pytest.raises(ValueError, match="cannot learn"):
        ledger.disclose(project.id, fact_key="f", holder_key="lin", episode=9)


def test_open_obligations_survive_until_settled(ledger, project) -> None:  # type: ignore[no-untyped-def]
    """An obligation is owed, not similar - retrieval would never surface it."""

    ledger.open_obligation(
        project.id,
        obligation_key="who_sent_the_letter",
        promise="The sender of the letter must be revealed.",
        episode=7,
    )
    still_open = ledger.series_context(project.id, episode=59)
    assert "The sender of the letter must be revealed." in still_open.open_obligations

    ledger.settle_obligation(
        project.id,
        obligation_key="who_sent_the_letter",
        episode=60,
        reason="revealed in the finale",
    )
    settled = ledger.series_context(project.id, episode=60)
    assert settled.open_obligations == []


def test_series_context_separates_audience_only_facts(ledger, project) -> None:  # type: ignore[no-untyped-def]
    """The audience/character gap is the irony, and it must stay visible."""

    ledger.establish_fact(project.id, fact_key="secret", summary="Mira is the informant.", episode=5)
    ledger.establish_fact(project.id, fact_key="shared", summary="The shop burned down.", episode=6)
    ledger.disclose(project.id, fact_key="shared", holder_key="lin", episode=6)

    context = ledger.series_context(project.id, episode=40, holder_keys=["lin"])
    assert context.audience_only_facts == ["Mira is the informant."]
    assert context.known_facts["lin"] == ["The shop burned down."]


def test_series_context_renders_compiler_continuity_facts(ledger, project) -> None:  # type: ignore[no-untyped-def]
    ledger.establish_fact(project.id, fact_key="shared", summary="The shop burned down.", episode=6)
    ledger.disclose(project.id, fact_key="shared", holder_key="lin", episode=6)
    ledger.open_obligation(
        project.id,
        obligation_key="who_burned_it",
        promise="Who burned the shop must be answered.",
        episode=6,
    )
    facts = ledger.series_context(project.id, episode=12, holder_keys=["lin"]).continuity_facts()
    names = {item["name"] for item in facts}
    assert names == {"known_fact", "open_obligation"}
    assert any(item.get("holder") == "lin" for item in facts)


def test_context_cost_does_not_grow_with_episode_number(ledger, project) -> None:  # type: ignore[no-untyped-def]
    """Heads, not history: episode 60 reads the same shape as episode 2."""

    for episode in range(1, 31):
        ledger.establish_fact(
            project.id,
            fact_key=f"fact-{episode}",
            summary=f"Something happened in episode {episode}.",
            episode=episode,
            disclose_to=[AUDIENCE, "lin"],
        )
    early = ledger.series_context(project.id, episode=2, holder_keys=["lin"])
    late = ledger.series_context(project.id, episode=30, holder_keys=["lin"])
    # Later episodes see more facts, but through one bounded query, not a replay.
    assert len(early.known_facts["lin"]) == 2
    assert len(late.known_facts["lin"]) == 30
    assert set(early.known_facts) == set(late.known_facts)
