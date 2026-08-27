"""Audit the recorded token prices, or bootstrap them into a scratch database.

**Migration `0051_token_pricing` is the source of truth.** It owns the rows, it
is what a real deployment runs, and it is self-contained so it replays the same
way for ever. This script does not define a single price: it imports the rate
table straight out of that migration, so the two cannot drift.

What it is for:

    uv run python scripts/seed_token_pricing.py --audit    # what does the DB hold?
    uv run python scripts/seed_token_pricing.py --confirm  # bootstrap a scratch DB

`--audit` reads the database and reports, per rate, whether the row `0051` should
have written is actually there, and whether its price still matches. That is the
question worth asking of a production database — a migration that ran is not the
same as a row that survived.

`--confirm` writes any missing rows. It exists for a database built from ORM
metadata rather than the migration chain — the per-test databases and the audit
database used while `0050` was still unmerged — and is **not** how a deployment
gets its prices. If a production database is missing rows, the answer is to find
out why `0051` did not apply, not to paper over it from here.

Opens no socket. Prints no secret.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from platform_database import Database  # noqa: E402
from platform_shared import Settings  # noqa: E402
from production_domain.models import ModelPricingProfile, new_id  # noqa: E402
from sqlalchemy import select  # noqa: E402

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0051_token_pricing_profiles.py"
)


def _migration() -> ModuleType:
    """Load the migration as a module so its rate table is the only definition.

    Alembic revisions are not importable as a package, and copying the table
    here would create a second source of truth — the exact failure this audit
    keeps finding elsewhere.
    """

    spec = importlib.util.spec_from_file_location("_pricing_0051", _MIGRATION)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging invariant.
        raise RuntimeError(f"cannot load the pricing migration at {_MIGRATION}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PRICING = _migration()
RATES = _PRICING.RATES
CHECKED_AT: datetime = _PRICING.CHECKED_AT
USD_PER_CNY = Decimal(str(_PRICING.USD_PER_CNY))
CNY_FX_SOURCE: str = _PRICING.CNY_FX_SOURCE
TOKENS: str = _PRICING.TOKENS
PIXELS: str = _PRICING.PIXELS

#: The migration's own formula builder, re-exported so a test can assert on the
#: expression that will actually be stored rather than a copy of it.
formula_for = _PRICING._formula


def _scope(provider: str, model: str, direction: str, resolution: str) -> str:
    scope = f"{provider}:{model} {direction}"
    return f"{scope} @{resolution}" if resolution else scope


def _audit(database: Database) -> int:
    missing = mismatched = present = 0
    with database.session() as session:
        for provider, model, direction, price, _cur, _unit, resolution, _src, _note in RATES:
            row = session.scalar(
                select(ModelPricingProfile).where(
                    ModelPricingProfile.provider == provider,
                    ModelPricingProfile.provider_model_id == model,
                    ModelPricingProfile.input_mode == direction,
                    ModelPricingProfile.resolution == resolution,
                )
            )
            scope = _scope(provider, model, direction, resolution)
            if row is None:
                print(f"  [MISSING ] {scope:58} 0051 should have written {price}")
                missing += 1
            elif row.unit_price != Decimal(price):
                print(f"  [CHANGED ] {scope:58} holds {row.unit_price}, 0051 wrote {price}")
                mismatched += 1
            else:
                present += 1
    total = len(RATES)
    print(f"\n  {present}/{total} rows present and unchanged, {missing} missing, {mismatched} changed.")
    if missing or mismatched:
        print("\n  A production database should match exactly. Find out why 0051 did not apply.\n")
        return 1
    return 0


def _bootstrap(database: Database, confirm: bool) -> int:
    written = skipped = 0
    with database.session() as session:
        for provider, model, direction, price, currency, unit, resolution, source, note in RATES:
            existing = session.scalar(
                select(ModelPricingProfile).where(
                    ModelPricingProfile.provider == provider,
                    ModelPricingProfile.provider_model_id == model,
                    ModelPricingProfile.input_mode == direction,
                    ModelPricingProfile.resolution == resolution,
                )
            )
            if existing is not None:
                skipped += 1
                continue
            written += 1
            action = "write" if confirm else "would write"
            print(f"  {action:12} {_scope(provider, model, direction, resolution):58} {price} {currency}")
            if not confirm:
                continue
            session.add(
                ModelPricingProfile(
                    id=new_id(),
                    provider=provider,
                    provider_model_id=model,
                    input_mode=direction,
                    resolution=resolution,
                    currency=currency,
                    billing_unit=unit,
                    unit_price=Decimal(price),
                    estimate_unit=unit,
                    estimate_unit_price=Decimal(price),
                    usd_per_currency=Decimal("1") if currency == "USD" else USD_PER_CNY,
                    fx_source="" if currency == "USD" else CNY_FX_SOURCE,
                    fx_checked_at=None if currency == "USD" else CHECKED_AT,
                    estimate_formula=formula_for(unit, direction, "estimate_unit_price"),
                    settlement_formula=formula_for(unit, direction, "unit_price"),
                    effective_from=CHECKED_AT,
                    source_url=source,
                    source_checked_at=CHECKED_AT,
                    notes=note,
                )
            )
    print(f"\n  {written} row(s) {'written' if confirm else 'to write'}, {skipped} already present.")
    if not confirm and written:
        print("\n  Plan only. Re-run with --confirm to write into a scratch database.\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--audit", action="store_true", help="report what the database holds")
    mode.add_argument(
        "--confirm",
        action="store_true",
        help="write missing rows into a scratch database; 0051 owns a real deployment",
    )
    args = parser.parse_args()
    database = Database(Settings().database_url)
    return _audit(database) if args.audit else _bootstrap(database, args.confirm)


if __name__ == "__main__":
    raise SystemExit(main())
