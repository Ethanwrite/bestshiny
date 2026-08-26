"""The operator tool refuses to record a finding that has no record in it.

`reconcile_uncertain` itself is covered in `test_production_evidence_api.py`.
What is tested here is the part that belongs to the command line: that writing is
opt-in, that a finding cannot be filed without a reason and evidence, and that
the default idempotency key distinguishes the two findings rather than letting
one replay as the other.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.reconcile_canary_usage import main


def _argv(monkeypatch, *args: str) -> None:
    monkeypatch.setattr("sys.argv", ["reconcile_canary_usage.py", *args])


def _never_touch_the_database(monkeypatch) -> list[object]:
    """Any call through to the database is a failure of the argument contract."""

    calls: list[object] = []

    def _refuse(*_args: object, **_kwargs: object) -> object:
        calls.append(_args)
        raise AssertionError("argument validation let a call reach the database")

    monkeypatch.setattr("scripts.reconcile_canary_usage._database", _refuse)
    return calls


def test_a_finding_without_a_reason_is_refused(monkeypatch) -> None:
    _never_touch_the_database(monkeypatch)
    _argv(monkeypatch, "usage-1", "--not-created", "--evidence", "console read", "--confirm")
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


def test_a_finding_without_evidence_is_refused(monkeypatch) -> None:
    _never_touch_the_database(monkeypatch)
    _argv(monkeypatch, "usage-1", "--not-created", "--reason", "refused", "--confirm")
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


def test_a_usage_with_no_finding_at_all_is_refused(monkeypatch) -> None:
    _never_touch_the_database(monkeypatch)
    _argv(monkeypatch, "usage-1", "--reason", "r", "--evidence", "e", "--confirm")
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


def test_the_two_findings_cannot_be_given_together(monkeypatch) -> None:
    _never_touch_the_database(monkeypatch)
    _argv(monkeypatch, "usage-1", "--not-created", "--actual-cost-usd", "0.01")
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


def test_without_confirm_it_plans_and_writes_nothing(monkeypatch, capsys) -> None:
    """The default is to say what would happen, exactly as the canary script does."""

    monkeypatch.setattr("scripts.reconcile_canary_usage.Settings", lambda: object())
    monkeypatch.setattr("scripts.reconcile_canary_usage._database", lambda _settings: object())

    def _refuse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a plan-only run reconciled something")

    monkeypatch.setattr(
        "scripts.reconcile_canary_usage.LiveCanaryPermitService",
        lambda _database: type("_S", (), {"reconcile_uncertain": _refuse})(),
    )
    _argv(monkeypatch, "usage-1", "--not-created", "--reason", "r", "--evidence", "e")
    assert main() == 0
    printed = capsys.readouterr().out
    assert "Plan only" in printed
    assert "CONFIRM_PROVIDER_NOT_CREATED" in printed


def test_the_default_key_separates_the_two_findings(monkeypatch) -> None:
    """A key shared by both findings would let one replay as the other."""

    seen: dict[str, object] = {}

    class _Service:
        @staticmethod
        def reconcile_uncertain(usage_id: str, **kwargs: object) -> tuple[object, str, bool]:
            seen.update({"usage_id": usage_id, **kwargs})
            reservation = type(
                "_R", (), {"usage_id": usage_id, "permit_id": "p-1", "status": "SETTLED"}
            )()
            return reservation, "audit-1", False

    class _Permit:
        reserved_cost_usd = Decimal("0")
        actual_cost_usd = Decimal("0")
        used_requests = 1
        max_requests = 1
        status = "EXHAUSTED"

    class _Session:
        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        @staticmethod
        def get(_model: object, _id: str) -> _Permit:
            return _Permit()

    monkeypatch.setattr("scripts.reconcile_canary_usage.Settings", lambda: object())
    monkeypatch.setattr(
        "scripts.reconcile_canary_usage._database",
        lambda _settings: type("_D", (), {"session": lambda self: _Session()})(),
    )
    monkeypatch.setattr(
        "scripts.reconcile_canary_usage.LiveCanaryPermitService", lambda _database: _Service()
    )

    _argv(monkeypatch, "usage-9", "--not-created", "--reason", "r", "--evidence", "e", "--confirm")
    assert main() == 0
    not_created_key = seen["idempotency_key"]

    _argv(
        monkeypatch,
        "usage-9",
        "--actual-cost-usd",
        "0.010000",
        "--reason",
        "r",
        "--evidence",
        "e",
        "--confirm",
    )
    assert main() == 0
    assert seen["idempotency_key"] != not_created_key
    assert seen["action"] == "SETTLE_ACTUAL_COST"
    assert seen["actual_cost_usd"] == Decimal("0.010000")
