"""Recording where a model stands in the live canary sequence.

`live_canary_status` is this platform's strongest production claim, and until now
nothing wrote it. The column, its four values and its index all existed; a canary
could run a flawless closed loop against a real provider and every model still
read `NOT_RUN`. Migration `0049` describes the vocabulary, and this module is the
only thing that speaks it.

The rule the module exists to enforce is that `VERIFIED_LIVE` is earned by one
specific thing and by nothing else: a **fully closed** loop — the request reached
the provider, the job reached `COMPLETED`, an artifact was registered and is
readable in object storage, and the reservation settled for exactly what it
reserved. Every one of those links, not most of them. A canary that completes but
whose artifact was never fetched is the failure this platform has already had
twice, and it must not read as a pass.

The second rule is that weather is not a verdict. A rate limit, a provider outage
or a network fault says nothing durable about whether this model works, so those
runs record nothing at all rather than overwriting what an earlier run proved. A
provider that rejected the request body, or an account that is not entitled to the
model, *is* durable evidence, and is recorded as such — that is what keeps one
blocked provider from stalling the audit of every model behind it, and what stops
a blocker from being mistaken later for a pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from platform_database import Database
from production_domain.models import ModelDefinition, utcnow
from sqlalchemy import select

NOT_RUN = "NOT_RUN"
VERIFIED_LIVE = "VERIFIED_LIVE"
LIVE_BLOCKED_EXTERNAL = "LIVE_BLOCKED_EXTERNAL"
CONTRACT_INVALID = "CONTRACT_INVALID"

# `live_canary_detail` is String(500).
_DETAIL_LIMIT = 500

# What the provider's own answer says about this model's live path, durably.
# The codes come from `provider_sdk.http`, which maps HTTP status to them.
#
#   401 CREDENTIAL_EXPIRED   this account cannot authenticate for the model
#   403 CONTENT_REJECTED     entitlement or policy refused it outside this repo
#   451 CONTENT_REJECTED     refused for legal reasons, likewise outside it
_BLOCKED_EXTERNAL_CODES = frozenset({"CREDENTIAL_EXPIRED", "CONTENT_REJECTED"})

#   400/404/409/422 INVALID_REQUEST   the provider rejected the body we build
_CONTRACT_INVALID_CODES = frozenset({"INVALID_REQUEST"})


@dataclass(frozen=True)
class CanaryLoop:
    """What one canary run actually observed, in the order the loop closes.

    Every field is an observation, never a conclusion. The verdict is derived
    here so that the rule lives in one place a test can hold still, rather than
    being re-decided by each caller that happens to have run a canary.
    """

    provider: str
    model: str
    job_id: str
    submission_state: str
    terminal_status: str
    output_asset_id: str | None = None
    artifact_bytes: int = 0
    artifact_in_storage: bool = False
    credit_status: str = ""
    credits_reserved: int = 0
    credits_settled: int = 0
    error_code: str | None = None
    provider_task_id: str | None = None

    @property
    def reached_provider(self) -> bool:
        """Whether anything left this process.

        A run refused at the live gate — the failure drill — never opened a
        socket. It proves our own fences work and proves nothing whatever about
        the provider, so it must not move this model's status in either
        direction.
        """

        return self.submission_state != "NOT_SENT"

    @property
    def closed(self) -> bool:
        """Every link in the loop, not most of them."""

        return (
            self.reached_provider
            and self.terminal_status == "COMPLETED"
            and bool(self.output_asset_id)
            and self.artifact_in_storage
            and self.artifact_bytes > 0
            and self.credit_status == "SETTLED"
            and self.credits_reserved > 0
            and self.credits_settled == self.credits_reserved
        )

    def verdict(self) -> str | None:
        """The status this run earns, or `None` when it earns nothing."""

        if self.closed:
            return VERIFIED_LIVE
        if not self.reached_provider:
            return None
        if self.error_code in _BLOCKED_EXTERNAL_CODES:
            return LIVE_BLOCKED_EXTERNAL
        if self.error_code in _CONTRACT_INVALID_CODES:
            return CONTRACT_INVALID
        # Reached the provider and came back neither closed nor durably
        # explained: a completed job whose artifact never landed, a timeout, a
        # rate limit. Real, and worth a human reading the report — but not a
        # verdict about the model.
        return None

    def detail(self) -> str:
        """One line of evidence, sized for `live_canary_detail`."""

        parts = [f"job {self.job_id}"]
        if self.provider_task_id:
            parts.append(f"provider task {self.provider_task_id}")
        if self.closed:
            parts.append(
                f"{self.artifact_bytes} B artifact registered; "
                f"{self.credits_settled} CR settled"
            )
        else:
            parts.append(f"terminal {self.terminal_status or '—'}")
            if self.error_code:
                parts.append(self.error_code)
            if self.credit_status:
                parts.append(f"credits {self.credit_status}")
        return " · ".join(parts)[:_DETAIL_LIMIT]


@dataclass(frozen=True)
class CanaryRecord:
    """What was written, for the caller to print as evidence."""

    logical_name: str
    previous_status: str
    status: str
    detail: str
    observed_at: datetime


def record_canary_outcome(
    database: Database,
    loop: CanaryLoop,
    *,
    observed_at: datetime | None = None,
) -> CanaryRecord | None:
    """Stamp `live_canary_status` for the model a canary just exercised.

    Returns the record written, or `None` when the run earned no verdict or the
    model is not in the registry. `last_live_test_at` moves whenever a request
    actually reached the provider; `last_verified_at` moves only for a closed
    loop, because it is the timestamp that backs the claim.
    """

    verdict = loop.verdict()
    if verdict is None:
        return None

    stamped_at = observed_at or utcnow()
    detail = loop.detail()
    with database.session() as session:
        row = session.scalar(
            select(ModelDefinition).where(
                ModelDefinition.provider == loop.provider,
                ModelDefinition.provider_model_id == loop.model,
            )
        )
        if row is None:
            return None
        previous = row.live_canary_status
        row.live_canary_status = verdict
        row.live_canary_detail = detail
        row.last_live_test_at = stamped_at
        if verdict == VERIFIED_LIVE:
            row.last_verified_at = stamped_at
        return CanaryRecord(
            logical_name=row.logical_name,
            previous_status=previous,
            status=verdict,
            detail=detail,
            observed_at=stamped_at,
        )


# Cost provenances that count as a settled, priced live call for a chat or
# embedding model. ESTIMATED and UNKNOWN are not evidence that money moved for
# a figure anyone can check, so they earn nothing.
_ROLE_SETTLEMENT_SOURCES = frozenset({"VERIFIED_PROVIDER", "TOKENS_LIST"})


def production_serviceable(
    *,
    enabled: bool,
    live_enabled: bool,
    lifecycle_status: str,
) -> bool:
    """Whether ordinary traffic may run this model on the automatic production budget.

    A paying user's credits are the user-side gate; this is the platform's,
    and it is the model's own switches and nothing else: enabled, switched on
    for live traffic, and not DISABLED or BLOCKED. A serviceable model runs
    user requests on their quote-bound spend authorization under the platform
    breaker with no operator-minted permit in the way (operator decision
    2026-09-02: a user who bought credits is settled in credits).

    `live_canary_status` is deliberately not a condition. It is evidence about
    the model — written when a loop closes, reset when the capability contract
    changes — that lifecycle promotion and routing read. It was briefly a gate
    on paying traffic, and with no chat or image model ever verified that gate
    kept the whole platform behind expired permits. The `LiveCanaryPermit`
    remains the fence only where the budget does not reach: the budget
    disabled, or a call the platform cannot price.
    """

    return bool(enabled and live_enabled and lifecycle_status not in {"DISABLED", "BLOCKED"})


def record_role_canary_outcome(
    database: Database,
    *,
    provider: str,
    model: str,
    cost_usd: Decimal,
    cost_source: str,
    evidence_reference: str,
    observed_at: datetime | None = None,
) -> CanaryRecord | None:
    """Stamp `VERIFIED_LIVE` for a chat or embedding model whose permit call settled.

    A media generation closes its loop across several boundaries; a role call
    closes it inside one request. The whole loop here is: the request reached
    the provider, a response came back, and the usage settled at a figure with
    a checkable provenance — the provider's own cost or counted tokens at the
    dated list rate. Anything less (no counts, no rates, an estimate) earns
    nothing, exactly as a generation whose artifact never landed earns nothing.
    """

    if cost_source not in _ROLE_SETTLEMENT_SOURCES:
        return None
    stamped_at = observed_at or utcnow()
    detail = f"role call settled USD {cost_usd} ({cost_source}) · {evidence_reference}"[:_DETAIL_LIMIT]
    with database.session() as session:
        row = session.scalar(
            select(ModelDefinition).where(
                ModelDefinition.provider == provider,
                ModelDefinition.provider_model_id == model,
            )
        )
        if row is None:
            return None
        previous = row.live_canary_status
        row.live_canary_status = VERIFIED_LIVE
        row.live_canary_detail = detail
        row.last_live_test_at = stamped_at
        row.last_verified_at = stamped_at
        return CanaryRecord(
            logical_name=row.logical_name,
            previous_status=previous,
            status=VERIFIED_LIVE,
            detail=detail,
            observed_at=stamped_at,
        )


__all__ = [
    "CONTRACT_INVALID",
    "CanaryLoop",
    "CanaryRecord",
    "LIVE_BLOCKED_EXTERNAL",
    "NOT_RUN",
    "VERIFIED_LIVE",
    "production_serviceable",
    "record_canary_outcome",
    "record_role_canary_outcome",
]
