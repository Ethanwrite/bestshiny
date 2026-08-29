"""The audit-wide spend ceiling, which used to count nothing at all.

`scripts/live_canary.py` mints one permit per model under a shared USD ceiling.
Two things were wrong with the total it computed, and they pulled in opposite
directions, so neither showed up as an obviously wrong number:

1. It read `body["items"]`. The endpoint answers `{"limit": n, "permits": [...]}`,
   and `dict.get` with a default turned that into an empty list — the ceiling
   counted zero and would have authorised any spend at all.
2. The rule it *meant* to apply charged every permit its full authorisation for
   ever, including permits that were exhausted having billed nothing. Three
   refused attempts had already committed USD 8.05 of a USD 10 budget without a
   cent reaching a provider.

These are offline arithmetic tests. Nothing here opens a socket.
"""

from __future__ import annotations

from decimal import Decimal

from scripts.live_canary import (
    GLOBAL_CANARY_COST_CEILING_USD,
    TERMINAL,
    Report,
    _permit_exposure,
    _report_failure_path,
)


def _permit(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "max_cost_usd": "1.000000",
        "reserved_cost_usd": "0.000000",
        "actual_cost_usd": "0.000000",
        "status": "ACTIVE",
    }
    base.update(overrides)
    return base


def test_an_active_permit_is_exposed_for_everything_it_may_still_draw() -> None:
    assert _permit_exposure(_permit()) == Decimal("1.000000")


def test_an_active_permit_is_never_worth_less_than_it_has_already_taken() -> None:
    """A ceiling raised after the fact must not shrink the exposure below reality."""

    overdrawn = _permit(max_cost_usd="0.100000", actual_cost_usd="0.400000")
    assert _permit_exposure(overdrawn) == Decimal("0.400000")


def test_an_exhausted_permit_that_billed_nothing_frees_its_whole_ceiling() -> None:
    """The case that stalled the audit: attempted, refused, reconciled at zero."""

    refused = _permit(status="EXHAUSTED", max_cost_usd="5.000000")
    assert _permit_exposure(refused) == Decimal("0")


def test_an_exhausted_permit_keeps_holding_an_unreconciled_usage() -> None:
    """UNCERTAIN is not evidence of zero, so the estimate stays counted."""

    unreconciled = _permit(
        status="EXHAUSTED",
        max_cost_usd="5.000000",
        reserved_cost_usd="0.890000",
    )
    assert _permit_exposure(unreconciled) == Decimal("0.890000")


def test_an_exhausted_permit_is_worth_what_it_actually_spent() -> None:
    spent = _permit(status="EXHAUSTED", max_cost_usd="1.200000", actual_cost_usd="0.891600")
    assert _permit_exposure(spent) == Decimal("0.891600")


def test_an_expired_permit_cannot_draw_again_either() -> None:
    expired = _permit(status="EXPIRED", max_cost_usd="3.000000")
    assert _permit_exposure(expired) == Decimal("0")


def test_the_three_pre_audit_permits_do_not_consume_the_budget_once_reconciled() -> None:
    """The real state this audit starts from, as the ceiling now reads it.

    All three attempts were refused before the provider created anything, and
    each was reconciled at USD 0. Under the old rule they held USD 8.05 of a
    USD 10 ceiling; the whole phase-two queue costs about USD 4.40.
    """

    history = [
        _permit(status="EXHAUSTED", max_cost_usd="3.000000"),
        _permit(status="EXHAUSTED", max_cost_usd="5.000000"),
        _permit(status="EXHAUSTED", max_cost_usd="0.050000"),
    ]
    committed = sum((_permit_exposure(item) for item in history), Decimal("0"))
    assert committed == Decimal("0")
    assert GLOBAL_CANARY_COST_CEILING_USD - committed == Decimal("10")


def test_operator_action_is_terminal_for_the_unattended_canary() -> None:
    assert "WORKER_NEEDS_USER_ACTION" in TERMINAL


def test_a_mapped_billed_canary_failure_is_not_reported_as_verified(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "scripts.live_canary._credit_state",
        lambda _engine, _job_id: {
            "credits": 1,
            "status": "RECONCILIATION_REQUIRED",
            "settled_credits": 0,
            "refunded_credits": 0,
            "balance_after": 99,
            "reconciliation_reason": "INVALID_REQUEST",
        },
    )
    monkeypatch.setattr("scripts.live_canary._report_events", lambda *_args: None)
    report = Report()

    result = _report_failure_path(
        object(),
        {
            "status": "WORKER_NEEDS_USER_ACTION",
            "error_code": "INVALID_REQUEST",
            "error_message": "provider rejected the request",
            "submission_state": "SENT_UNCONFIRMED",
            "safe_to_retry": False,
        },
        "job-1",
        report,
        drill=False,
    )

    assert result == 1
    assert report.failures == 1
