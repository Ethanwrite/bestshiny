"""Every model in the registry must agree with itself, four ways.

For each of the twenty-five models there are four independent facts, and this
audit found each of them wrong somewhere:

    registry ID        the string sent to the provider — `grok-video`,
                       `veo-3.1-quality`, `wan-3.0` and `seedance-2.5` were all
                       names no provider publishes
    pricing profile    a row in `model_pricing_profiles` for that exact string
    pricing_status     the report shown to an operator
    live quote         what `estimate()` actually does when money is at stake

They are computed from different places, so nothing forced them to agree, and
they did not: Wan reported VERIFIED and raised `PricingUnverified` at the till,
Doubao reported UNVERIFIED for a model that was priced.

This asserts the alignment on a container built from the shipped defaults, so a
model added without a price, or priced under a string the registry does not
hold, fails here rather than at a provider's invoice.

The fixture runs the migration chain rather than building the schema from ORM
metadata, because the prices live in migrations 0044-0051. On a metadata-built
database the price table is empty and every assertion below passes vacuously —
which is how the Wan defect survived this long.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cost_core import CreditPricingEngine, PricingUnverified
from model_registry_core import ModelCapabilityRegistry
from platform_shared import Settings
from production_domain.models import ModelDefinition, ModelPricingProfile
from sqlalchemy import select
from video_platform_api.container import build_container

EXPECTED_MODEL_COUNT = 25

# The five that carry no price, and why. Each is a deliberate refusal, not a gap
# waiting to be filled with a guess.
UNPRICEABLE = {
    "flow-veo-3.1-internal": "Google publishes no third-party Flow API and no per-call price",
    "flow-narwhal-image-internal": "NARWHAL is not a Google-published identifier",
    "wan-3.0-official": (
        "the DashScope route: this account has no Wan 3.0 access there, so no rate is "
        "confirmed for it. Kept as a distinct record because it is a different provider "
        "and a different model id from the OpenRouter route, not a duplicate of it."
    ),
}

# Retired: each held a (provider, provider_model_id, modality) that an OpenRouter
# record already owned, behind a provider whose every method raised
# PROVIDER_NOT_CONFIGURED. `model_definitions` is UNIQUE on that triple, so they
# could never have been repointed at OpenRouter — they were second names for
# models already in the registry.
RETIRED_LOGICAL_NAMES = {"grok-video-official", "veo-3.1-quality-official"}

# Strings that were once in the registry and are not model IDs at any provider.
RETIRED_IDS = {"grok-video", "veo-3.1-quality", "wan-3.0", "seedance-2.5", "seedream-5-0"}


@pytest.fixture(scope="module")
def aligned(tmp_path_factory):  # type: ignore[no-untyped-def]
    """A fresh deployment: an empty database taken through the whole chain.

    Not `create_all_and_stamp()`. The prices live in migrations 0044-0051, and a
    database built from ORM metadata has none of them — every assertion below
    would pass vacuously on an empty price table, which is exactly how the Wan
    defect survived. Running the real chain is also the only way this test means
    what its name says: that a fresh install is internally consistent.

    `deployment_environment` is "development" so the container does not stamp
    over the schema the migrations just built.
    """

    import os

    from alembic import command
    from alembic.config import Config

    root = tmp_path_factory.mktemp("aligned")
    url = f"sqlite:///{root / 'platform.db'}"

    # `migrations/env.py` builds its own `Settings()` and takes `database_url`
    # from it, so setting `sqlalchemy.url` on the Config alone is ignored. The
    # environment is what Settings reads.
    repo = Path(__file__).resolve().parents[1]
    config = Config(str(repo / "alembic.ini"))
    config.set_main_option("script_location", str(repo / "migrations"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous

    settings = Settings(
        _env_file=None,
        database_url=url,
        storage_root=root / "media",
        public_base_url="http://testserver",
        auth_required=False,
        platform_api_key="test-platform-key",
        deployment_environment="development",
        # Declared the way a real deployment declares them, so the registry rows
        # hold the IDs a deployment actually sends.
        ark_api_key="test-ark-key",
        doubao_model_id="doubao-seed-2-0-lite-260428",
        wan_api_key="test-wan-key",
        wan2_7_t2v_model_id="wan2.7-t2v-2026-06-12",
        runapi_api_key="test-runapi-key",
        runapi_base_url="https://runapi.ai",
        runapi_model_id="gpt-5.6-luna",
        deepseek_api_key="test-deepseek-key",
        deepseek_model_id="deepseek-v4-flash",
    )
    built = build_container(settings)
    try:
        yield built
    finally:
        built.database.engine.dispose()


def _definitions(container) -> list[ModelDefinition]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        return list(session.scalars(select(ModelDefinition)).all())


def _has_profile(container, definition: ModelDefinition) -> bool:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        return (
            session.scalar(
                select(ModelPricingProfile).where(
                    ModelPricingProfile.provider == definition.provider,
                    ModelPricingProfile.provider_model_id == definition.provider_model_id,
                )
            )
            is not None
        )


def _quote_succeeds(container, definition: ModelDefinition) -> bool | None:  # type: ignore[no-untyped-def]
    """None for a model the credit engine does not quote (chat and embeddings)."""

    if definition.modality not in {"image", "video"}:
        return None
    engine = CreditPricingEngine(
        ModelCapabilityRegistry(database=container.database),
        database=container.database,
        require_verified_pricing=True,
    )
    request: dict[str, object] = {
        "provider": definition.provider,
        "model": definition.provider_model_id,
        "media_type": "video" if definition.modality == "video" else "image",
        "resolution": "720p",
    }
    if definition.modality == "video":
        request["duration"] = 4.0
    try:
        engine.estimate(**request)  # type: ignore[arg-type]
    except PricingUnverified:
        return False
    return True


def test_the_registry_holds_every_model_this_audit_covered(aligned) -> None:  # type: ignore[no-untyped-def]
    assert len(_definitions(aligned)) == EXPECTED_MODEL_COUNT


def test_no_model_is_a_second_name_for_one_already_in_the_registry(aligned) -> None:  # type: ignore[no-untyped-def]
    """`model_definitions` is UNIQUE on (provider, provider_model_id, modality).

    Two records used to violate the spirit of that while satisfying the letter,
    by naming a fictional provider: `grok-video-official` and
    `veo-3.1-quality-official` described the same two models OpenRouter already
    served, behind stubs where every call raised PROVIDER_NOT_CONFIGURED.
    """

    definitions = _definitions(aligned)
    triples = [(d.provider, d.provider_model_id, d.modality) for d in definitions]
    assert len(triples) == len(set(triples))
    names = {d.logical_name for d in definitions}
    assert not (names & RETIRED_LOGICAL_NAMES)


def test_no_model_sends_a_string_its_provider_does_not_publish(aligned) -> None:  # type: ignore[no-untyped-def]
    """A logical name must never reach a provider as an API model ID — §20."""

    offenders = [
        (definition.logical_name, definition.provider_model_id)
        for definition in _definitions(aligned)
        if definition.provider_model_id in RETIRED_IDS
    ]
    assert not offenders


def test_pricing_status_agrees_with_whether_a_profile_exists(aligned) -> None:  # type: ignore[no-untyped-def]
    """The report is derived from the price table; it must not describe a stale row."""

    disagreements = [
        (definition.logical_name, definition.provider_model_id, definition.pricing_status)
        for definition in _definitions(aligned)
        if (definition.pricing_status == "VERIFIED") != _has_profile(aligned, definition)
    ]
    assert not disagreements


def test_pricing_status_agrees_with_what_the_till_does(aligned) -> None:  # type: ignore[no-untyped-def]
    """Wan reported VERIFIED and raised PricingUnverified. Never again."""

    disagreements = [
        (definition.logical_name, definition.pricing_status, quote)
        for definition in _definitions(aligned)
        for quote in [_quote_succeeds(aligned, definition)]
        if quote is not None and (definition.pricing_status == "VERIFIED") != quote
    ]
    assert not disagreements


def test_exactly_the_unpriceable_models_are_refused(aligned) -> None:  # type: ignore[no-untyped-def]
    """Neither more nor fewer: a shrinking list means someone guessed a price.

    The chain has run, so this is the real set: three refusals, each deliberate.
    """

    unverified = {
        definition.logical_name
        for definition in _definitions(aligned)
        if definition.pricing_status == "UNVERIFIED"
    }
    assert unverified == set(UNPRICEABLE)


def test_every_priced_model_is_priced_under_the_string_it_actually_sends(aligned) -> None:  # type: ignore[no-untyped-def]
    """The Wan defect in general form: a price keyed on a string nobody sends."""

    for definition in _definitions(aligned):
        if definition.pricing_status != "VERIFIED":
            continue
        assert _has_profile(aligned, definition), (
            f"{definition.logical_name} is VERIFIED but no profile is keyed on "
            f"{definition.provider}:{definition.provider_model_id}"
        )
