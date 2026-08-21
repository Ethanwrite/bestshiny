from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from platform_database import Database
from production_domain.models import (
    AccountStatus,
    BrowserWorker,
    Character,
    CharacterIdentityVersion,
    FlowMigrationPlan,
    FlowMigrationStatus,
    FlowMigrationVerificationStatus,
    MediaAsset,
    MediaProviderBinding,
    Project,
    ProviderAccount,
    ProviderCharacterBinding,
    ProviderCredential,
    ProviderInstructionBinding,
    ProviderProjectBinding,
    ProviderProjectBindingStatus,
    utcnow,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .scheduler import AccountScheduler, NoAccountAvailable

FLOW_PROVIDER = "google_flow"
ACTIVE_BINDING_STATUSES = frozenset(
    {
        ProviderProjectBindingStatus.PROVISIONING.value,
        ProviderProjectBindingStatus.READY.value,
        ProviderProjectBindingStatus.DEGRADED.value,
        ProviderProjectBindingStatus.MIGRATION_REQUIRED.value,
        ProviderProjectBindingStatus.MIGRATING.value,
    }
)
ACTIVE_MIGRATION_STATUSES = frozenset(
    {
        FlowMigrationStatus.PLANNED.value,
        FlowMigrationStatus.USER_REVIEW_REQUIRED.value,
        FlowMigrationStatus.APPROVED.value,
        FlowMigrationStatus.MIGRATING.value,
    }
)
MIGRATION_TRIGGERS = frozenset(
    {
        "ACCOUNT_DISABLED",
        "ACCOUNT_PERMANENTLY_UNHEALTHY",
        "CREDITS_EXHAUSTED",
        "SESSION_UNRECOVERABLE",
    }
)


class FlowProjectProvisioningError(RuntimeError):
    """A provisioning implementation failed before or after a possible remote side effect."""

    def __init__(self, message: str, *, remote_side_effect_possible: bool):
        super().__init__(message)
        self.remote_side_effect_possible = remote_side_effect_possible


class FlowProjectProvisioner(Protocol):
    async def create_project(
        self,
        *,
        local_project_id: str,
        provider_account_id: str,
        worker_id: str,
        idempotency_key: str,
    ) -> str: ...


class OfflineFlowProjectProvisioner:
    """Production-safe default until a reviewed Flow create-project transport is installed."""

    async def create_project(
        self,
        *,
        local_project_id: str,
        provider_account_id: str,
        worker_id: str,
        idempotency_key: str,
    ) -> str:
        del local_project_id, provider_account_id, worker_id, idempotency_key
        raise FlowProjectProvisioningError(
            "automatic Flow project provisioning is not configured",
            remote_side_effect_possible=False,
        )


class FlowAffinityUnavailable(NoAccountAvailable):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class FlowAffinityConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class FlowAffinityLease:
    binding_id: str
    local_project_id: str
    provider_account: ProviderAccount
    worker: BrowserWorker
    provider_project_id: str


class FlowProjectAllocator:
    """Create or reuse one sticky Flow account/project affinity per local project.

    A PROVISIONING row is committed before the provisioner is entered. It is the
    concurrency and crash fence: another worker either observes READY or fails
    closed; it never creates a second remote project or silently selects another
    account.
    """

    def __init__(
        self,
        database: Database,
        scheduler: AccountScheduler,
        provisioner: FlowProjectProvisioner | None = None,
        *,
        provisioning_lease_seconds: int = 300,
    ):
        self.database = database
        self.scheduler = scheduler
        self.provisioner = provisioner or OfflineFlowProjectProvisioner()
        self.provisioning_lease_seconds = max(30, provisioning_lease_seconds)

    @staticmethod
    def _active_binding_statement(local_project_id: str):  # type: ignore[no-untyped-def]
        return select(ProviderProjectBinding).where(
            ProviderProjectBinding.local_project_id == local_project_id,
            ProviderProjectBinding.provider == FLOW_PROVIDER,
            ProviderProjectBinding.status.in_(ACTIVE_BINDING_STATUSES),
        )

    @staticmethod
    def _remote_owner_statement(provider_project_id: str):  # type: ignore[no-untyped-def]
        """Return the permanent owner row, including inactive historical bindings."""

        return select(ProviderProjectBinding).where(
            ProviderProjectBinding.provider == FLOW_PROVIDER,
            ProviderProjectBinding.provider_project_id == provider_project_id,
        )

    def binding_for_project(self, local_project_id: str) -> ProviderProjectBinding | None:
        with self.database.session() as session:
            active = session.scalar(self._active_binding_statement(local_project_id))
            if active is not None:
                return active
            # An inactive historical affinity is still materially different
            # from a project that has never used Flow. Do not turn DISABLED or
            # FAILED into an implicit re-provision/migration.
            return session.scalar(
                select(ProviderProjectBinding)
                .where(
                    ProviderProjectBinding.local_project_id == local_project_id,
                    ProviderProjectBinding.provider == FLOW_PROVIDER,
                )
                .order_by(ProviderProjectBinding.updated_at.desc())
            )

    def bind_existing(
        self,
        *,
        local_project_id: str,
        provider_account_id: str,
        provider_project_id: str,
    ) -> ProviderProjectBinding:
        remote_id = provider_project_id.strip()
        if not remote_id:
            raise ValueError("provider_project_id is required")
        with self.database.session() as session:
            project = session.get(Project, local_project_id)
            account = session.get(ProviderAccount, provider_account_id)
            if project is None or account is None:
                raise LookupError("project or provider account not found")
            if account.provider != FLOW_PROVIDER:
                raise FlowAffinityConflict("provider account is not a Google Flow account")
            remote_owner = session.scalar(self._remote_owner_statement(remote_id).with_for_update())
            if remote_owner is not None and remote_owner.local_project_id != local_project_id:
                raise FlowAffinityConflict("Flow project is permanently owned by another local project")
            active = session.scalar(self._active_binding_statement(local_project_id).with_for_update())
            if active is not None:
                if (
                    active.status == ProviderProjectBindingStatus.READY.value
                    and active.provider_account_id == account.id
                    and active.provider_project_id == remote_id
                ):
                    return active
                raise FlowAffinityConflict("local project already has an active Flow affinity")
            if remote_owner is not None:
                # A reviewed migration may bind this local project to a new
                # remote project, but it must not create a second ownership row
                # for an inactive historical remote identity.
                raise FlowAffinityConflict(
                    "Flow project is already reserved by an inactive historical binding "
                    "for this local project"
                )
            binding = ProviderProjectBinding(
                local_project_id=local_project_id,
                provider=FLOW_PROVIDER,
                provider_account_id=account.id,
                provider_project_id=remote_id,
                status=ProviderProjectBindingStatus.READY.value,
                ready_at=utcnow(),
            )
            try:
                with session.begin_nested():
                    session.add(binding)
                    session.flush()
            except IntegrityError as exc:
                winner = session.scalar(self._active_binding_statement(local_project_id))
                if (
                    winner is not None
                    and winner.status == ProviderProjectBindingStatus.READY.value
                    and winner.provider_account_id == account.id
                    and winner.provider_project_id == remote_id
                ):
                    return winner
                remote_winner = session.scalar(self._remote_owner_statement(remote_id))
                if remote_winner is not None:
                    raise FlowAffinityConflict(
                        "Flow project is permanently owned by another binding"
                    ) from exc
                raise FlowAffinityConflict(
                    "Flow project binding conflicts with another concurrent operation"
                ) from exc
            return binding

    async def acquire_for_generation(
        self,
        *,
        local_project_id: str,
        capability: str,
        model: str,
        priority: int,
        generation_job_id: str,
        claim_token: str,
    ) -> FlowAffinityLease:
        binding = self.binding_for_project(local_project_id)
        if binding is not None:
            return self._acquire_ready_binding(
                binding,
                capability=capability,
                model=model,
                priority=priority,
                generation_job_id=generation_job_id,
                claim_token=claim_token,
            )
        return await self._provision_affinity(
            local_project_id=local_project_id,
            capability=capability,
            model=model,
            priority=priority,
            generation_job_id=generation_job_id,
            claim_token=claim_token,
        )

    def _acquire_ready_binding(
        self,
        binding: ProviderProjectBinding,
        *,
        capability: str,
        model: str,
        priority: int,
        generation_job_id: str,
        claim_token: str,
    ) -> FlowAffinityLease:
        if binding.status == ProviderProjectBindingStatus.PROVISIONING.value:
            expiry = binding.provisioning_expires_at
            normalized_expiry = (
                expiry.replace(tzinfo=UTC) if expiry is not None and expiry.tzinfo is None else expiry
            )
            if normalized_expiry is not None and normalized_expiry <= datetime.now(UTC):
                self.mark_migration_required(binding.id, trigger_reason="SESSION_UNRECOVERABLE")
                raise FlowAffinityUnavailable(
                    "FLOW_MIGRATION_REQUIRED",
                    "Flow project provisioning lease expired with an unknown remote outcome",
                )
            raise FlowAffinityUnavailable(
                "FLOW_AFFINITY_PROVISIONING",
                "Flow project affinity is being provisioned by another worker",
                retryable=True,
            )
        if binding.status == ProviderProjectBindingStatus.MIGRATION_REQUIRED.value:
            raise FlowAffinityUnavailable(
                "FLOW_MIGRATION_REQUIRED",
                "Flow project affinity requires an explicit reviewed migration plan",
            )
        if binding.status == ProviderProjectBindingStatus.MIGRATING.value:
            raise FlowAffinityUnavailable(
                "FLOW_MIGRATION_IN_PROGRESS",
                "Flow project affinity migration is still in progress",
                retryable=True,
            )
        if binding.status == ProviderProjectBindingStatus.DEGRADED.value:
            raise FlowAffinityUnavailable(
                "FLOW_AFFINITY_DEGRADED",
                "Flow project affinity is degraded and cannot fail over automatically",
            )
        if binding.status == ProviderProjectBindingStatus.DISABLED.value:
            raise FlowAffinityUnavailable(
                "FLOW_AFFINITY_DISABLED",
                "Flow project affinity is disabled; an explicit reviewed binding is required",
            )
        if binding.status == ProviderProjectBindingStatus.FAILED.value:
            raise FlowAffinityUnavailable(
                "FLOW_AFFINITY_FAILED",
                "Flow project affinity provisioning failed; an explicit reviewed binding is required",
            )
        if binding.status != ProviderProjectBindingStatus.READY.value or not binding.provider_project_id:
            raise FlowAffinityUnavailable(
                "FLOW_AFFINITY_NOT_READY",
                f"Flow project affinity is not ready: {binding.status}",
            )
        try:
            account, worker = self.scheduler.select_account(
                FLOW_PROVIDER,
                capability,
                model,
                priority,
                project_id=binding.local_project_id,
                generation_job_id=generation_job_id,
                claim_token=claim_token,
            )
        except NoAccountAvailable as exc:
            trigger = self._permanent_migration_trigger(binding.provider_account_id)
            if trigger is not None:
                self.mark_migration_required(binding.id, trigger_reason=trigger)
                raise FlowAffinityUnavailable(
                    "FLOW_MIGRATION_REQUIRED",
                    "sticky Flow account cannot continue; explicit migration review is required",
                ) from exc
            raise FlowAffinityUnavailable(
                "FLOW_STICKY_ACCOUNT_UNAVAILABLE",
                "sticky Flow account is temporarily unavailable; failover is forbidden",
                retryable=True,
            ) from exc
        if account.id != binding.provider_account_id:
            self.scheduler.release_job(
                generation_job_id,
                success=False,
                error="scheduler violated Flow sticky affinity",
                clear_routing=True,
            )
            raise FlowAffinityConflict("scheduler selected an account outside the Flow binding")
        return FlowAffinityLease(
            binding_id=binding.id,
            local_project_id=binding.local_project_id,
            provider_account=account,
            worker=worker,
            provider_project_id=binding.provider_project_id,
        )

    async def _provision_affinity(
        self,
        *,
        local_project_id: str,
        capability: str,
        model: str,
        priority: int,
        generation_job_id: str,
        claim_token: str,
    ) -> FlowAffinityLease:
        try:
            account, worker = self.scheduler.select_account(
                FLOW_PROVIDER,
                capability,
                model,
                priority,
                project_id=None,
                generation_job_id=generation_job_id,
                claim_token=claim_token,
            )
        except NoAccountAvailable as exc:
            raise FlowAffinityUnavailable(
                "FLOW_PROVISIONING_ACCOUNT_UNAVAILABLE",
                "no healthy Google Flow account is available for project provisioning",
                retryable=True,
            ) from exc
        try:
            binding, claimed = self._claim_provisioning(
                local_project_id=local_project_id,
                provider_account_id=account.id,
            )
        except BaseException:
            # Account/worker capacity was reserved in a preceding transaction.
            # Any cancellation or database failure before the durable binding
            # claim must release that reservation exactly once.
            self._release_unbound_job(
                generation_job_id,
                "Flow affinity provisioning claim failed",
            )
            raise
        if not claimed:
            self._release_unbound_job(generation_job_id, "another worker won Flow affinity")
            return self._acquire_ready_binding(
                binding,
                capability=capability,
                model=model,
                priority=priority,
                generation_job_id=generation_job_id,
                claim_token=claim_token,
            )
        assert binding.provisioning_token is not None
        try:
            remote_project_id = (
                await self.provisioner.create_project(
                    local_project_id=local_project_id,
                    provider_account_id=account.id,
                    worker_id=worker.id,
                    idempotency_key=binding.id,
                )
            ).strip()
            if not remote_project_id:
                raise FlowProjectProvisioningError(
                    "Flow provisioner returned an empty remote project id",
                    remote_side_effect_possible=True,
                )
        except FlowProjectProvisioningError as exc:
            self._finish_failed_provisioning(
                binding.id,
                binding.provisioning_token,
                reason=str(exc),
                remote_side_effect_possible=exc.remote_side_effect_possible,
            )
            self._release_unbound_job(generation_job_id, str(exc))
            raise FlowAffinityUnavailable(
                "FLOW_PROVISIONING_REVIEW_REQUIRED"
                if exc.remote_side_effect_possible
                else "FLOW_PROVISIONING_UNAVAILABLE",
                str(exc),
            ) from exc
        except BaseException:
            self._finish_failed_provisioning(
                binding.id,
                binding.provisioning_token,
                reason="Flow provisioning interrupted after its durable claim",
                remote_side_effect_possible=True,
            )
            self._release_unbound_job(generation_job_id, "Flow provisioning interrupted")
            raise
        try:
            ready = self._finish_ready_provisioning(
                binding.id,
                binding.provisioning_token,
                provider_project_id=remote_project_id,
            )
        except (FlowAffinityConflict, IntegrityError) as exc:
            self._finish_failed_provisioning(
                binding.id,
                binding.provisioning_token,
                reason="remote Flow project ownership conflict",
                remote_side_effect_possible=True,
                observed_remote_project_id=remote_project_id,
            )
            self._release_unbound_job(generation_job_id, str(exc))
            raise FlowAffinityUnavailable(
                "FLOW_REMOTE_PROJECT_OWNERSHIP_CONFLICT",
                "provisioned Flow project is already owned; explicit review is required",
            ) from exc
        return FlowAffinityLease(
            binding_id=ready.id,
            local_project_id=ready.local_project_id,
            provider_account=account,
            worker=worker,
            provider_project_id=remote_project_id,
        )

    def _claim_provisioning(
        self,
        *,
        local_project_id: str,
        provider_account_id: str,
    ) -> tuple[ProviderProjectBinding, bool]:
        token = str(uuid.uuid4())
        now = utcnow()
        with self.database.session() as session:
            if session.get(Project, local_project_id) is None:
                raise LookupError("local project not found")
            existing = session.scalar(
                select(ProviderProjectBinding)
                .where(
                    ProviderProjectBinding.local_project_id == local_project_id,
                    ProviderProjectBinding.provider == FLOW_PROVIDER,
                )
                .order_by(
                    ProviderProjectBinding.status.in_(ACTIVE_BINDING_STATUSES).desc(),
                    ProviderProjectBinding.updated_at.desc(),
                )
                .with_for_update()
            )
            if existing is not None:
                # An inactive historical binding also fences automatic
                # provisioning. Only bind_existing/a reviewed migration may
                # deliberately establish a replacement affinity.
                return existing, False
            candidate = ProviderProjectBinding(
                local_project_id=local_project_id,
                provider=FLOW_PROVIDER,
                provider_account_id=provider_account_id,
                provider_project_id=None,
                status=ProviderProjectBindingStatus.PROVISIONING.value,
                status_reason="AUTOMATIC_PROVISIONING",
                provisioning_token=token,
                provisioning_expires_at=now + timedelta(seconds=self.provisioning_lease_seconds),
            )
            try:
                with session.begin_nested():
                    session.add(candidate)
                    session.flush()
            except IntegrityError:
                winner = session.scalar(self._active_binding_statement(local_project_id))
                if winner is None:
                    raise
                return winner, False
            return candidate, True

    def _finish_ready_provisioning(
        self,
        binding_id: str,
        token: str,
        *,
        provider_project_id: str,
    ) -> ProviderProjectBinding:
        with self.database.session() as session:
            owner = session.scalar(
                self._remote_owner_statement(provider_project_id)
                .where(ProviderProjectBinding.id != binding_id)
                .with_for_update()
            )
            if owner is not None:
                raise FlowAffinityConflict("Flow project is permanently owned by another binding")
            result = session.execute(
                update(ProviderProjectBinding)
                .where(
                    ProviderProjectBinding.id == binding_id,
                    ProviderProjectBinding.status == ProviderProjectBindingStatus.PROVISIONING.value,
                    ProviderProjectBinding.provisioning_token == token,
                )
                .values(
                    provider_project_id=provider_project_id,
                    status=ProviderProjectBindingStatus.READY.value,
                    status_reason="AUTOMATIC_PROVISIONING_COMPLETED",
                    ready_at=utcnow(),
                    provisioning_token=None,
                    provisioning_expires_at=None,
                    version=ProviderProjectBinding.version + 1,
                )
            )
            if int(getattr(result, "rowcount", 0)) != 1:
                raise FlowAffinityConflict("Flow provisioning claim was superseded")
            binding = session.get(ProviderProjectBinding, binding_id)
            if binding is None:  # pragma: no cover - protected by the update.
                raise LookupError("Flow project binding disappeared")
            session.flush()
            return binding

    def _finish_failed_provisioning(
        self,
        binding_id: str,
        token: str,
        *,
        reason: str,
        remote_side_effect_possible: bool,
        observed_remote_project_id: str | None = None,
    ) -> None:
        with self.database.session() as session:
            binding = session.scalar(
                select(ProviderProjectBinding)
                .where(
                    ProviderProjectBinding.id == binding_id,
                    ProviderProjectBinding.status == ProviderProjectBindingStatus.PROVISIONING.value,
                    ProviderProjectBinding.provisioning_token == token,
                )
                .with_for_update()
            )
            if binding is None:
                return
            binding.status = (
                ProviderProjectBindingStatus.MIGRATION_REQUIRED.value
                if remote_side_effect_possible
                else ProviderProjectBindingStatus.FAILED.value
            )
            binding.status_reason = reason[:240]
            binding.provisioning_token = None
            binding.provisioning_expires_at = None
            binding.version += 1
            if remote_side_effect_possible:
                binding.migration_required_at = utcnow()
                self._ensure_migration_plan_in_session(
                    session,
                    binding,
                    trigger_reason="SESSION_UNRECOVERABLE",
                    source_project_id=observed_remote_project_id,
                )

    def _permanent_migration_trigger(self, account_id: str) -> str | None:
        with self.database.session() as session:
            account = session.get(ProviderAccount, account_id)
            if account is None or account.status == AccountStatus.DISABLED.value:
                return "ACCOUNT_DISABLED"
            if account.status == AccountStatus.EXPIRED.value:
                return "SESSION_UNRECOVERABLE"
            if bool((account.metadata_json or {}).get("permanently_unhealthy")):
                return "ACCOUNT_PERMANENTLY_UNHEALTHY"
            if account.credits <= 0:
                return "CREDITS_EXHAUSTED"
            credential = (
                session.get(ProviderCredential, account.credential_id) if account.credential_id else None
            )
            expires_at = credential.expires_at if credential is not None else None
            normalized_expiry = (
                expires_at.replace(tzinfo=UTC)
                if expires_at is not None and expires_at.tzinfo is None
                else expires_at
            )
            if normalized_expiry is not None and normalized_expiry <= datetime.now(UTC):
                return "SESSION_UNRECOVERABLE"
            return None

    def mark_migration_required(
        self,
        binding_id: str,
        *,
        trigger_reason: str,
    ) -> FlowMigrationPlan:
        if trigger_reason not in MIGRATION_TRIGGERS:
            raise ValueError("unsupported automatic Flow migration trigger")
        with self.database.session() as session:
            binding = session.scalar(
                select(ProviderProjectBinding)
                .where(
                    ProviderProjectBinding.id == binding_id,
                    ProviderProjectBinding.provider == FLOW_PROVIDER,
                )
                .with_for_update()
            )
            if binding is None:
                raise LookupError("Flow project binding not found")
            if binding.status in {
                ProviderProjectBindingStatus.DISABLED.value,
                ProviderProjectBindingStatus.FAILED.value,
            }:
                raise FlowAffinityConflict(f"cannot migrate an inactive Flow binding: {binding.status}")
            if binding.status != ProviderProjectBindingStatus.MIGRATION_REQUIRED.value:
                binding.status = ProviderProjectBindingStatus.MIGRATION_REQUIRED.value
                binding.status_reason = trigger_reason
                binding.migration_required_at = binding.migration_required_at or utcnow()
                binding.provisioning_token = None
                binding.provisioning_expires_at = None
                binding.version += 1
            return self._ensure_migration_plan_in_session(
                session,
                binding,
                trigger_reason=trigger_reason,
            )

    def _ensure_migration_plan_in_session(
        self,
        session: Session,
        binding: ProviderProjectBinding,
        *,
        trigger_reason: str,
        source_project_id: str | None = None,
    ) -> FlowMigrationPlan:
        existing = session.scalar(
            select(FlowMigrationPlan).where(
                FlowMigrationPlan.source_binding_id == binding.id,
                FlowMigrationPlan.migration_status.in_(ACTIVE_MIGRATION_STATUSES),
            )
        )
        if existing is not None:
            return existing
        characters, instructions, assets = self._migration_inventory(
            session,
            local_project_id=binding.local_project_id,
            account_id=binding.provider_account_id,
        )
        plan = FlowMigrationPlan(
            source_binding_id=binding.id,
            local_project_id=binding.local_project_id,
            source_account_id=binding.provider_account_id,
            target_account_id=None,
            source_project_id=source_project_id or binding.provider_project_id,
            target_project_id=None,
            characters_json=characters,
            instructions_json=instructions,
            assets_json=assets,
            migration_status=FlowMigrationStatus.USER_REVIEW_REQUIRED.value,
            verification_status=FlowMigrationVerificationStatus.USER_REVIEW_REQUIRED.value,
            trigger_reason=trigger_reason,
        )
        try:
            with session.begin_nested():
                session.add(plan)
                session.flush()
            return plan
        except IntegrityError:
            winner = session.scalar(
                select(FlowMigrationPlan).where(
                    FlowMigrationPlan.source_binding_id == binding.id,
                    FlowMigrationPlan.migration_status.in_(ACTIVE_MIGRATION_STATUSES),
                )
            )
            if winner is None:
                raise
            return winner

    @staticmethod
    def _migration_inventory(
        session: Session,
        *,
        local_project_id: str,
        account_id: str,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
        character_rows = session.execute(
            select(ProviderCharacterBinding, CharacterIdentityVersion, Character)
            .join(
                CharacterIdentityVersion,
                CharacterIdentityVersion.id == ProviderCharacterBinding.character_identity_version_id,
            )
            .join(Character, Character.id == CharacterIdentityVersion.character_id)
            .where(
                Character.project_id == local_project_id,
                ProviderCharacterBinding.provider == FLOW_PROVIDER,
                ProviderCharacterBinding.provider_account_id == account_id,
            )
            .order_by(ProviderCharacterBinding.id)
        ).all()
        characters: list[dict[str, object]] = [
            {
                "binding_id": binding.id,
                "character_id": character.id,
                "identity_version_id": identity.id,
                "binding": binding.binding_json,
            }
            for binding, identity, character in character_rows
        ]
        instruction_rows = session.scalars(
            select(ProviderInstructionBinding)
            .where(
                ProviderInstructionBinding.project_id == local_project_id,
                ProviderInstructionBinding.provider == FLOW_PROVIDER,
                ProviderInstructionBinding.provider_account_id == account_id,
            )
            .order_by(ProviderInstructionBinding.id)
        )
        instructions: list[dict[str, object]] = [
            {
                "binding_id": binding.id,
                "name": binding.instruction_name,
                "provider_instruction_id": binding.provider_instruction_id,
            }
            for binding in instruction_rows
        ]
        asset_rows = session.execute(
            select(MediaProviderBinding, MediaAsset)
            .join(MediaAsset, MediaAsset.id == MediaProviderBinding.asset_id)
            .where(
                MediaAsset.project_id == local_project_id,
                MediaProviderBinding.provider == FLOW_PROVIDER,
                MediaProviderBinding.account_id == account_id,
            )
            .order_by(MediaProviderBinding.id)
        ).all()
        assets: list[dict[str, object]] = [
            {
                "binding_id": binding.id,
                "asset_id": asset.id,
                "provider_media_id": binding.provider_media_id,
                "status": binding.status,
            }
            for binding, asset in asset_rows
        ]
        return characters, instructions, assets

    def _release_unbound_job(self, generation_job_id: str, error: str) -> None:
        self.scheduler.release_job(
            generation_job_id,
            success=None,
            error=error[:4000],
            clear_routing=True,
        )


__all__ = [
    "FlowAffinityConflict",
    "FlowAffinityLease",
    "FlowAffinityUnavailable",
    "FlowProjectAllocator",
    "FlowProjectProvisioner",
    "FlowProjectProvisioningError",
    "OfflineFlowProjectProvisioner",
]
