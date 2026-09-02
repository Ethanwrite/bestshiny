"""The router's admission policy: who may be picked, versus what is ranked.

Cold start (the operator's phase policy, 2026-09-02): every enabled,
router-enabled model of a configured provider may be routed to unless its
lifecycle is DISABLED or BLOCKED, so evidence decides the *ranking* while
nothing has evidence yet rather than deciding who is eligible for a first
call. Strict: only LIVE/DEGRADED — the live-mode behaviour before this
policy existed, and the one to switch back to once the catalogue has earned
its lifecycle states.

What the policy must never do is relax a gate: the capability, mode, duration,
resolution and reference checks apply identically under both policies, and
the live-canary permit at the gateway is untouched by routing.
"""

from __future__ import annotations

from model_registry_core import ShotRequirements, VideoModelRouter
from platform_shared.config import Settings
from production_domain.models import ModelDefinition, ModelLifecycleStatus
from sqlalchemy import select, update
from video_platform_api.container import router_requires_live_lifecycle


def _set_video_lifecycles(container, default: str, overrides: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """Put every enabled video definition into ``default``, with per-model overrides."""

    with container.database.session() as session:
        session.execute(
            update(ModelDefinition)
            .where(ModelDefinition.modality == "video")
            .values(lifecycle_status=default)
        )
        for provider_model_id, status in overrides.items():
            session.execute(
                update(ModelDefinition)
                .where(ModelDefinition.provider_model_id == provider_model_id)
                .values(lifecycle_status=status)
            )


def _routable_model_ids(container, *, require_live: bool) -> set[str]:  # type: ignore[no-untyped-def]
    """Video models the registry offers the router under the given policy."""

    return {
        profile.model_id
        for profile in container.model_registry.routable(require_live=require_live)
        if profile.modality == "video"
    }


def test_the_policy_predicate_follows_mode_and_setting() -> None:
    assert Settings().router_admission_policy == "cold_start"
    assert router_requires_live_lifecycle(
        Settings(provider_mode="live", router_admission_policy="strict")
    )
    assert not router_requires_live_lifecycle(
        Settings(provider_mode="live", router_admission_policy="cold_start")
    )
    # Outside live mode there is nothing to gate on either way.
    assert not router_requires_live_lifecycle(
        Settings(provider_mode="mock", router_admission_policy="strict")
    )


def test_strict_admits_only_live_or_degraded_models(container) -> None:  # type: ignore[no-untyped-def]
    _set_video_lifecycles(
        container,
        ModelLifecycleStatus.CONFIGURED.value,
        {
            "doubao-seedance-2-5-260628": ModelLifecycleStatus.LIVE.value,
            "kwaivgi/kling-v3.0-pro": ModelLifecycleStatus.DEGRADED.value,
        },
    )

    assert _routable_model_ids(container, require_live=True) == {
        "doubao-seedance-2-5-260628",
        "kwaivgi/kling-v3.0-pro",
    }


def test_cold_start_admits_configured_models_but_never_disabled_or_blocked(container) -> None:  # type: ignore[no-untyped-def]
    _set_video_lifecycles(
        container,
        ModelLifecycleStatus.CONFIGURED.value,
        {
            "kwaivgi/kling-v3.0-std": ModelLifecycleStatus.BLOCKED.value,
            "x-ai/grok-imagine-video": ModelLifecycleStatus.DISABLED.value,
            "google/veo-3.1-lite": ModelLifecycleStatus.TESTING.value,
        },
    )
    with container.database.session() as session:
        enabled_video = set(
            session.scalars(
                select(ModelDefinition.provider_model_id).where(
                    ModelDefinition.modality == "video",
                    ModelDefinition.enabled.is_(True),
                    ModelDefinition.router_enabled.is_(True),
                )
            )
        )

    routable = _routable_model_ids(container, require_live=False)

    assert "kwaivgi/kling-v3.0-std" not in routable
    assert "x-ai/grok-imagine-video" not in routable
    assert "google/veo-3.1-lite" in routable  # TESTING is a first-call candidate
    assert routable == enabled_video - {"kwaivgi/kling-v3.0-std", "x-ai/grok-imagine-video"}
    # And strict would have admitted nothing from this catalogue at all.
    assert _routable_model_ids(container, require_live=True) == set()


def test_cold_start_relaxes_no_capability_gate(container) -> None:  # type: ignore[no-untyped-def]
    """Broad admission means a CONFIGURED model can be *considered*; every hard
    gate still decides whether it can carry *this* request."""

    _set_video_lifecycles(container, ModelLifecycleStatus.CONFIGURED.value, {})
    router = VideoModelRouter(
        container.model_registry,
        require_live_lifecycle=False,
        scene_champions=container.video_router.scene_champions,
    )

    # A last frame is a capability: Grok declares no end frame and is refused
    # even though cold start admitted it to the candidate pool.
    framed = router.rank(ShotRequirements(requires_start_frame=True, requires_end_frame=True, duration=8))
    rejected = {f"{item.provider}:{item.model}": item for item in framed.rejected}
    assert "CAPABILITY_REQUIRED" in rejected["openrouter:x-ai/grok-imagine-video"].reason_codes
    assert framed.recommended == "wan-2.7"

    # A mode fact: references plus an end frame resolve to Wan's R2V, which
    # carries no last frame, so the first_last_frame primary is vetoed.
    mixed = router.rank(
        ShotRequirements(
            requires_reference_images=True,
            requires_end_frame=True,
            reference_image_count=2,
            duration=8,
        )
    )
    rejected = {f"{item.provider}:{item.model}": item for item in mixed.rejected}
    assert "MODE_ROLE_UNSUPPORTED" in rejected["wan:wan-2.7"].reason_codes
    assert mixed.recommended == "kwaivgi/kling-v3.0-pro"

    # A duration bound: 20 seconds is beyond every 15s envelope, and only the
    # 30s route survives — no champion, so open scoring decides.
    long = router.rank(ShotRequirements(duration=20, resolution="720p"))
    rejected = {f"{item.provider}:{item.model}": item for item in long.rejected}
    assert "DURATION_UNSUPPORTED" in rejected["seedance:doubao-seedance-2-5-260628"].reason_codes
    assert long.recommended == "alibaba/wan-3.0"

    # A resolution bound: 4K is declared by exactly one model.
    ultra = router.rank(ShotRequirements(duration=8, resolution="4K"))
    assert {candidate.model for candidate in ultra.candidates} == {"google/veo-3.1"}
