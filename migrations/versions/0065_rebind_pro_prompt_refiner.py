"""Point the paid-tier prompt refiner at a provider that is switched on.

`PROMPT_REFINER_LOW_COST` for `plan_tier = ALL` — every tier above FREE — was
bound to `runapi / gpt-5.6-luna`. Production runs with
`ALLOW_RUNAPI_EDGE_CALLS=false`, and the RunAPI adapter refuses every edge call
while that flag is off:

    ProviderError: live RunAPI call denied; ALLOW_RUNAPI_EDGE_CALLS=true is required

So the primary refiner could not answer at all. `/v1/prompts/refine` degraded to
`local_safe_fallback` and returned the corrector's own output, which reads as a
prompt with one sentence appended and no model rewriting — observed on the PRO
workspace on 2026-08-30, while FREE was unaffected because its binding is
Seedance.

Worse than useless: the reservation is taken before the adapter's policy check,
so each blocked attempt consumed a live-canary request and left an `UNCERTAIN`
usage holding budget that never settles.

`openrouter / openai/gpt-5.6-sol` is already the bound `PROMPT_REFINER_FALLBACK`
for the same scope, is `CONFIGURED`, and was verified end to end from the
production container — a real completion settled at $0.00016. Binding the
primary to it makes the paid tiers refine through a path that works, without
changing the RunAPI policy flag. FREE keeps Seedance and is untouched.

The update is guarded on the binding still pointing at the RunAPI model, so an
administrator's later change is left alone rather than overwritten, and it is a
no-op on a database where the catalogue has not been seeded yet. The frozen
catalogue default in `config/model-registry/defaults.json` is changed in the
same commit, so a newly seeded database does not reintroduce the RunAPI binding.

Revision ID: 0065_rebind_pro_prompt_refiner
Revises: 0064_free_tier_defaults
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0065_rebind_pro_prompt_refiner"
down_revision: str | None = "0064_free_tier_defaults"
branch_labels = None
depends_on = None

ROLE = "PROMPT_REFINER_LOW_COST"
PLAN_TIER = "ALL"
RUNAPI = ("runapi", "gpt-5.6-luna")
OPENROUTER = ("openrouter", "openai/gpt-5.6-sol")

_REBIND = """
    update model_role_bindings
       set model_definition_id = (
               select id from model_definitions
                where provider = :to_provider and provider_model_id = :to_model
           ),
           updated_at = :now
     where role = :role
       and plan_tier = :plan_tier
       and model_definition_id = (
               select id from model_definitions
                where provider = :from_provider and provider_model_id = :from_model
           )
       and exists (
               select 1 from model_definitions
                where provider = :to_provider and provider_model_id = :to_model
           )
"""


REQUIRED_TABLES = frozenset({"model_role_bindings", "model_definitions"})


def _rebind(source: tuple[str, str], target: tuple[str, str]) -> None:
    # Partial schemas reach this migration — the history tests build one
    # deliberately — so absent tables are nothing to rebind rather than an
    # error.
    if not REQUIRED_TABLES <= set(sa.inspect(op.get_bind()).get_table_names()):
        return
    # `now()` is PostgreSQL-only and the SQLite half of the matrix runs the same
    # migrations, so the timestamp is bound rather than left to the engine.
    op.execute(
        sa.text(_REBIND).bindparams(
            now=datetime.now(UTC),
            role=ROLE,
            plan_tier=PLAN_TIER,
            from_provider=source[0],
            from_model=source[1],
            to_provider=target[0],
            to_model=target[1],
        )
    )


def upgrade() -> None:
    _rebind(RUNAPI, OPENROUTER)


def downgrade() -> None:
    _rebind(OPENROUTER, RUNAPI)
