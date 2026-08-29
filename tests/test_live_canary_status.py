"""The writer that decides whether a model may claim VERIFIED_LIVE.

Until this existed, `live_canary_status` had a column, four documented values, an
index and no writer: a canary could close the loop against a real provider and
every model still read `NOT_RUN`. These tests hold the two rules that make the
status worth reading — a pass is earned by the whole loop, and weather is not a
verdict.
"""

import pytest
from model_registry_core import (
    CONTRACT_INVALID,
    LIVE_BLOCKED_EXTERNAL,
    VERIFIED_LIVE,
    CanaryLoop,
    record_canary_outcome,
)
from production_domain.models import ModelDefinition

PROVIDER = "openrouter"
PROVIDER_MODEL_ID = "vendor/canary-probe"


@pytest.fixture
def model(container):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        session.add(
            ModelDefinition(
                logical_name="canary-probe-openrouter",
                provider=PROVIDER,
                provider_model_id=PROVIDER_MODEL_ID,
                modality="video",
                capabilities=["video_generation"],
            )
        )
    return PROVIDER_MODEL_ID


def _status(container, provider_model_id=PROVIDER_MODEL_ID):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        row = session.query(ModelDefinition).filter_by(provider_model_id=provider_model_id).one()
        return row.live_canary_status, row.live_canary_detail, row.last_verified_at, row.last_live_test_at


def _closed_loop(**overrides):  # type: ignore[no-untyped-def]
    fields = dict(
        provider=PROVIDER,
        model=PROVIDER_MODEL_ID,
        job_id="job-1",
        submission_state="CONFIRMED",
        terminal_status="COMPLETED",
        output_asset_id="asset-1",
        artifact_bytes=413652,
        artifact_in_storage=True,
        credit_status="SETTLED",
        credits_reserved=44,
        credits_settled=44,
        provider_task_id="fb7cf016-479d-4816-a066-8894525466d8",
    )
    fields.update(overrides)
    return CanaryLoop(**fields)


def test_a_fully_closed_loop_earns_verified_live(container, model):  # type: ignore[no-untyped-def]
    record = record_canary_outcome(container.database, _closed_loop())

    assert record is not None
    assert record.previous_status == "NOT_RUN"
    assert record.status == VERIFIED_LIVE
    status, detail, verified_at, tested_at = _status(container)
    assert status == VERIFIED_LIVE
    # The provider's own task id is the part an auditor can take back to the
    # vendor's console; a status with no such handle is unfalsifiable.
    assert "fb7cf016-479d-4816-a066-8894525466d8" in detail
    assert verified_at is not None
    assert tested_at is not None


@pytest.mark.parametrize(
    ("broken", "why"),
    [
        ({"terminal_status": "FAILED"}, "the job never completed"),
        ({"output_asset_id": None}, "completed with no output asset"),
        ({"artifact_in_storage": False}, "the artifact was never fetched into the bucket"),
        ({"artifact_bytes": 0}, "a zero-byte artifact is not an artifact"),
        ({"credit_status": "RECONCILIATION_REQUIRED"}, "billing did not settle"),
        ({"credits_settled": 20}, "settled for less than it reserved"),
        ({"submission_state": "NOT_SENT"}, "nothing reached the provider"),
    ],
)
def test_one_broken_link_is_never_verified_live(container, model, broken, why):  # type: ignore[no-untyped-def]
    loop = _closed_loop(**broken)

    assert loop.closed is False, why
    assert loop.verdict() != VERIFIED_LIVE, why
    assert _status(container)[0] == "NOT_RUN"


def test_the_failure_drill_records_nothing_at_all(container, model):  # type: ignore[no-untyped-def]
    # The drill is refused at our own live gate before a socket opens. It proves
    # our fences work and says nothing whatever about the provider, so it must
    # not move the status in either direction.
    record = record_canary_outcome(
        container.database,
        _closed_loop(
            submission_state="NOT_SENT",
            terminal_status="FAILED",
            error_code="LIVE_CANARY_DENIED",
            artifact_in_storage=False,
            output_asset_id=None,
            credit_status="REFUNDED",
        ),
    )

    assert record is None
    assert _status(container)[0] == "NOT_RUN"


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        ("CREDENTIAL_EXPIRED", LIVE_BLOCKED_EXTERNAL),
        ("CONTENT_REJECTED", LIVE_BLOCKED_EXTERNAL),
        ("INVALID_REQUEST", CONTRACT_INVALID),
    ],
)
def test_a_durable_provider_answer_is_recorded_as_itself(  # type: ignore[no-untyped-def]
    container, model, error_code, expected
):
    record = record_canary_outcome(
        container.database,
        _closed_loop(
            terminal_status="FAILED",
            output_asset_id=None,
            artifact_in_storage=False,
            artifact_bytes=0,
            credit_status="REFUNDED",
            credits_settled=0,
            error_code=error_code,
        ),
    )

    assert record is not None
    assert record.status == expected
    status, detail, verified_at, tested_at = _status(container)
    assert status == expected
    assert error_code in detail
    # A blocker is not a pass: `last_verified_at` is the timestamp that backs
    # the claim, and only a closed loop may move it.
    assert verified_at is None
    assert tested_at is not None


@pytest.mark.parametrize("error_code", ["RATE_LIMIT", "PROVIDER_BUSY", "PROVIDER_NETWORK_ERROR"])
def test_weather_is_not_a_verdict_and_cannot_erase_a_pass(container, model, error_code):  # type: ignore[no-untyped-def]
    record_canary_outcome(container.database, _closed_loop())
    assert _status(container)[0] == VERIFIED_LIVE

    later = record_canary_outcome(
        container.database,
        _closed_loop(
            job_id="job-2",
            terminal_status="FAILED",
            output_asset_id=None,
            artifact_in_storage=False,
            credit_status="REFUNDED",
            error_code=error_code,
        ),
    )

    assert later is None
    assert _status(container)[0] == VERIFIED_LIVE


def test_a_later_contract_rejection_does_overwrite_a_pass(container, model):  # type: ignore[no-untyped-def]
    # The opposite of the case above. A provider that now rejects the body we
    # build is real evidence about today, and the stale pass must not survive it.
    record_canary_outcome(container.database, _closed_loop())

    record = record_canary_outcome(
        container.database,
        _closed_loop(
            job_id="job-3",
            terminal_status="FAILED",
            output_asset_id=None,
            artifact_in_storage=False,
            credit_status="REFUNDED",
            error_code="INVALID_REQUEST",
        ),
    )

    assert record is not None
    assert record.previous_status == VERIFIED_LIVE
    assert _status(container)[0] == CONTRACT_INVALID


def test_an_unregistered_model_is_not_invented(container):  # type: ignore[no-untyped-def]
    assert record_canary_outcome(container.database, _closed_loop(model="vendor/not-here")) is None
