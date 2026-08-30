"""What "canonical pricing" is allowed to mean, as three rules with teeth.

The operator set them out on 2026-08-29, together with an audited price sheet:

1. A promotion is never canonical. Not folded into the base rate, and not
   end-dated beside it either — end-dating was already how this table modelled
   Ark's 1080p discount, and it was still the row quoting money on the day.
2. The vendor's ORIGINAL list price wins over whatever is charged today. A
   reseller's rate and a limited-time launch rate are both discounts, and a
   discount under-quotes the instant it lapses. Quoting the higher figure can
   only ever be generous.
3. A price stays in the currency its vendor publishes it in. `usd_per_currency`
   is an FX *snapshot* with its own source and date; USD is derived from it when
   a quote or a settlement is taken. Converting the CNY rows in place would
   erase the difference between a price published in dollars and a price
   translated into dollars on one particular day — and that difference is
   exactly what someone needs when the rate moves.

These are pinned against a database built by the real migration chain, because
the rules are about what a deployment holds, not about what a helper returns.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]

ARK_MODEL = "doubao-seedance-2-5-260628"
USD_PER_CNY = 0.14743

#: Every model in the registry that bills in its vendor's own non-USD currency.
CNY_MODELS = {
    "doubao-seedance-2-5-260628",
    "doubao-seedream-5-0-260128",
    "doubao-seed-2-0-lite-260428",
    "glm-5.2",
    "qwen3.8-max",
    "wan2.7-t2v-2026-06-12",
    "wan2.7-i2v-2026-04-25",
    "wan2.7-r2v-2026-06-12",
}

#: Canonical models with no published per-call price anywhere. Not a gap to be
#: filled with a plausible number: Google sells Flow as subscription credits and
#: publishes no per-call rate, and DashScope's Wan 3.0 is invitation-only and
#: unreachable from this account.
UNPRICEABLE = {
    ("google_flow", "flow-veo-3.1"),
    ("google_flow", "NARWHAL"),
    ("wan", "wan3.0-video"),
}


@pytest.fixture(scope="module")
def migrated(tmp_path_factory) -> list[dict]:  # type: ignore[no-untyped-def]
    """The pricing table a deployment holds after the full migration chain."""

    database_path = tmp_path_factory.mktemp("canonical") / "pricing.db"
    # `migrations/env.py` reads DATABASE_URL, not the alembic.ini URL, and this
    # fixture is module-scoped so `monkeypatch` is unavailable here.
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{database_path}"
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous

    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "select provider, provider_model_id, input_mode, resolution, currency, "
                "billing_unit, unit_price, estimate_unit, estimate_unit_price, "
                "usd_per_currency, fx_source, fx_checked_at, effective_from, "
                "effective_until, source_url, notes from model_pricing_profiles"
            )
        ).mappings().all()
    engine.dispose()
    return [dict(row) for row in rows]


def test_no_row_anywhere_carries_an_end_date(migrated) -> None:
    """Rule 1, at its strongest: a dated row IS the promotional shape.

    0044 dated Ark's 1080p discount rather than folding it into the base price,
    which was the careful thing to do and still left the discount as the row a
    quote resolved to — the engine prefers the narrower dated rate while it is in
    force, by design. Deleting it is what actually stops it quoting.

    Asserted across the whole table rather than on the one known row, so the next
    promotion someone seeds fails here instead of in a month's billing.
    """

    dated = [row for row in migrated if row["effective_until"] is not None]
    assert dated == [], f"promotional rows are not canonical pricing: {dated}"


def test_the_ark_1080p_list_rate_is_what_quotes_once_the_promotion_is_gone(migrated) -> None:
    """Nothing had to be written in its place: 77.00 was already seeded beside it."""

    ark_1080p = [
        row
        for row in migrated
        if row["provider_model_id"] == ARK_MODEL
        and row["resolution"] == "1080p"
        and row["input_mode"] == "no_video_input"
    ]
    assert len(ark_1080p) == 1
    assert float(ark_1080p[0]["unit_price"]) == pytest.approx(77.0)
    assert float(ark_1080p[0]["estimate_unit_price"]) == pytest.approx(3.742)
    # The promotional figures, which must not survive anywhere in this scope.
    assert float(ark_1080p[0]["unit_price"]) != pytest.approx(55.44)
    assert float(ark_1080p[0]["estimate_unit_price"]) != pytest.approx(2.69424)


def test_a_resold_rate_never_becomes_the_canonical_price(migrated) -> None:
    """Rule 2. OpenRouter's 2.00/10.00 for Sol is a reseller discount.

    OpenAI's own page shows 4.00/20.00 today, which is itself promotional against
    the 5.00/30.00 original launch list. All three numbers are real; only one of
    them is safe to quote, because it is the only one that cannot fall short when
    a promotion ends.
    """

    sol = {
        row["input_mode"]: row
        for row in migrated
        if row["provider_model_id"] == "openai/gpt-5.6-sol"
    }
    assert float(sol["input_tokens"]["unit_price"]) == pytest.approx(5.00)
    assert float(sol["output_tokens"]["unit_price"]) == pytest.approx(30.00)
    # The two discounted readings, neither of which may be the stored price.
    for discounted in (2.00, 4.00):
        assert float(sol["input_tokens"]["unit_price"]) != pytest.approx(discounted)
    for discounted in (10.00, 20.00):
        assert float(sol["output_tokens"]["unit_price"]) != pytest.approx(discounted)
    # And the row says why, so the next person to read 5.00 against a 2.00
    # invoice does not "correct" it back.
    assert "ORIGINAL" in sol["input_tokens"]["notes"]


def test_wan_3_0_is_quoted_at_openrouter_list_not_the_endpoint_discount(migrated) -> None:
    """The same rule on the route that actually bills today.

    OpenRouter's Alibaba endpoint carries a 15% discount, making the charged
    figures 0.0425 / 0.085 / 0.17. The list SKUs are what is stored.
    """

    wan30 = {
        row["resolution"]: float(row["unit_price"])
        for row in migrated
        if row["provider_model_id"] == "alibaba/wan-3.0"
    }
    assert wan30 == {
        "480p": pytest.approx(0.05),
        "720p": pytest.approx(0.10),
        "1080p": pytest.approx(0.20),
    }


def test_a_cny_price_stays_a_cny_price_with_its_fx_snapshot_beside_it(migrated) -> None:
    """Rule 3. Converting in place would destroy the distinction that matters.

    A row reading 0.08845800 USD tells you nothing about whether Alibaba
    publishes dollars or whether somebody translated 0.60 CNY on a Tuesday. The
    canonical figure is the vendor's own; the snapshot is how USD is derived
    from it, and it carries a source and a date so it can be seen to go stale.
    """

    cny = [row for row in migrated if row["provider_model_id"] in CNY_MODELS]
    assert cny, "the CNY-billed models must still be present"
    for row in cny:
        assert row["currency"] == "CNY", row["provider_model_id"]
        assert float(row["usd_per_currency"]) == pytest.approx(USD_PER_CNY)
        assert row["fx_source"], row["provider_model_id"]
        assert "2026-08-26" in row["fx_source"]
        assert row["fx_checked_at"] is not None, row["provider_model_id"]

    # A USD row is the other half of the same rule: no conversion happened, so
    # its snapshot is exactly 1 and it names no parity. (Two spellings of "no
    # conversion" are already in the table — 0047 writes a sentence, 0051 an
    # empty string — so the assertion is on the fact, not on the wording.)
    usd = [row for row in migrated if row["currency"] == "USD"]
    assert usd
    for row in usd:
        assert float(row["usd_per_currency"]) == pytest.approx(1.0)
        assert "parity" not in row["fx_source"].lower(), row["provider_model_id"]


def test_every_row_still_carries_the_source_that_makes_it_a_price(migrated) -> None:
    """A figure without a first-party URL is a number someone remembered."""

    for row in migrated:
        assert row["source_url"].startswith("https://"), row["provider_model_id"]
        assert float(row["unit_price"]) > 0, row["provider_model_id"]
        assert float(row["estimate_unit_price"]) > 0, row["provider_model_id"]


def test_a_model_with_no_published_rate_is_left_unpriced_rather_than_guessed(migrated) -> None:
    """The honest state, and the one that fails closed.

    Google Flow is sold as credits inside a subscription and publishes no
    per-call rate; DashScope's Wan 3.0 is invitation-only and this account has no
    access. Seeding a plausible number for any of the three would make them
    quotable, and a quotable model with an invented price loses money silently —
    which is the failure the whole pricing table was built to end.
    """

    priced = {(row["provider"], row["provider_model_id"]) for row in migrated}
    for scope in UNPRICEABLE:
        assert scope not in priced, f"{scope} has no published price and must stay unpriced"
