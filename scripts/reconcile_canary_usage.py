"""Close a live canary usage that is waiting on a human reading a console.

A `LiveCanaryUsage` goes UNCERTAIN the instant a request crosses the provider
boundary, because from inside this process a timeout and a billed generation are
the same event. The permit therefore keeps the whole estimate held in
`reserved_cost_usd` until someone says which it was — and the audit's global
ceiling counts that hold, so an unreconciled refusal spends budget nobody spent.

    uv run python scripts/reconcile_canary_usage.py --list
    uv run python scripts/reconcile_canary_usage.py <usage-id> \
        --not-created --reason "..." --evidence "..." --confirm

This talks to the database through the same audited service the internal route
uses, so the `DecisionRecord` and the idempotency fence are identical. It exists
because the operator needs it before the API image carrying that route is
rebuilt, and because reconciliation is a deliberate act with a written reason
rather than something a deployment should perform on its own.

Prints no secret. Opens no socket to any provider.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from entitlement_core import LiveCanaryPermitService  # noqa: E402
from platform_database import Database  # noqa: E402
from platform_shared import Settings  # noqa: E402
from production_domain.models import LiveCanaryPermit, LiveCanaryUsage  # noqa: E402
from sqlalchemy import select  # noqa: E402


def _database(settings: Settings) -> Database:
    return Database(settings.database_url)


def _list(database: Database) -> int:
    with database.session() as session:
        rows = list(
            session.execute(
                select(LiveCanaryUsage, LiveCanaryPermit)
                .join(LiveCanaryPermit, LiveCanaryPermit.id == LiveCanaryUsage.permit_id)
                .order_by(LiveCanaryUsage.created_at)
            )
        )
    if not rows:
        print("  no live canary usages recorded")
        return 0
    print(f"  {'usage id':38} {'status':10} {'provider':12} {'model':32} held      actual")
    for usage, permit in rows:
        held = Decimal(str(usage.estimated_cost_usd or 0))
        actual = "—" if usage.actual_cost_usd is None else format(usage.actual_cost_usd, "f")
        print(
            f"  {usage.id:38} {usage.status:10} {permit.provider:12} "
            f"{permit.model:32} {format(held, 'f'):9} {actual}"
        )
    waiting = sum(usage.status == "UNCERTAIN" for usage, _permit in rows)
    print(f"\n  {waiting} usage(s) waiting on a finding.\n")
    return 0


def _reconcile(database: Database, args: argparse.Namespace) -> int:
    service = LiveCanaryPermitService(database)
    action = "CONFIRM_PROVIDER_NOT_CREATED" if args.not_created else "SETTLE_ACTUAL_COST"
    if not args.confirm:
        print(
            f"\n  Plan only. This would record {action} against usage {args.usage_id},\n"
            f"  releasing its hold on the permit"
            + (f" and settling USD {args.actual_cost_usd}." if not args.not_created else " at USD 0.")
            + "\n\n  Re-run with --confirm to write it.\n"
        )
        return 0
    reservation, audit_id, replayed = service.reconcile_uncertain(
        args.usage_id,
        action=action,
        actual_cost_usd=None if args.not_created else args.actual_cost_usd,
        idempotency_key=args.idempotency_key,
        reason=args.reason,
        evidence_reference=args.evidence,
    )
    with database.session() as session:
        permit = session.get(LiveCanaryPermit, reservation.permit_id)
        if permit is None:  # pragma: no cover - transaction invariant.
            raise RuntimeError("live canary permit disappeared after reconciliation")
        held = format(permit.reserved_cost_usd, "f")
        spent = format(permit.actual_cost_usd, "f")
        used = f"{permit.used_requests}/{permit.max_requests}"
        status = permit.status
    print(f"\n  usage      {reservation.usage_id} → {reservation.status}")
    print(f"  action     {action}{' (replayed)' if replayed else ''}")
    print(f"  audit      {audit_id}")
    print(f"  permit     {status} · {used} requests · USD {spent} spent · USD {held} still held\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("usage_id", nargs="?", help="the LiveCanaryUsage to close")
    parser.add_argument("--list", action="store_true", help="show every usage and its status")
    finding = parser.add_mutually_exclusive_group()
    finding.add_argument(
        "--not-created",
        action="store_true",
        help="the provider created no job; settles at USD 0",
    )
    finding.add_argument(
        "--actual-cost-usd",
        type=Decimal,
        help="the figure the provider's own billing shows",
    )
    parser.add_argument("--reason", default="", help="why, in one line, for the audit record")
    parser.add_argument("--evidence", default="", help="what was read, and where")
    parser.add_argument(
        "--idempotency-key",
        default="",
        help="defaults to a key derived from the usage id and the finding",
    )
    parser.add_argument("--confirm", action="store_true", help="write it; otherwise plan only")
    args = parser.parse_args()

    # Argument validation comes before anything opens a connection. A malformed
    # invocation should cost a usage message, not a database session and a
    # stack trace about settings it was never going to need.
    listing = args.list or not args.usage_id
    if not listing:
        if not args.not_created and args.actual_cost_usd is None:
            parser.error("choose --not-created or --actual-cost-usd")
        if not args.reason.strip() or not args.evidence.strip():
            parser.error("--reason and --evidence are both required; they are the record")
        if not args.idempotency_key:
            finding_tag = "not-created" if args.not_created else f"actual-{args.actual_cost_usd}"
            args.idempotency_key = f"reconcile:{args.usage_id}:{finding_tag}"

    settings = Settings()
    database = _database(settings)
    return _list(database) if listing else _reconcile(database, args)


if __name__ == "__main__":
    raise SystemExit(main())
