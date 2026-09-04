from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DDL,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    false,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class AssetType(StrEnum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    REFERENCE = "REFERENCE"
    CHARACTER_REFERENCE = "CHARACTER_REFERENCE"
    LOCATION_REFERENCE = "LOCATION_REFERENCE"
    PROP_REFERENCE = "PROP_REFERENCE"
    START_FRAME = "START_FRAME"
    END_FRAME = "END_FRAME"
    GENERATED_FRAME = "GENERATED_FRAME"
    CHARACTER_MASTER = "CHARACTER_MASTER"
    LOCATION_MASTER = "LOCATION_MASTER"
    PROP_MASTER = "PROP_MASTER"
    KEYFRAME = "KEYFRAME"


class AssetKind(StrEnum):
    """Logical production assets shared by manual and automated workflows."""

    CHARACTER = "CHARACTER"
    SCENE = "SCENE"
    PRODUCT = "PRODUCT"
    PROP = "PROP"
    WARDROBE = "WARDROBE"
    VEHICLE = "VEHICLE"
    CREATURE = "CREATURE"
    VOICE = "VOICE"
    STYLE = "STYLE"
    REFERENCE = "REFERENCE"


class AssetVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    REJECTED = "REJECTED"


class JobStatus(StrEnum):
    NEW = "NEW"
    RESERVED = "RESERVED"
    QUEUED = "QUEUED"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    WORKER_NEEDS_USER_ACTION = "WORKER_NEEDS_USER_ACTION"


class AccountStatus(StrEnum):
    READY = "READY"
    BUSY = "BUSY"
    COOLDOWN = "COOLDOWN"
    DISABLED = "DISABLED"
    EXPIRED = "EXPIRED"


class WorkerStatus(StrEnum):
    READY = "READY"
    BUSY = "BUSY"
    OFFLINE = "OFFLINE"
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"


class ProviderProjectBindingStatus(StrEnum):
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
    MIGRATING = "MIGRATING"
    DISABLED = "DISABLED"
    FAILED = "FAILED"


class FlowMigrationStatus(StrEnum):
    PLANNED = "PLANNED"
    USER_REVIEW_REQUIRED = "USER_REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    MIGRATING = "MIGRATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FlowMigrationVerificationStatus(StrEnum):
    PENDING = "PENDING"
    USER_REVIEW_REQUIRED = "USER_REVIEW_REQUIRED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class CredentialStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INVALID = "INVALID"
    ROTATION_REQUIRED = "ROTATION_REQUIRED"
    REVOKED = "REVOKED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class PlatformRole(StrEnum):
    USER = "USER"
    ADMIN = "ADMIN"
    SUPER_ADMIN = "SUPER_ADMIN"


class ModelLifecycleStatus(StrEnum):
    DISABLED = "DISABLED"
    CONFIGURED = "CONFIGURED"
    TESTING = "TESTING"
    VERIFIED = "VERIFIED"
    LIVE = "LIVE"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"


class TimelineTransitionType(StrEnum):
    CONTINUOUS = "CONTINUOUS"
    SCENE_CUT = "SCENE_CUT"
    TIME_JUMP = "TIME_JUMP"
    FLASHBACK = "FLASHBACK"
    FLASH_FORWARD = "FLASH_FORWARD"
    MONTAGE = "MONTAGE"
    DREAM = "DREAM"
    LOCATION_CHANGE = "LOCATION_CHANGE"
    EXPLICIT_RESET = "EXPLICIT_RESET"


class ShotDependencyType(StrEnum):
    """Why a later shot explicitly requires earlier narrative material.

    Similarity retrieval decides what a shot *resembles*; these rows record what
    a shot *requires*. A payoff shares no vocabulary with its setup, so the
    requirement is structural, never left to cosine ranking.
    """

    FORESHADOWING = "FORESHADOWING"
    FACT_REVELATION = "FACT_REVELATION"
    OBLIGATION_FULFILLMENT = "OBLIGATION_FULFILLMENT"
    STATE_INHERITANCE = "STATE_INHERITANCE"


class ShotDependencyOrigin(StrEnum):
    SCRIPT_COMPILER = "SCRIPT_COMPILER"
    MANUAL = "MANUAL"


class ShotNarrativeEffectType(StrEnum):
    """What committing a shot does to the narrative ledger.

    Declared at compile/planning time, applied exactly once inside the
    candidate-commit transaction — the moment the shot's content becomes canon.
    Foreshadowing is an OPEN_OBLIGATION whose metadata records the category.
    """

    ESTABLISH_FACT = "ESTABLISH_FACT"
    DISCLOSE_FACT = "DISCLOSE_FACT"
    OPEN_OBLIGATION = "OPEN_OBLIGATION"
    SETTLE_OBLIGATION = "SETTLE_OBLIGATION"


class ShotNarrativeEffectOrigin(StrEnum):
    SCRIPT_COMPILER = "SCRIPT_COMPILER"
    MANUAL = "MANUAL"
    CREATIVE_DIRECTOR = "CREATIVE_DIRECTOR"
    EPISODE_CONTINUATION = "EPISODE_CONTINUATION"


class CharacterStateProposalKind(StrEnum):
    INITIALIZE = "INITIALIZE"
    NARRATIVE = "NARRATIVE"
    EVIDENCE_DERIVED = "EVIDENCE_DERIVED"
    IDENTITY_REBASE = "IDENTITY_REBASE"


class CharacterStateProposalSource(StrEnum):
    RULES = "RULES"
    LLM = "LLM"
    HUMAN = "HUMAN"
    VISUAL_EVIDENCE = "VISUAL_EVIDENCE"


class CharacterStateValidationStage(StrEnum):
    POLICY = "POLICY"
    VISUAL = "VISUAL"
    HUMAN_OVERRIDE = "HUMAN_OVERRIDE"


class CharacterStateDecision(StrEnum):
    PASS = "PASS"
    REJECT = "REJECT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class CharacterStateValidatorKind(StrEnum):
    RULE_ENGINE = "RULE_ENGINE"
    VLM = "VLM"
    HUMAN = "HUMAN"


class CharacterStateCommitActor(StrEnum):
    SYSTEM = "SYSTEM"
    HUMAN = "HUMAN"


class BillingEvidenceSource(StrEnum):
    VERIFIED_PROVIDER = "VERIFIED_PROVIDER"
    ESTIMATED = "ESTIMATED"
    RECONCILED_MANUAL = "RECONCILED_MANUAL"
    UNKNOWN = "UNKNOWN"


class ContinuityMode(StrEnum):
    NONE = "NONE"
    PREVIOUS_END_FRAME = "PREVIOUS_END_FRAME"
    REFERENCE_FRAME = "REFERENCE_FRAME"
    START_END_FRAME = "START_END_FRAME"
    PROVIDER_CONTINUATION = "PROVIDER_CONTINUATION"
    HARD_CONTINUITY = "HARD_CONTINUITY"
    HYBRID = "HYBRID"
    RE_ANCHOR = "RE_ANCHOR"


class ShotStatus(StrEnum):
    DRAFT = "DRAFT"
    PLANNED = "PLANNED"
    READY = "READY"
    QUEUED = "QUEUED"
    GENERATING = "GENERATING"
    VALIDATING = "VALIDATING"
    REPAIRING = "REPAIRING"
    REGENERATING = "REGENERATING"
    USER_REVIEW_REQUIRED = "USER_REVIEW_REQUIRED"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"


class CandidateStatus(StrEnum):
    CREATED = "CREATED"
    GENERATING = "GENERATING"
    VALIDATING = "VALIDATING"
    PASSED = "PASSED"
    SOFT_FAILED = "SOFT_FAILED"
    HARD_FAILED = "HARD_FAILED"
    USER_REVIEW_REQUIRED = "USER_REVIEW_REQUIRED"
    COMMITTED = "COMMITTED"
    REJECTED = "REJECTED"
    # Terminal state for a row that was pre-allocated as an empty batch-sibling
    # slot by the retired pre-creation scheme and never received media. Only the
    # one-time audit (`scripts/retire_empty_candidates.py`) writes it; the
    # current pipeline creates candidates inside the completion transaction and
    # can no longer leave such rows behind.
    RETIRED = "RETIRED"


class QADecision(StrEnum):
    PASS = "PASS"
    SOFT_FAIL = "SOFT_FAIL"
    HARD_FAIL = "HARD_FAIL"
    USER_REVIEW_REQUIRED = "USER_REVIEW_REQUIRED"


class GenerationPolicy(StrEnum):
    TEXT_TO_VIDEO = "TEXT_TO_VIDEO"
    IMAGE_TO_VIDEO = "IMAGE_TO_VIDEO"
    CONTINUE_I2V = "CONTINUE_I2V"
    CONTINUE_V2V = "CONTINUE_V2V"
    HYBRID_REFERENCE = "HYBRID_REFERENCE"
    REANCHOR_CHARACTER = "REANCHOR_CHARACTER"
    REANCHOR_SCENE = "REANCHOR_SCENE"
    REANCHOR_FULL = "REANCHOR_FULL"
    START_END_FRAME = "START_END_FRAME"
    REFERENCE_TO_VIDEO = "REFERENCE_TO_VIDEO"


class RetryCategory(StrEnum):
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    WORKER_DISCONNECT = "WORKER_DISCONNECT"
    RATE_LIMIT = "RATE_LIMIT"
    CREDENTIAL_EXPIRED = "CREDENTIAL_EXPIRED"
    PROVIDER_BUSY = "PROVIDER_BUSY"
    INVALID_REQUEST = "INVALID_REQUEST"
    CONTENT_REJECTED = "CONTENT_REJECTED"
    PERMANENT_ERROR = "PERMANENT_ERROR"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)
    platform_role: Mapped[str] = mapped_column(
        String(40), default=PlatformRole.USER.value, server_default=PlatformRole.USER.value, nullable=False
    )


class WorkspaceMembership(Base, TimestampMixin):
    __tablename__ = "workspace_memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership_user"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(40), default="VIEWER", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)


class AuthSession(Base, TimestampMixin):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str] = mapped_column(String(500), default="", nullable=False)


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("credit_balance >= 0", name="ck_workspace_credit_balance"),
        CheckConstraint("max_storage_bytes > 0", name="ck_workspace_max_storage_positive"),
        CheckConstraint("used_storage_bytes >= 0", name="ck_workspace_storage_used_nonnegative"),
        CheckConstraint("reserved_storage_bytes >= 0", name="ck_workspace_storage_reserved_nonnegative"),
        CheckConstraint(
            "used_storage_bytes + reserved_storage_bytes <= max_storage_bytes",
            name="ck_workspace_storage_capacity",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)
    plan_tier: Mapped[str] = mapped_column(String(40), default="FREE", nullable=False)
    credit_balance: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    max_storage_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=5 * 1024 * 1024 * 1024,
        nullable=False,
    )
    used_storage_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reserved_storage_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class WorkspaceUsageCounter(Base, TimestampMixin):
    """Server-owned counters behind the FREE plan's hard usage gates.

    One row per workspace, created on first metered use. Increments happen
    inside the transaction that admits the metered action (row-locked on
    PostgreSQL), so the browser cannot spend past a limit by racing requests.
    """

    __tablename__ = "workspace_usage_counters"
    __table_args__ = (
        CheckConstraint(
            "prompt_optimizations >= 0", name="ck_workspace_usage_prompt_optimizations"
        ),
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    prompt_optimizations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class WorkspaceCreditEntry(Base, TimestampMixin):
    """Current state of one server-priced generation credit reservation."""

    __tablename__ = "workspace_credit_entries"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_credit_entry_project_key"),
        UniqueConstraint("generation_job_id", name="uq_credit_entry_generation_job"),
        CheckConstraint("credits > 0", name="ck_workspace_credit_entry_positive"),
        CheckConstraint("balance_after >= 0", name="ck_workspace_credit_entry_balance"),
        CheckConstraint("settled_credits >= 0", name="ck_credit_entry_settled_nonnegative"),
        CheckConstraint("refunded_credits >= 0", name="ck_credit_entry_refunded_nonnegative"),
        CheckConstraint(
            "settled_credits + refunded_credits <= credits",
            name="ck_credit_entry_allocation",
        ),
        CheckConstraint("version > 0", name="ck_credit_entry_version_positive"),
        CheckConstraint(
            "status IN ('RESERVED', 'SETTLED', 'REFUNDED', 'RECONCILIATION_REQUIRED')",
            name="ck_credit_entry_status",
        ),
        CheckConstraint(
            "(status = 'RESERVED' AND settled_credits = 0 AND refunded_credits = 0 "
            "AND settled_at IS NULL AND refunded_at IS NULL "
            "AND reconciliation_required_at IS NULL) OR "
            "(status = 'RECONCILIATION_REQUIRED' AND settled_credits = 0 "
            "AND refunded_credits = 0 AND settled_at IS NULL AND refunded_at IS NULL "
            "AND reconciliation_required_at IS NOT NULL "
            "AND reconciliation_reason IS NOT NULL) OR "
            "(status = 'SETTLED' AND settled_credits = credits AND refunded_credits = 0 "
            "AND settled_at IS NOT NULL AND refunded_at IS NULL) OR "
            "(status = 'REFUNDED' AND settled_credits = 0 AND refunded_credits = credits "
            "AND refunded_at IS NOT NULL AND settled_at IS NULL)",
            name="ck_credit_entry_state_allocation",
        ),
        Index("ix_workspace_credit_entries_status", "status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str] = mapped_column(String(250), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    settled_credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    refunded_credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="RESERVED", nullable=False)
    reason: Mapped[str] = mapped_column(String(120), default="GENERATION_RESERVED", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciliation_required_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciliation_reason: Mapped[str | None] = mapped_column(String(240))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class WorkspaceCreditEvent(Base):
    """Append-only audit fact emitted for every wallet lifecycle transition."""

    __tablename__ = "workspace_credit_events"
    __table_args__ = (
        UniqueConstraint("credit_entry_id", "event_key", name="uq_credit_event_entry_key"),
        CheckConstraint("credits >= 0", name="ck_credit_event_credits_nonnegative"),
        CheckConstraint("balance_after >= 0", name="ck_credit_event_balance_nonnegative"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    credit_entry_id: Mapped[str] = mapped_column(
        ForeignKey("workspace_credit_entries.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"), index=True
    )
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    balance_delta: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(240), nullable=False)
    actor_type: Mapped[str] = mapped_column(
        String(80), default="SYSTEM", server_default="SYSTEM", nullable=False
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class AlchemyWebhookDelivery(Base):
    """Immutable receipt for one authenticated Alchemy webhook delivery."""

    __tablename__ = "alchemy_webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("provider_event_id", name="uq_alchemy_delivery_provider_event"),
        CheckConstraint("activity_count >= 0", name="ck_alchemy_delivery_activity_count"),
        CheckConstraint("accepted_count >= 0", name="ck_alchemy_delivery_accepted_count"),
        CheckConstraint("credited_count >= 0", name="ck_alchemy_delivery_credited_count"),
        CheckConstraint("ignored_count >= 0", name="ck_alchemy_delivery_ignored_count"),
        CheckConstraint("length(payload_hash) = 64", name="ck_alchemy_delivery_payload_hash"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider_event_id: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    webhook_id: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    webhook_type: Mapped[str] = mapped_column(String(80), nullable=False)
    network: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    activity_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credited_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ignored_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result: Mapped[str] = mapped_column(String(40), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class DePayCheckoutSession(Base, TimestampMixin):
    """One authenticated workspace handoff to the shared DePay payment link."""

    __tablename__ = "depay_checkout_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_depay_checkout_token_hash"),
        UniqueConstraint("payment_intent_id", name="uq_depay_checkout_payment_intent"),
        CheckConstraint("requested_quantity > 0", name="ck_depay_checkout_quantity_positive"),
        CheckConstraint("credits_granted >= 0", name="ck_depay_checkout_credits_nonnegative"),
        CheckConstraint(
            "status IN ('PENDING', 'PAID', 'EXPIRED', 'CANCELLED', 'RECONCILIATION_REQUIRED')",
            name="ck_depay_checkout_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    payment_intent_id: Mapped[str | None] = mapped_column(
        ForeignKey("onchain_payment_intents.id", ondelete="RESTRICT"),
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    credits_granted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="PENDING", index=True, nullable=False)
    payment_id: Mapped[str | None] = mapped_column(
        ForeignKey("onchain_payments.id", ondelete="RESTRICT"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class DePayWebhookDelivery(Base):
    """Append-only receipt for one authenticated DePay callback transaction."""

    __tablename__ = "depay_webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_depay_delivery_event_key"),
        CheckConstraint("length(payload_hash) = 64", name="ck_depay_delivery_payload_hash"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_key: Mapped[str] = mapped_column(String(240), index=True, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    link_id: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    checkout_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("depay_checkout_sessions.id", ondelete="RESTRICT"), index=True
    )
    payment_id: Mapped[str | None] = mapped_column(
        ForeignKey("onchain_payments.id", ondelete="RESTRICT"), index=True
    )
    result: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class EIP3009Authorization(Base, TimestampMixin):
    """One user-signed Base USDC authorization submitted by the platform relayer."""

    __tablename__ = "eip3009_authorizations"
    __table_args__ = (
        UniqueConstraint("payment_intent_id", name="uq_eip3009_authorization_payment_intent"),
        UniqueConstraint("nonce", name="uq_eip3009_authorization_nonce"),
        UniqueConstraint("transaction_hash", name="uq_eip3009_authorization_transaction_hash"),
        CheckConstraint("chain_id > 0", name="ck_eip3009_authorization_chain_positive"),
        CheckConstraint("value_microunits > 0", name="ck_eip3009_authorization_value_positive"),
        CheckConstraint("valid_after >= 0", name="ck_eip3009_authorization_valid_after"),
        CheckConstraint("valid_before > valid_after", name="ck_eip3009_authorization_window"),
        CheckConstraint("attempt_count >= 0", name="ck_eip3009_authorization_attempts"),
        CheckConstraint(
            "status IN ('PENDING', 'SUBMITTING', 'SUBMITTED', 'CONFIRMED', 'FAILED', "
            "'EXPIRED', 'CANCELLED', "
            "'RECONCILIATION_REQUIRED')",
            name="ck_eip3009_authorization_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    payment_intent_id: Mapped[str] = mapped_column(
        ForeignKey("onchain_payment_intents.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_address: Mapped[str] = mapped_column(String(42), nullable=False)
    from_address: Mapped[str] = mapped_column(String(42), index=True, nullable=False)
    to_address: Mapped[str] = mapped_column(String(42), index=True, nullable=False)
    value_microunits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    valid_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    valid_before: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    nonce: Mapped[str] = mapped_column(String(66), nullable=False)
    typed_data_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_hash: Mapped[str | None] = mapped_column(String(64))
    raw_transaction: Mapped[str | None] = mapped_column(Text)
    relayer_address: Mapped[str] = mapped_column(String(42), nullable=False)
    relayer_nonce: Mapped[int | None] = mapped_column(BigInteger)
    transaction_hash: Mapped[str | None] = mapped_column(String(66), index=True)
    status: Mapped[str] = mapped_column(String(40), default="PENDING", index=True, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(120))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class RelayerAccountState(Base, TimestampMixin):
    """Database lock row serializing one relayer account's transaction nonce."""

    __tablename__ = "relayer_account_states"
    __table_args__ = (
        CheckConstraint("chain_id > 0", name="ck_relayer_account_chain_positive"),
        CheckConstraint(
            "last_submitted_nonce IS NULL OR last_submitted_nonce >= 0",
            name="ck_relayer_account_nonce_nonnegative",
        ),
    )
    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_submitted_nonce: Mapped[int | None] = mapped_column(BigInteger)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class WorkspaceWalletBinding(Base, TimestampMixin):
    """Workspace ownership projection for a verified EVM wallet."""

    __tablename__ = "workspace_wallet_bindings"
    __table_args__ = (
        UniqueConstraint("chain_id", "address", name="uq_workspace_wallet_chain_address"),
        CheckConstraint("chain_id > 0", name="ck_workspace_wallet_chain_positive"),
        CheckConstraint("length(address) = 42", name="ck_workspace_wallet_address_length"),
        CheckConstraint("address = lower(address)", name="ck_workspace_wallet_address_lowercase"),
        CheckConstraint(
            "status IN ('PENDING', 'VERIFIED', 'REVOKED')",
            name="ck_workspace_wallet_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    chain_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    address: Mapped[str] = mapped_column(String(42), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="PENDING", index=True, nullable=False)
    verified_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class WalletBindingChallenge(Base, TimestampMixin):
    """Single-use wallet ownership challenge issued to an authenticated workspace member."""

    __tablename__ = "wallet_binding_challenges"
    __table_args__ = (
        UniqueConstraint("nonce_hash", name="uq_wallet_binding_challenge_nonce_hash"),
        CheckConstraint("chain_id > 0", name="ck_wallet_binding_challenge_chain_positive"),
        CheckConstraint("length(address) = 42", name="ck_wallet_binding_challenge_address_length"),
        CheckConstraint("address = lower(address)", name="ck_wallet_binding_challenge_address_lowercase"),
        CheckConstraint(
            "length(message_hash) = 64",
            name="ck_wallet_binding_challenge_message_hash",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    chain_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    address: Mapped[str] = mapped_column(String(42), index=True, nullable=False)
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    message_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class OnchainPaymentIntent(Base, TimestampMixin):
    """Server-priced payment order, frozen at creation.

    The `sku`/`amount`/`currency`/`credits`/`pricing_version`/`provider` columns
    are the immutable commercial snapshot: settlement validates the paid amount
    against this row, never against the live catalogue, so republishing prices
    cannot change what an order already sold. `PaymentOrder` is the name the
    payment services use for exactly this table.
    """

    __tablename__ = "onchain_payment_intents"
    __table_args__ = (
        UniqueConstraint("transaction_hash", name="uq_onchain_payment_intent_transaction_hash"),
        CheckConstraint("chain_id IS NULL OR chain_id > 0", name="ck_payment_intent_chain_positive"),
        CheckConstraint("raw_amount_microunits > 0", name="ck_payment_intent_amount_positive"),
        CheckConstraint("credits > 0", name="ck_payment_intent_credits_positive"),
        CheckConstraint("amount > 0", name="ck_payment_intent_snapshot_amount_positive"),
        CheckConstraint(
            "status IN ('PENDING', 'SUBMITTED', 'PAID', 'EXPIRED', 'CANCELLED', 'RECONCILIATION_REQUIRED')",
            name="ck_payment_intent_status",
        ),
        Index("ix_onchain_payment_intents_provider", "provider"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    wallet_binding_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspace_wallet_bindings.id", ondelete="RESTRICT"), index=True
    )
    network: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    chain_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    from_address: Mapped[str | None] = mapped_column(String(42), index=True)
    to_address: Mapped[str | None] = mapped_column(String(42), index=True)
    token_address: Mapped[str | None] = mapped_column(String(42), index=True)
    sku: Mapped[str] = mapped_column(
        String(80), default="legacy_direct", server_default="'legacy_direct'", nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), default=Decimal("0.01"), server_default="0.01", nullable=False
    )
    currency: Mapped[str] = mapped_column(
        String(20), default="USDC", server_default="'USDC'", nullable=False
    )
    raw_amount_microunits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    pricing_version: Mapped[str] = mapped_column(
        String(80), default="legacy", server_default="'legacy'", nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(40), default="ALCHEMY", server_default="'ALCHEMY'", nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    transaction_hash: Mapped[str | None] = mapped_column(String(66), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


# The payment services speak in orders, not intents. Same table, same rows.
PaymentOrder = OnchainPaymentIntent


class XunhuPayCheckoutSession(Base, TimestampMixin):
    """One server-priced CNY checkout created with the XunHuPay gateway."""

    __tablename__ = "xunhupay_checkout_sessions"
    __table_args__ = (
        UniqueConstraint("payment_order_id", name="uq_xunhupay_checkout_payment_order"),
        UniqueConstraint("trade_order_id", name="uq_xunhupay_checkout_trade_order"),
        CheckConstraint("credits_granted >= 0", name="ck_xunhupay_checkout_credits_nonnegative"),
        CheckConstraint(
            "status IN ('PENDING', 'PAID', 'EXPIRED', 'CANCELLED', 'RECONCILIATION_REQUIRED')",
            name="ck_xunhupay_checkout_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    payment_order_id: Mapped[str] = mapped_column(
        ForeignKey("onchain_payment_intents.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    trade_order_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    gateway_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    checkout_url: Mapped[str | None] = mapped_column(Text)
    qrcode_url: Mapped[str | None] = mapped_column(Text)
    credits_granted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="PENDING", index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class XunhuPaySettlement(Base):
    """Immutable authenticated XunHuPay payment fact used to post the credit ledger."""

    __tablename__ = "xunhupay_settlements"
    __table_args__ = (
        UniqueConstraint("payment_order_id", name="uq_xunhupay_settlement_payment_order"),
        UniqueConstraint("transaction_id", name="uq_xunhupay_settlement_transaction"),
        UniqueConstraint("open_order_id", name="uq_xunhupay_settlement_open_order"),
        CheckConstraint("amount > 0", name="ck_xunhupay_settlement_amount_positive"),
        CheckConstraint("credits_granted >= 0", name="ck_xunhupay_settlement_credits_nonnegative"),
        CheckConstraint("length(payload_hash) = 64", name="ck_xunhupay_settlement_payload_hash"),
        CheckConstraint(
            "status IN ('CREDITED', 'RECONCILIATION_REQUIRED')",
            name="ck_xunhupay_settlement_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    checkout_session_id: Mapped[str] = mapped_column(
        ForeignKey("xunhupay_checkout_sessions.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    payment_order_id: Mapped[str] = mapped_column(
        ForeignKey("onchain_payment_intents.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    transaction_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    open_order_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(20), default="CNY", nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    credits_granted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class OnchainPayment(Base, TimestampMixin):
    """Canonical Base USDC transfer fact derived from an authenticated delivery."""

    __tablename__ = "onchain_payments"
    __table_args__ = (
        UniqueConstraint("network", "transaction_hash", "log_index", name="uq_onchain_payment_log"),
        CheckConstraint("chain_id > 0", name="ck_onchain_payment_chain_positive"),
        CheckConstraint("token_decimals = 6", name="ck_onchain_payment_usdc_decimals"),
        CheckConstraint("raw_amount_microunits > 0", name="ck_onchain_payment_amount_positive"),
        CheckConstraint("credits_granted >= 0", name="ck_onchain_payment_credits_nonnegative"),
        CheckConstraint(
            "status IN ('RECEIVED', 'UNMATCHED', 'CREDITED', 'REMOVED', 'RECONCILIATION_REQUIRED')",
            name="ck_onchain_payment_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    network: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    chain_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    transaction_hash: Mapped[str] = mapped_column(String(66), index=True, nullable=False)
    log_index: Mapped[str] = mapped_column(String(66), nullable=False)
    block_number: Mapped[str] = mapped_column(String(66), nullable=False)
    from_address: Mapped[str] = mapped_column(String(42), index=True, nullable=False)
    to_address: Mapped[str] = mapped_column(String(42), index=True, nullable=False)
    token_address: Mapped[str] = mapped_column(String(42), index=True, nullable=False)
    token_decimals: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_amount_microunits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True
    )
    wallet_binding_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspace_wallet_bindings.id", ondelete="RESTRICT"), index=True
    )
    payment_intent_id: Mapped[str | None] = mapped_column(
        ForeignKey("onchain_payment_intents.id", ondelete="RESTRICT"), index=True
    )
    provider_event_id: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    credits_granted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class WorkspaceCreditLedgerEntry(Base):
    """Append-only balance mutation for purchases and their chain-reorg reversals."""

    __tablename__ = "workspace_credit_ledger_entries"
    __table_args__ = (
        UniqueConstraint("external_reference", name="uq_workspace_credit_ledger_external_reference"),
        CheckConstraint("credits > 0", name="ck_workspace_credit_ledger_credits_positive"),
        CheckConstraint("balance_before >= 0", name="ck_workspace_credit_ledger_before_nonnegative"),
        CheckConstraint("balance_after >= 0", name="ck_workspace_credit_ledger_after_nonnegative"),
        CheckConstraint("direction IN ('CREDIT', 'DEBIT')", name="ck_workspace_credit_ledger_direction"),
        CheckConstraint(
            "entry_type IN ('USDC_PURCHASE', 'USDC_REORG_REVERSAL', 'CNY_PURCHASE')",
            name="ck_workspace_credit_ledger_entry_type",
        ),
        CheckConstraint(
            "(payment_id IS NOT NULL AND xunhupay_settlement_id IS NULL) OR "
            "(payment_id IS NULL AND xunhupay_settlement_id IS NOT NULL)",
            name="ck_workspace_credit_ledger_payment_source",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    payment_id: Mapped[str | None] = mapped_column(
        ForeignKey("onchain_payments.id", ondelete="RESTRICT"), index=True
    )
    xunhupay_settlement_id: Mapped[str | None] = mapped_column(
        ForeignKey("xunhupay_settlements.id", ondelete="RESTRICT"), index=True
    )
    related_entry_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspace_credit_ledger_entries.id", ondelete="RESTRICT"), index=True
    )
    external_reference: Mapped[str] = mapped_column(String(240), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_before: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(20), default="USDC", nullable=False)
    raw_amount_microunits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chain_id: Mapped[int | None] = mapped_column(BigInteger)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class AdminCreditAdjustment(Base):
    """Append-only operator credit mutation, separate from payment evidence."""

    __tablename__ = "admin_credit_adjustments"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_admin_credit_adjustment_idempotency"),
        CheckConstraint("delta != 0", name="ck_admin_credit_adjustment_delta_nonzero"),
        CheckConstraint("before_balance >= 0", name="ck_admin_credit_adjustment_before_nonnegative"),
        CheckConstraint("after_balance >= 0", name="ck_admin_credit_adjustment_after_nonnegative"),
        Index("ix_admin_credit_adjustments_workspace_created", "workspace_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    operator_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    before_balance: Mapped[int] = mapped_column(Integer, nullable=False)
    after_balance: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(240))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class AdminAuditLog(Base):
    """Append-only, redacted record of every high-impact platform mutation."""

    __tablename__ = "admin_audit_logs"
    __table_args__ = (
        Index("ix_admin_audit_entity_created", "entity_type", "entity_id", "created_at"),
        Index("ix_admin_audit_actor_created", "actor_user_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    actor_role: Mapped[str] = mapped_column(String(40), nullable=False)
    action: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    entity_id: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    before_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    request_id: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class LegacyWorkspaceClaim(Base, TimestampMixin):
    """Append-only audit record for an explicit legacy-data ownership transfer."""

    __tablename__ = "legacy_workspace_claims"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_legacy_workspace_claim_idempotency"),
        UniqueConstraint("legacy_user_id", name="uq_legacy_workspace_claim_legacy_user"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    legacy_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    target_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    actor_type: Mapped[str] = mapped_column(String(80), nullable=False)
    workspace_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    project_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="COMPLETED", nullable=False)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)
    default_aspect_ratio: Mapped[str] = mapped_column(String(20), default="9:16", nullable=False)
    default_provider: Mapped[str] = mapped_column(String(80), default="google_flow", nullable=False)
    default_language: Mapped[str] = mapped_column(String(30), default="zh-CN", nullable=False)
    canonical_style_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("asset_versions.id", ondelete="RESTRICT"), index=True
    )
    episodes: Mapped[list[Episode]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Episode(Base, TimestampMixin):
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("project_id", "episode_number", name="uq_episode_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    script_source: Mapped[str] = mapped_column(Text, default="", nullable=False)
    script_structured: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT", nullable=False)
    project: Mapped[Project] = relationship(back_populates="episodes")
    scenes: Mapped[list[Scene]] = relationship(back_populates="episode", cascade="all, delete-orphan")


class Scene(Base, TimestampMixin):
    __tablename__ = "scenes"
    __table_args__ = (UniqueConstraint("episode_id", "sequence", name="uq_scene_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), index=True)
    location_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    time_context: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    scene_description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    world_state_id: Mapped[str | None] = mapped_column(ForeignKey("timeline_states.id"))
    lighting_preset_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(40), default="PLANNED", nullable=False)
    episode: Mapped[Episode] = relationship(back_populates="scenes")
    shots: Mapped[list[Shot]] = relationship(back_populates="scene", cascade="all, delete-orphan")


class Shot(Base, TimestampMixin):
    __tablename__ = "shots"
    __table_args__ = (UniqueConstraint("scene_id", "sequence", name="uq_shot_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scene_id: Mapped[str] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    shot_type: Mapped[str] = mapped_column(String(60), default="MEDIUM", nullable=False)
    duration: Mapped[float] = mapped_column(Float, default=8.0, nullable=False)
    user_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    compiled_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    #: The approved director intent behind this shot: the staged action
    #: description, start and end states, gaze target, per-shot continuity
    #: obligations, the key-visual anchors it depends on and the reference
    #: media those resolve to. Live input to prompt compilation - re-read on
    #: every recompile and retry - as opposed to CreativeShotLineage.intent_json,
    #: which is the immutable history of what was approved. Empty for shots
    #: that did not come from a screenplay.
    director_intent_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=False
    )
    negative_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    provider: Mapped[str] = mapped_column(String(80), default="google_flow", nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="veo", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT", nullable=False)
    previous_shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), nullable=True)
    next_shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), nullable=True)
    input_state_id: Mapped[str | None] = mapped_column(ForeignKey("timeline_states.id"))
    output_state_id: Mapped[str | None] = mapped_column(ForeignKey("timeline_states.id"))
    camera_state_id: Mapped[str | None] = mapped_column(String(36))
    lighting_state_id: Mapped[str | None] = mapped_column(String(36))
    blocking_state_id: Mapped[str | None] = mapped_column(String(36))
    start_frame_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), nullable=True)
    end_frame_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), nullable=True)
    output_video_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), nullable=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), nullable=True)
    committed_candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_candidates.id"), nullable=True
    )
    continuity_policy: Mapped[str] = mapped_column(String(60), default="HYBRID", nullable=False)
    generation_policy: Mapped[str] = mapped_column(
        String(60), default=GenerationPolicy.TEXT_TO_VIDEO.value, nullable=False
    )
    preferred_provider: Mapped[str] = mapped_column(String(80), default="google_flow", nullable=False)
    preferred_model: Mapped[str] = mapped_column(String(120), default="veo", nullable=False)
    continuity_mode: Mapped[str] = mapped_column(
        String(50), default=ContinuityMode.NONE.value, nullable=False
    )
    downstream_state_stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stale_reason: Mapped[str | None] = mapped_column(String(240))
    stale_from_shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    scene: Mapped[Scene] = relationship(back_populates="shots")


class TimelineState(Base, TimestampMixin):
    __tablename__ = "timeline_states"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    episode_id: Mapped[str | None] = mapped_column(ForeignKey("episodes.id"), index=True)
    scene_id: Mapped[str | None] = mapped_column(ForeignKey("scenes.id"), index=True)
    shot_id: Mapped[str | None] = mapped_column(String(36), index=True)
    previous_state_id: Mapped[str | None] = mapped_column(ForeignKey("timeline_states.id"))
    state_kind: Mapped[str] = mapped_column(String(40), default="SHOT_INPUT", nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    semantic_embedding: Mapped[list[float] | None] = mapped_column(Vector(16).with_variant(JSON(), "sqlite"))
    visual_embedding: Mapped[list[float] | None] = mapped_column(Vector(16).with_variant(JSON(), "sqlite"))
    camera_embedding: Mapped[list[float] | None] = mapped_column(Vector(16).with_variant(JSON(), "sqlite"))
    character_track_embedding: Mapped[list[float] | None] = mapped_column(
        Vector(16).with_variant(JSON(), "sqlite")
    )


class ShotStateSnapshot(Base, TimestampMixin):
    __tablename__ = "shot_state_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shot_id: Mapped[str] = mapped_column(ForeignKey("shots.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(36), index=True)
    timeline_state_id: Mapped[str] = mapped_column(ForeignKey("timeline_states.id"), index=True)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class NarrativeEvent(Base, TimestampMixin):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(240), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(36))
    object_id: Mapped[str | None] = mapped_column(String(36))
    dialogue: Mapped[str] = mapped_column(Text, default="", nullable=False)
    preconditions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    effects: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timeline_position: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    source_text: Mapped[str] = mapped_column(Text, default="", nullable=False)


class Character(Base, TimestampMixin):
    __tablename__ = "characters"
    __table_args__ = (UniqueConstraint("id", "project_id", name="uq_characters_id_project"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    canonical_facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT", nullable=False)
    current_identity_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("character_identity_versions.id", ondelete="SET NULL"), index=True
    )


class CharacterIdentityVersion(Base, TimestampMixin):
    __tablename__ = "character_identity_versions"
    __table_args__ = (
        UniqueConstraint("character_id", "version", name="uq_character_identity_version"),
        UniqueConstraint("id", "character_id", name="uq_character_identity_id_character"),
        CheckConstraint("version > 0", name="ck_character_identity_version_positive"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    master_asset_id: Mapped[str] = mapped_column(ForeignKey("media_assets.id"), nullable=False)
    front_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    left_profile_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    right_profile_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    three_quarter_left_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    three_quarter_right_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    full_body_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    face_embedding: Mapped[list[float] | None] = mapped_column(JSON)
    appearance_embedding: Mapped[list[float] | None] = mapped_column(JSON)
    hair_signature: Mapped[str] = mapped_column(Text, default="", nullable=False)
    costume_signature: Mapped[str] = mapped_column(Text, default="", nullable=False)
    provider_bindings_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="LOCKED", nullable=False)
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class NarrativeFact(Base, TimestampMixin):
    """One story fact established at a point in the series, append-only.

    A fact is what *happened*. Who is entitled to know it is tracked separately
    by NarrativeDisclosure, because the same fact reaches the audience and each
    character at different moments - that gap is what dramatic irony is made of.
    """

    __tablename__ = "narrative_facts"
    __table_args__ = (
        UniqueConstraint("project_id", "fact_key", name="uq_narrative_fact_key"),
        CheckConstraint("length(fact_key) > 0", name="ck_narrative_fact_key_nonempty"),
        CheckConstraint("length(fact_hash) = 64", name="ck_narrative_fact_hash_length"),
        CheckConstraint("established_episode > 0", name="ck_narrative_fact_episode_positive"),
        Index("ix_narrative_fact_lookup", "project_id", "established_episode"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    fact_key: Mapped[str] = mapped_column(String(160), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    fact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    established_episode: Mapped[int] = mapped_column(Integer, nullable=False)
    # Scene/shot sequence complete the narrative position within the episode.
    # 0 means "start of episode" — the pre-position, episode-granular legacy
    # value; real shots are 1-based, so 0 sorts before every actual shot.
    established_scene_sequence: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    established_shot_sequence: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    established_shot_id: Mapped[str | None] = mapped_column(
        ForeignKey("shots.id", ondelete="SET NULL"), index=True
    )
    subject_character_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class NarrativeDisclosure(Base, TimestampMixin):
    """Append-only record that a fact became known to one holder.

    ``holder_key`` is a character ID, or ``AUDIENCE`` for the viewer. A shot may
    only let a character act on a fact that was disclosed to them at or before
    the current episode; the audience knowing it is never sufficient.
    """

    __tablename__ = "narrative_disclosures"
    __table_args__ = (
        UniqueConstraint("fact_id", "holder_key", name="uq_narrative_disclosure_holder"),
        CheckConstraint("length(holder_key) > 0", name="ck_narrative_disclosure_holder_nonempty"),
        CheckConstraint("disclosed_episode > 0", name="ck_narrative_disclosure_episode_positive"),
        Index("ix_narrative_disclosure_lookup", "project_id", "holder_key", "disclosed_episode"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    fact_id: Mapped[str] = mapped_column(
        ForeignKey("narrative_facts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    holder_key: Mapped[str] = mapped_column(String(64), nullable=False)
    disclosed_episode: Mapped[int] = mapped_column(Integer, nullable=False)
    disclosed_scene_sequence: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    disclosed_shot_sequence: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    disclosed_shot_id: Mapped[str | None] = mapped_column(
        ForeignKey("shots.id", ondelete="SET NULL"), index=True
    )
    channel: Mapped[str] = mapped_column(String(40), default="ON_SCREEN", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class NarrativeObligation(Base, TimestampMixin):
    """A setup that owes the viewer a payoff, and whether it has been paid.

    Retrieval by similarity cannot surface these: episode 60's payoff shares no
    vocabulary with episode 7's promise. An obligation is *owed*, not *similar*,
    so it is tracked explicitly and carried into every later episode's context.
    """

    __tablename__ = "narrative_obligations"
    __table_args__ = (
        UniqueConstraint("project_id", "obligation_key", name="uq_narrative_obligation_key"),
        CheckConstraint("length(obligation_key) > 0", name="ck_narrative_obligation_key_nonempty"),
        CheckConstraint("opened_episode > 0", name="ck_narrative_obligation_open_positive"),
        CheckConstraint(
            "settled_episode IS NULL OR settled_episode >= opened_episode",
            name="ck_narrative_obligation_settled_after_open",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'SETTLED', 'ABANDONED')",
            name="ck_narrative_obligation_status",
        ),
        CheckConstraint(
            "(status = 'OPEN' AND settled_episode IS NULL) OR "
            "(status != 'OPEN' AND settled_episode IS NOT NULL)",
            name="ck_narrative_obligation_status_settled_pair",
        ),
        Index("ix_narrative_obligation_open", "project_id", "status", "opened_episode"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    obligation_key: Mapped[str] = mapped_column(String(160), nullable=False)
    promise: Mapped[str] = mapped_column(Text, nullable=False)
    opened_episode: Mapped[int] = mapped_column(Integer, nullable=False)
    opened_scene_sequence: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    opened_shot_sequence: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    opened_shot_id: Mapped[str | None] = mapped_column(
        ForeignKey("shots.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="OPEN", nullable=False)
    settled_episode: Mapped[int | None] = mapped_column(Integer)
    settled_scene_sequence: Mapped[int | None] = mapped_column(Integer)
    settled_shot_sequence: Mapped[int | None] = mapped_column(Integer)
    settled_shot_id: Mapped[str | None] = mapped_column(
        ForeignKey("shots.id", ondelete="SET NULL"), index=True
    )
    settled_reason: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ShotDependency(Base, TimestampMixin):
    """One explicit narrative dependency of a shot on earlier material.

    ``target_shot_id`` is the shot that depends; the referent is an earlier
    shot, an established narrative fact, or an obligation — at least one is
    required, and the type-specific constraints below hold each dependency kind
    to the referent that makes it checkable. Rows are written by script
    compilation or by manual editing, and retrieval must force them into
    generation context rather than hoping similarity surfaces them.

    ``source_shot_id`` carries no ON DELETE action on purpose: deleting a shot
    that a *surviving* shot explicitly depends on must fail loudly, while a
    delete that removes both ends in one statement passes because the row
    cascades away with its target.
    """

    __tablename__ = "shot_dependencies"
    __table_args__ = (
        UniqueConstraint("target_shot_id", "dependency_key", name="uq_shot_dependency_key"),
        CheckConstraint(
            "dependency_type IN ('FORESHADOWING', 'FACT_REVELATION', "
            "'OBLIGATION_FULFILLMENT', 'STATE_INHERITANCE')",
            name="ck_shot_dependency_type",
        ),
        CheckConstraint(
            "origin IN ('SCRIPT_COMPILER', 'MANUAL')",
            name="ck_shot_dependency_origin",
        ),
        CheckConstraint(
            "source_shot_id IS NOT NULL OR fact_key IS NOT NULL OR obligation_key IS NOT NULL",
            name="ck_shot_dependency_referent",
        ),
        CheckConstraint(
            "dependency_type != 'FACT_REVELATION' OR fact_key IS NOT NULL",
            name="ck_shot_dependency_fact_referent",
        ),
        CheckConstraint(
            "dependency_type != 'OBLIGATION_FULFILLMENT' OR obligation_key IS NOT NULL",
            name="ck_shot_dependency_obligation_referent",
        ),
        CheckConstraint(
            "dependency_type != 'STATE_INHERITANCE' OR source_shot_id IS NOT NULL",
            name="ck_shot_dependency_state_referent",
        ),
        Index("ix_shot_dependency_target", "project_id", "target_shot_id"),
        Index("ix_shot_dependency_source_shot", "source_shot_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    target_shot_id: Mapped[str] = mapped_column(
        ForeignKey("shots.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), nullable=True)
    dependency_type: Mapped[str] = mapped_column(String(40), nullable=False)
    fact_key: Mapped[str | None] = mapped_column(String(160))
    obligation_key: Mapped[str | None] = mapped_column(String(160))
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    origin: Mapped[str] = mapped_column(
        String(40), default=ShotDependencyOrigin.MANUAL.value, nullable=False
    )
    dependency_key: Mapped[str] = mapped_column(String(420), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    @staticmethod
    def natural_key(
        dependency_type: str,
        *,
        source_shot_id: str | None = None,
        fact_key: str | None = None,
        obligation_key: str | None = None,
    ) -> str:
        """The idempotency key: one row per (type, referent) on a target shot."""

        return "|".join(
            [dependency_type, source_shot_id or "", fact_key or "", obligation_key or ""]
        )


class ShotNarrativeEffect(Base, TimestampMixin):
    """One declared ledger consequence of a shot, applied when the shot commits.

    Declarations are written by script compilation (explicit directives),
    manual editing, or upper-level planning; the *ledger* rows they imply are
    written exactly once, inside the candidate-commit transaction, at the
    shot's complete narrative position. ``applied_at``/``applied_candidate_id``
    record that application; a commit replay verifies instead of re-writing.

    The position columns denormalize the shot's (episode, scene, shot) order at
    declaration time so pending effects are comparable to ledger positions
    without joins. Recompiling an episode deletes its shots and these rows
    cascade with them — safe, because effects only apply at commit and a
    committed shot refuses recompilation.
    """

    __tablename__ = "shot_narrative_effects"
    __table_args__ = (
        UniqueConstraint("shot_id", "effect_key", name="uq_shot_narrative_effect_key"),
        CheckConstraint(
            "effect_type IN ('ESTABLISH_FACT', 'DISCLOSE_FACT', "
            "'OPEN_OBLIGATION', 'SETTLE_OBLIGATION')",
            name="ck_shot_narrative_effect_type",
        ),
        CheckConstraint(
            "origin IN ('SCRIPT_COMPILER', 'MANUAL', 'CREATIVE_DIRECTOR', "
            "'EPISODE_CONTINUATION')",
            name="ck_shot_narrative_effect_origin",
        ),
        CheckConstraint(
            "effect_type NOT IN ('ESTABLISH_FACT', 'DISCLOSE_FACT') OR fact_key IS NOT NULL",
            name="ck_shot_narrative_effect_fact_referent",
        ),
        CheckConstraint(
            "effect_type NOT IN ('OPEN_OBLIGATION', 'SETTLE_OBLIGATION') "
            "OR obligation_key IS NOT NULL",
            name="ck_shot_narrative_effect_obligation_referent",
        ),
        CheckConstraint(
            "episode_number > 0 AND scene_sequence > 0 AND shot_sequence > 0",
            name="ck_shot_narrative_effect_position",
        ),
        Index("ix_shot_narrative_effect_fact", "project_id", "fact_key"),
        Index("ix_shot_narrative_effect_obligation", "project_id", "obligation_key"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    shot_id: Mapped[str] = mapped_column(
        ForeignKey("shots.id", ondelete="CASCADE"), index=True, nullable=False
    )
    effect_type: Mapped[str] = mapped_column(String(40), nullable=False)
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    scene_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    shot_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    fact_key: Mapped[str | None] = mapped_column(String(160))
    obligation_key: Mapped[str | None] = mapped_column(String(160))
    holder_key: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    channel: Mapped[str] = mapped_column(String(40), default="ON_SCREEN", nullable=False)
    disclose_to: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    subject_character_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    origin: Mapped[str] = mapped_column(
        String(40), default=ShotNarrativeEffectOrigin.MANUAL.value, nullable=False
    )
    effect_key: Mapped[str] = mapped_column(String(420), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_candidate_id: Mapped[str | None] = mapped_column(String(36), index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    @staticmethod
    def natural_key(
        effect_type: str,
        *,
        fact_key: str | None = None,
        obligation_key: str | None = None,
        holder_key: str | None = None,
    ) -> str:
        """One row per (type, referent, holder) on a shot — replay-idempotent."""

        return "|".join([effect_type, fact_key or "", obligation_key or "", holder_key or ""])


class TimelineBranch(Base, TimestampMixin):
    """One narrative timeline branch (dream, flashback, alternate), with a lifecycle.

    ``timeline_scope_key`` strings previously proliferated with no record of
    what each branch was, where it forked, or whether it ever ended
    (OPEN_ISSUES 2.3). This row is the branch's identity and lifecycle:
    ACTIVE accepts state writes; MERGED recorded a declared write-back
    manifest; RETIRED and ABANDONED refuse new writes but keep history
    readable. Rows are never physically deleted while any
    CharacterStateVersion, head, delta or transition still references the
    scope — those rows are the audit trail the branch anchors.
    """

    __tablename__ = "timeline_branches"
    __table_args__ = (
        UniqueConstraint("project_id", "scope_key", name="uq_timeline_branch_scope"),
        CheckConstraint(
            "branch_kind IN ('MAIN', 'DREAM', 'FLASHBACK', 'FLASH_FORWARD', 'ALTERNATE')",
            name="ck_timeline_branch_kind",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'MERGED', 'RETIRED', 'ABANDONED')",
            name="ck_timeline_branch_status",
        ),
        CheckConstraint(
            "branch_kind = 'MAIN' OR parent_scope_key IS NOT NULL",
            name="ck_timeline_branch_parent_required",
        ),
        Index("ix_timeline_branch_status", "project_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scope_key: Mapped[str] = mapped_column(String(120), nullable=False)
    branch_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    parent_scope_key: Mapped[str | None] = mapped_column(String(120))
    fork_shot_id: Mapped[str | None] = mapped_column(
        ForeignKey("shots.id", ondelete="SET NULL"), index=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    merged_by: Mapped[str | None] = mapped_column(String(120))
    merge_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    merge_manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retire_reason: Mapped[str | None] = mapped_column(String(500))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class CharacterEvidenceSubmission(Base, TimestampMixin):
    """Durable lifecycle of one shadow Character Evidence job, one per candidate.

    The unique candidate key is the idempotency guarantee: however many sweeps,
    replays or process restarts occur, at most one remote GPU job is dispatched
    per candidate. Status is explicit — a 202 acceptance is ACCEPTED, never
    evidence; a signed callback moves it to REPORTED or FAILED; an acceptance
    that never calls back past its deadline becomes RECONCILIATION_REQUIRED and
    waits for an operator. Shadow-only by check constraint: this table cannot
    express an operating mode that could gate a candidate.
    """

    __tablename__ = "character_evidence_submissions"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_character_evidence_submission_candidate"),
        CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'REPORTED', 'FAILED', 'SKIPPED', "
            "'RECONCILIATION_REQUIRED')",
            name="ck_character_evidence_submission_status",
        ),
        CheckConstraint(
            "operating_mode = 'SHADOW'",
            name="ck_character_evidence_submission_shadow_only",
        ),
        CheckConstraint(
            "submission_count >= 0",
            name="ck_character_evidence_submission_count",
        ),
        Index("ix_character_evidence_submission_status", "status", "updated_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("generation_candidates.id", ondelete="CASCADE"), nullable=False
    )
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id", ondelete="SET NULL"))
    character_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(40), default="PENDING", nullable=False)
    operating_mode: Mapped[str] = mapped_column(String(20), default="SHADOW", nullable=False)
    threshold_version: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    submission_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_callback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(String(500))
    skip_reason: Mapped[str | None] = mapped_column(String(240))
    reconciliation_note: Mapped[str | None] = mapped_column(Text)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_by: Mapped[str | None] = mapped_column(String(120))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class CharacterStateVersion(Base, TimestampMixin):
    """Immutable, fully materialized narrative state for one timeline scope."""

    __tablename__ = "character_state_versions"
    __table_args__ = (
        UniqueConstraint(
            "character_id",
            "timeline_scope_key",
            "version",
            name="uq_character_state_version_scope_number",
        ),
        UniqueConstraint("id", "character_id", name="uq_character_state_version_id_character"),
        CheckConstraint("version > 0", name="ck_character_state_version_positive"),
        CheckConstraint(
            "length(timeline_scope_key) > 0",
            name="ck_character_state_version_scope_nonempty",
        ),
        CheckConstraint(
            "length(identity_fingerprint) = 64 AND length(state_hash) = 64",
            name="ck_character_state_version_hash_lengths",
        ),
        CheckConstraint(
            "(previous_state_version_id IS NULL AND previous_state_hash IS NULL) OR "
            "(previous_state_version_id IS NOT NULL AND previous_state_hash IS NOT NULL "
            "AND length(previous_state_hash) = 64)",
            name="ck_character_state_version_previous_hash",
        ),
        CheckConstraint(
            "previous_state_version_id IS NULL OR previous_state_version_id != id",
            name="ck_character_state_version_not_self_parent",
        ),
        CheckConstraint(
            "(source_shot_id IS NULL AND source_candidate_id IS NULL) OR "
            "(source_shot_id IS NOT NULL AND source_candidate_id IS NOT NULL)",
            name="ck_character_state_version_source_pair",
        ),
        ForeignKeyConstraint(
            ["character_id", "project_id"],
            ["characters.id", "characters.project_id"],
            name="fk_character_state_version_character_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["previous_state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_version_previous_character",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["identity_version_id", "character_id"],
            ["character_identity_versions.id", "character_identity_versions.character_id"],
            name="fk_character_state_version_identity_character",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_character_state_version_scope",
            "project_id",
            "character_id",
            "timeline_scope_key",
            "version",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    character_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    timeline_scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_state_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    identity_version_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    source_shot_id: Mapped[str | None] = mapped_column(
        ForeignKey("shots.id", ondelete="RESTRICT"), index=True
    )
    source_candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_candidates.id", ondelete="RESTRICT"), index=True
    )
    state_schema_version: Mapped[str] = mapped_column(
        String(80), default="character-state-v1", nullable=False
    )
    narrative_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    identity_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_state_hash: Mapped[str | None] = mapped_column(String(64))
    state_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)


class CharacterStateDelta(Base, TimestampMixin):
    """Immutable proposal; superseding a proposal always creates a new row."""

    __tablename__ = "character_state_deltas"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_character_state_delta_project_key"),
        UniqueConstraint(
            "candidate_id",
            "character_id",
            "proposal_revision",
            name="uq_character_state_delta_candidate_revision",
        ),
        CheckConstraint("proposal_revision > 0", name="ck_character_state_delta_revision_positive"),
        CheckConstraint("target_version > 0", name="ck_character_state_delta_target_positive"),
        CheckConstraint("length(timeline_scope_key) > 0", name="ck_character_state_delta_scope_nonempty"),
        CheckConstraint("patch_format = 'JSON_PATCH_V1'", name="ck_character_state_delta_patch_format"),
        CheckConstraint(
            "proposal_kind IN ('INITIALIZE', 'NARRATIVE', 'EVIDENCE_DERIVED', 'IDENTITY_REBASE')",
            name="ck_character_state_delta_proposal_kind",
        ),
        CheckConstraint(
            "source_kind IN ('RULES', 'LLM', 'HUMAN', 'VISUAL_EVIDENCE')",
            name="ck_character_state_delta_source_kind",
        ),
        CheckConstraint(
            "length(target_state_hash) = 64 AND length(input_timeline_state_hash) = 64 "
            "AND length(planned_output_timeline_state_hash) = 64",
            name="ck_character_state_delta_hash_lengths",
        ),
        CheckConstraint(
            "(proposal_kind = 'INITIALIZE' AND base_state_version_id IS NULL "
            "AND base_state_hash IS NULL) OR "
            "(proposal_kind != 'INITIALIZE' AND base_state_version_id IS NOT NULL "
            "AND base_state_hash IS NOT NULL AND length(base_state_hash) = 64)",
            name="ck_character_state_delta_base_contract",
        ),
        CheckConstraint(
            "supersedes_delta_id IS NULL OR supersedes_delta_id != id",
            name="ck_character_state_delta_not_self_supersede",
        ),
        CheckConstraint(
            "source_kind NOT IN ('LLM', 'VISUAL_EVIDENCE') OR model_execution_record_id IS NOT NULL",
            name="ck_character_state_delta_model_provenance",
        ),
        CheckConstraint(
            "source_kind != 'HUMAN' OR proposed_by_user_id IS NOT NULL",
            name="ck_character_state_delta_human_provenance",
        ),
        ForeignKeyConstraint(
            ["character_id", "project_id"],
            ["characters.id", "characters.project_id"],
            name="fk_character_state_delta_character_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["base_state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_delta_base_character",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["identity_version_id", "character_id"],
            ["character_identity_versions.id", "character_identity_versions.character_id"],
            name="fk_character_state_delta_identity_character",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_character_state_delta_scope",
            "project_id",
            "character_id",
            "timeline_scope_key",
            "created_at",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    character_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    timeline_scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    shot_id: Mapped[str] = mapped_column(
        ForeignKey("shots.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("generation_candidates.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    base_state_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    identity_version_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    input_timeline_state_id: Mapped[str] = mapped_column(
        ForeignKey("timeline_states.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    planned_output_timeline_state_id: Mapped[str] = mapped_column(
        ForeignKey("timeline_states.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    proposal_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    supersedes_delta_id: Mapped[str | None] = mapped_column(
        ForeignKey("character_state_deltas.id", ondelete="RESTRICT"), index=True
    )
    proposal_kind: Mapped[str] = mapped_column(
        String(40), default=CharacterStateProposalKind.NARRATIVE.value, nullable=False
    )
    source_kind: Mapped[str] = mapped_column(
        String(40), default=CharacterStateProposalSource.RULES.value, nullable=False
    )
    patch_format: Mapped[str] = mapped_column(String(40), default="JSON_PATCH_V1", nullable=False)
    patch_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    changed_paths_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    proposed_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    base_state_hash: Mapped[str | None] = mapped_column(String(64))
    target_state_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    input_timeline_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    planned_output_timeline_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state_schema_version: Mapped[str] = mapped_column(
        String(80), default="character-state-v1", nullable=False
    )
    policy_version: Mapped[str] = mapped_column(
        String(80), default="character-state-policy-v1", nullable=False
    )
    model_execution_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_execution_records.id", ondelete="RESTRICT"), index=True
    )
    proposed_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)


class CharacterStateValidation(Base, TimestampMixin):
    """Immutable policy, visual, or human evidence about one proposed delta."""

    __tablename__ = "character_state_validations"
    __table_args__ = (
        UniqueConstraint(
            "state_delta_id",
            "stage",
            "attempt",
            name="uq_character_state_validation_stage_attempt",
        ),
        CheckConstraint("attempt > 0", name="ck_character_state_validation_attempt_positive"),
        CheckConstraint(
            "stage IN ('POLICY', 'VISUAL', 'HUMAN_OVERRIDE')",
            name="ck_character_state_validation_stage",
        ),
        CheckConstraint(
            "decision IN ('PASS', 'REJECT', 'REVIEW_REQUIRED')",
            name="ck_character_state_validation_decision",
        ),
        CheckConstraint(
            "validator_kind IN ('RULE_ENGINE', 'VLM', 'HUMAN')",
            name="ck_character_state_validation_validator",
        ),
        CheckConstraint(
            "length(validated_target_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_character_state_validation_hash_lengths",
        ),
        CheckConstraint(
            "validator_kind != 'VLM' OR model_execution_record_id IS NOT NULL",
            name="ck_character_state_validation_model_provenance",
        ),
        CheckConstraint(
            "validator_kind != 'HUMAN' OR validated_by_user_id IS NOT NULL",
            name="ck_character_state_validation_human_provenance",
        ),
        CheckConstraint(
            "stage != 'POLICY' OR validator_kind = 'RULE_ENGINE'",
            name="ck_character_state_validation_policy_rules",
        ),
        CheckConstraint(
            "stage != 'HUMAN_OVERRIDE' OR validator_kind = 'HUMAN'",
            name="ck_character_state_validation_override_human",
        ),
        Index("ix_character_state_validation_delta", "state_delta_id", "stage", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    state_delta_id: Mapped[str] = mapped_column(
        ForeignKey("character_state_deltas.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    decision: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    validator_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    model_execution_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_execution_records.id", ondelete="RESTRICT"), index=True
    )
    qa_result_id: Mapped[str | None] = mapped_column(
        ForeignKey("qa_results.id", ondelete="RESTRICT"), index=True
    )
    evidence_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), index=True
    )
    validated_target_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    violations_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    policy_version: Mapped[str] = mapped_column(
        String(80), default="character-state-policy-v1", nullable=False
    )
    validated_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )


class CharacterStateCommit(Base, TimestampMixin):
    """Append-only adoption record connecting validated evidence to a new state."""

    __tablename__ = "character_state_commits"
    __table_args__ = (
        UniqueConstraint("state_delta_id", name="uq_character_state_commit_delta"),
        UniqueConstraint("to_state_version_id", name="uq_character_state_commit_to_version"),
        UniqueConstraint(
            "candidate_id", "character_id", name="uq_character_state_commit_candidate_character"
        ),
        CheckConstraint("expected_head_version >= 0", name="ck_character_state_commit_head_nonnegative"),
        CheckConstraint(
            "from_state_version_id IS NULL OR from_state_version_id != to_state_version_id",
            name="ck_character_state_commit_distinct_versions",
        ),
        CheckConstraint("length(commit_hash) = 64", name="ck_character_state_commit_hash_length"),
        CheckConstraint("length(trim(reason)) > 0", name="ck_character_state_commit_reason_nonempty"),
        CheckConstraint(
            "commit_actor IN ('SYSTEM', 'HUMAN')",
            name="ck_character_state_commit_actor",
        ),
        CheckConstraint(
            "commit_actor != 'HUMAN' OR committed_by_user_id IS NOT NULL",
            name="ck_character_state_commit_human_provenance",
        ),
        ForeignKeyConstraint(
            ["character_id", "project_id"],
            ["characters.id", "characters.project_id"],
            name="fk_character_state_commit_character_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["from_state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_commit_from_character",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["to_state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_commit_to_character",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_character_state_commit_scope",
            "project_id",
            "character_id",
            "timeline_scope_key",
            "created_at",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    character_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    timeline_scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    shot_id: Mapped[str] = mapped_column(
        ForeignKey("shots.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("generation_candidates.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    state_delta_id: Mapped[str] = mapped_column(
        ForeignKey("character_state_deltas.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    from_state_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    to_state_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    policy_validation_id: Mapped[str] = mapped_column(
        ForeignKey("character_state_validations.id", ondelete="RESTRICT"), nullable=False
    )
    visual_validation_id: Mapped[str] = mapped_column(
        ForeignKey("character_state_validations.id", ondelete="RESTRICT"), nullable=False
    )
    human_validation_id: Mapped[str | None] = mapped_column(
        ForeignKey("character_state_validations.id", ondelete="RESTRICT")
    )
    expected_head_version: Mapped[int] = mapped_column(Integer, nullable=False)
    commit_actor: Mapped[str] = mapped_column(
        String(40), default=CharacterStateCommitActor.SYSTEM.value, nullable=False
    )
    committed_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    commit_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class CharacterStateHead(Base, TimestampMixin):
    """Mutable CAS projection; version and commit rows remain authoritative."""

    __tablename__ = "character_state_heads"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "character_id",
            "timeline_scope_key",
            name="uq_character_state_head_scope",
        ),
        CheckConstraint("lock_version > 0", name="ck_character_state_head_version_positive"),
        CheckConstraint("length(timeline_scope_key) > 0", name="ck_character_state_head_scope_nonempty"),
        ForeignKeyConstraint(
            ["character_id", "project_id"],
            ["characters.id", "characters.project_id"],
            name="fk_character_state_head_character_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_head_version_character",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_character_state_head_scope",
            "project_id",
            "character_id",
            "timeline_scope_key",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    character_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    timeline_scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    state_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False)


class Location(Base, TimestampMixin):
    __tablename__ = "locations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    canonical_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class Prop(Base, TimestampMixin):
    __tablename__ = "props"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    canonical_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class GenerationCandidate(Base, TimestampMixin):
    __tablename__ = "generation_candidates"
    __table_args__ = (UniqueConstraint("shot_id", "attempt_number", name="uq_candidate_attempt"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shot_id: Mapped[str] = mapped_column(ForeignKey("shots.id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    generation_job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    output_asset_id: Mapped[str | None] = mapped_column(String(36), index=True)
    qa_result_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(
        String(40), default=CandidateStatus.CREATED.value, index=True, nullable=False
    )
    accepted_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class MediaAsset(Base, TimestampMixin):
    __tablename__ = "media_assets"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "sha256",
            "asset_type",
            "lineage_key",
            name="uq_media_asset_lineage_hash",
        ),
        CheckConstraint(
            "verification_status IN ('READY', 'PENDING_VERIFICATION', 'VERIFYING', "
            "'INVALID', 'QUARANTINED')",
            name="ck_media_asset_verification_status",
        ),
        Index("ix_asset_provider_media", "provider", "provider_media_id"),
        Index("ix_media_asset_verification", "verification_status", "verification_claimed_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    asset_type: Mapped[str] = mapped_column(String(50), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    lineage_key: Mapped[str] = mapped_column(String(500), default="shared", nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    local_path: Mapped[str | None] = mapped_column(String(1000))
    public_url: Mapped[str | None] = mapped_column(String(2000))
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Float)
    provider: Mapped[str | None] = mapped_column(String(80))
    provider_media_id: Mapped[str | None] = mapped_column(String(500))
    character_id: Mapped[str | None] = mapped_column(String(36))
    scene_id: Mapped[str | None] = mapped_column(ForeignKey("scenes.id"))
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"))
    parent_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), index=True)
    generation_candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_candidates.id"), index=True
    )
    # Content verification. Paths that validate full bytes inline (multipart
    # upload, downloaded provider output) register READY; a direct upload is
    # adopted from a HEAD plus a 64 KB header and registers
    # PENDING_VERIFICATION, is claimed to VERIFYING by the async verifier
    # (leased, so a crashed worker's claim lapses and the row re-verifies),
    # and only a full decode promotes it to READY. INVALID is a file that
    # does not decode; QUARANTINED is one whose bytes contradict what was
    # declared (forged MIME, SHA mismatch). Providers and build chains may
    # only consume READY assets.
    verification_status: Mapped[str] = mapped_column(
        String(30), default="READY", server_default="READY", nullable=False
    )
    verification_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_error: Mapped[str | None] = mapped_column(String(500))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class DirectUploadStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


class DirectUpload(Base, TimestampMixin):
    """One authorized direct-to-storage upload, between presign and completion.

    The client PUTs bytes straight to object storage, so this row is where the
    server's decisions live in the meantime: which project and asset type were
    authorized, which key was chosen, which digest the store will enforce, and
    which quota reservation is being held. The client sends back only this row's
    id, so it cannot promote its upload to a different project, a different
    asset type or a different key between the two calls.
    """

    __tablename__ = "direct_uploads"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_direct_upload_idempotency"),
        Index("ix_direct_upload_expiry", "status", "expires_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(36), index=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(50), nullable=False)
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    declared_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    lineage_key: Mapped[str] = mapped_column(String(500), default="shared", nullable=False)
    shot_id: Mapped[str | None] = mapped_column(String(36))
    character_id: Mapped[str | None] = mapped_column(String(36))
    storage_reservation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(
        String(40), default=DirectUploadStatus.PENDING.value, index=True, nullable=False
    )
    media_asset_id: Mapped[str | None] = mapped_column(String(36), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class MediaRenditionKind(StrEnum):
    """What one stored encoding of a media asset is *for*.

    The original is the user's bytes and is never replaced or re-encoded — a
    character face, a product label or a fabric weave only survives at the
    resolution it arrived at. Everything else is a derived copy created because
    some consumer cannot accept the original, and a derived copy is disposable.
    """

    ORIGINAL = "ORIGINAL"
    PROVIDER_REFERENCE = "PROVIDER_REFERENCE"
    THUMBNAIL = "THUMBNAIL"


class MediaRendition(Base, TimestampMixin):
    """One stored encoding of a media asset, derived from its original.

    A rendition never stands alone: it records the constraint profile that
    caused it to exist, so a provider whose limits later change gets a new
    rendition instead of silently reusing one built for different limits.
    """

    __tablename__ = "media_renditions"
    __table_args__ = (
        UniqueConstraint(
            "media_asset_id",
            "kind",
            "constraint_key",
            name="uq_media_rendition_scope",
        ),
        CheckConstraint(
            "lifecycle_status IN ('ACTIVE', 'GC_CLAIMED', 'DELETED')",
            name="ck_media_rendition_lifecycle",
        ),
        Index("ix_media_rendition_asset", "media_asset_id", "kind"),
        Index("ix_media_rendition_gc", "lifecycle_status", "last_accessed_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    media_asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    # The bounds this encoding was built to satisfy. "original" for the source
    # row; otherwise a stable digest of the consumer's declared constraints.
    constraint_key: Mapped[str] = mapped_column(String(200), default="original", nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    local_path: Mapped[str | None] = mapped_column(String(1000))
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    # Garbage-collection lifecycle. A derived copy is disposable cache; these
    # columns make its disposal observable and safe: ACTIVE rows serve, a
    # sweeper claims a row under a lease before touching storage (so two
    # workers cannot double-delete), and DELETED rows remain as tombstones
    # recording what was removed — reconcilable, and revivable in place when
    # the same constraints are needed again. Originals are never collected.
    lifecycle_status: Mapped[str] = mapped_column(
        String(20), default="ACTIVE", server_default="ACTIVE", nullable=False
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gc_claim_id: Mapped[str | None] = mapped_column(String(36))
    gc_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_reason: Mapped[str | None] = mapped_column(String(240))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class Asset(Base, TimestampMixin):
    """A logical, versioned production asset rather than a single media file."""

    __tablename__ = "assets"
    __table_args__ = (Index("ix_assets_project_kind_status", "project_id", "asset_type", "status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    asset_type: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    canonical_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    canonical_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", index=True, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)


class AssetVersion(Base, TimestampMixin):
    """Immutable version payload; becoming canonical always requires an explicit promotion."""

    __tablename__ = "asset_versions"
    __table_args__ = (UniqueConstraint("asset_id", "version", name="uq_asset_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    primary_media_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), index=True
    )
    parent_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("asset_versions.id", ondelete="SET NULL"), index=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    continuity_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    embedding_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="USER_UPLOAD", index=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=AssetVersionStatus.READY.value, index=True, nullable=False
    )
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)


class AssetVersionMedia(Base, TimestampMixin):
    """Role-labelled media belonging to a logical asset version."""

    __tablename__ = "asset_version_media"
    __table_args__ = (
        UniqueConstraint("asset_version_id", "media_asset_id", "role", name="uq_asset_version_media_role"),
        Index("ix_asset_version_media_version_role", "asset_version_id", "role"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_version_id: Mapped[str] = mapped_column(
        ForeignKey("asset_versions.id", ondelete="CASCADE"), index=True
    )
    media_asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), index=True
    )
    role: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class AssetCanonicalPromotion(Base, TimestampMixin):
    """Append-only audit record for an explicit canonical-version change."""

    __tablename__ = "asset_canonical_promotions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    from_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("asset_versions.id", ondelete="SET NULL"), index=True
    )
    to_version_id: Mapped[str] = mapped_column(
        ForeignKey("asset_versions.id", ondelete="RESTRICT"), index=True
    )
    promoted_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


def _install_asset_registry_integrity_ddl() -> None:
    """Install DB-side immutability guards for both create_all and migrations.

    Production databases receive equivalent DDL from migration 0008. These table
    events keep ephemeral ``Base.metadata.create_all`` databases honest as well.
    Physical deletion of a logical asset is intentionally rejected while it owns
    versions or promotion history; callers must archive assets instead.
    """

    anchor = AssetCanonicalPromotion.__table__
    sqlite_statements = (
        """CREATE TRIGGER IF NOT EXISTS trg_assets_canonical_same_asset_insert
        BEFORE INSERT ON assets WHEN NEW.canonical_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM asset_versions
            WHERE id = NEW.canonical_version_id AND asset_id = NEW.id
        ) BEGIN SELECT RAISE(ABORT, 'canonical version must belong to the same asset'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_assets_canonical_same_asset_update
        BEFORE UPDATE OF id, canonical_version_id ON assets
        WHEN NEW.canonical_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM asset_versions
            WHERE id = NEW.canonical_version_id AND asset_id = NEW.id
        ) BEGIN SELECT RAISE(ABORT, 'canonical version must belong to the same asset'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_assets_canonical_requires_promotion_update
        BEFORE UPDATE OF canonical_version_id ON assets
        WHEN NOT (NEW.canonical_version_id IS OLD.canonical_version_id) AND (
            NEW.canonical_version_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM asset_canonical_promotions
                WHERE asset_id = NEW.id
                  AND to_version_id = NEW.canonical_version_id
                  AND from_version_id IS OLD.canonical_version_id
                  AND created_at >= OLD.updated_at
            )
        ) BEGIN SELECT RAISE(ABORT, 'canonical change requires a fresh promotion record'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_asset_versions_parent_same_asset_insert
        BEFORE INSERT ON asset_versions WHEN NEW.parent_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM asset_versions
            WHERE id = NEW.parent_version_id AND asset_id = NEW.asset_id
        ) BEGIN SELECT RAISE(ABORT, 'parent version must belong to the same asset'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_asset_promotions_versions_same_asset_insert
        BEFORE INSERT ON asset_canonical_promotions WHEN NOT EXISTS (
            SELECT 1 FROM asset_versions WHERE id = NEW.to_version_id AND asset_id = NEW.asset_id
        ) OR (NEW.from_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM asset_versions WHERE id = NEW.from_version_id AND asset_id = NEW.asset_id
        )) BEGIN SELECT RAISE(ABORT, 'promotion versions must belong to the same asset'); END""",
    )
    for statement in sqlite_statements:
        event.listen(anchor, "after_create", DDL(statement).execute_if(dialect="sqlite"))

    protected_table_names = (
        "asset_versions",
        "asset_version_media",
        "asset_canonical_promotions",
    )
    for table_name in protected_table_names:
        for operation in ("UPDATE", "DELETE"):
            trigger_name = f"trg_{table_name}_append_only_{operation.lower()}"
            message = f"{table_name} is append-only"
            statement = (
                f"CREATE TRIGGER IF NOT EXISTS {trigger_name} BEFORE {operation} ON {table_name} "
                f"BEGIN SELECT RAISE(ABORT, '{message}'); END"
            )
            event.listen(anchor, "after_create", DDL(statement).execute_if(dialect="sqlite"))

    postgres_statements = (
        """CREATE OR REPLACE FUNCTION enforce_asset_registry_consistency()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME = 'assets' THEN
                IF NEW.canonical_version_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM asset_versions
                    WHERE id = NEW.canonical_version_id AND asset_id = NEW.id
                ) THEN RAISE EXCEPTION 'canonical version must belong to the same asset'
                    USING ERRCODE = '23514'; END IF;
                IF TG_OP = 'UPDATE'
                   AND NEW.canonical_version_id IS DISTINCT FROM OLD.canonical_version_id AND (
                    NEW.canonical_version_id IS NULL OR NOT EXISTS (
                        SELECT 1 FROM asset_canonical_promotions
                        WHERE asset_id = NEW.id
                          AND to_version_id = NEW.canonical_version_id
                          AND from_version_id IS NOT DISTINCT FROM OLD.canonical_version_id
                          AND created_at >= OLD.updated_at
                    )
                ) THEN RAISE EXCEPTION 'canonical change requires a fresh promotion record'
                    USING ERRCODE = '23514'; END IF;
            ELSIF TG_TABLE_NAME = 'asset_versions' THEN
                IF NEW.parent_version_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM asset_versions
                    WHERE id = NEW.parent_version_id AND asset_id = NEW.asset_id
                ) THEN RAISE EXCEPTION 'parent version must belong to the same asset'
                    USING ERRCODE = '23514'; END IF;
            ELSIF TG_TABLE_NAME = 'asset_canonical_promotions' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM asset_versions
                    WHERE id = NEW.to_version_id AND asset_id = NEW.asset_id
                ) OR (NEW.from_version_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM asset_versions
                    WHERE id = NEW.from_version_id AND asset_id = NEW.asset_id
                )) THEN RAISE EXCEPTION 'promotion versions must belong to the same asset'
                    USING ERRCODE = '23514'; END IF;
            END IF;
            RETURN NEW;
        END; $$""",
        """CREATE OR REPLACE FUNCTION enforce_asset_registry_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '%% is append-only', TG_TABLE_NAME
            USING ERRCODE = '23000';
            RETURN OLD;
        END; $$""",
        """CREATE TRIGGER trg_assets_canonical_same_asset
        BEFORE INSERT OR UPDATE OF id, canonical_version_id ON assets FOR EACH ROW
        EXECUTE FUNCTION enforce_asset_registry_consistency()""",
        """CREATE TRIGGER trg_asset_versions_parent_same_asset
        BEFORE INSERT ON asset_versions FOR EACH ROW
        EXECUTE FUNCTION enforce_asset_registry_consistency()""",
        """CREATE TRIGGER trg_asset_promotions_versions_same_asset
        BEFORE INSERT ON asset_canonical_promotions FOR EACH ROW
        EXECUTE FUNCTION enforce_asset_registry_consistency()""",
    )
    for statement in postgres_statements:
        event.listen(anchor, "after_create", DDL(statement).execute_if(dialect="postgresql"))
    for table_name in protected_table_names:
        statement = (
            f"CREATE TRIGGER trg_{table_name}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table_name} FOR EACH ROW "
            "EXECUTE FUNCTION enforce_asset_registry_append_only()"
        )
        event.listen(anchor, "after_create", DDL(statement).execute_if(dialect="postgresql"))


_install_asset_registry_integrity_ddl()


def _install_shot_lineage_ddl() -> None:
    sqlite_check = """(
        NEW.previous_shot_id = NEW.id OR NOT EXISTS (
            SELECT 1
            FROM shots AS previous
            JOIN scenes AS previous_scene ON previous_scene.id = previous.scene_id
            JOIN episodes AS previous_episode ON previous_episode.id = previous_scene.episode_id
            JOIN scenes AS current_scene ON current_scene.id = NEW.scene_id
            JOIN episodes AS current_episode ON current_episode.id = current_scene.episode_id
            WHERE previous.id = NEW.previous_shot_id
              AND previous_episode.project_id = current_episode.project_id
        )
    )"""
    for operation, suffix in (("INSERT", "insert"), ("UPDATE OF scene_id, previous_shot_id", "update")):
        event.listen(
            Shot.__table__,
            "after_create",
            DDL(
                f"""CREATE TRIGGER IF NOT EXISTS trg_shots_previous_same_project_{suffix}
                BEFORE {operation} ON shots
                WHEN NEW.previous_shot_id IS NOT NULL AND {sqlite_check}
                BEGIN SELECT RAISE(ABORT, 'previous shot must belong to the same project'); END"""
            ).execute_if(dialect="sqlite"),
        )

    event.listen(
        Shot.__table__,
        "after_create",
        DDL(
            """CREATE OR REPLACE FUNCTION enforce_shot_previous_same_project()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.previous_shot_id IS NOT NULL AND (
                    NEW.previous_shot_id = NEW.id OR NOT EXISTS (
                        SELECT 1
                        FROM shots AS previous
                        JOIN scenes AS previous_scene ON previous_scene.id = previous.scene_id
                        JOIN episodes AS previous_episode ON previous_episode.id = previous_scene.episode_id
                        JOIN scenes AS current_scene ON current_scene.id = NEW.scene_id
                        JOIN episodes AS current_episode ON current_episode.id = current_scene.episode_id
                        WHERE previous.id = NEW.previous_shot_id
                          AND previous_episode.project_id = current_episode.project_id
                    )
                ) THEN
                    RAISE EXCEPTION 'previous shot must belong to the same project'
                    USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END; $$"""
        ).execute_if(dialect="postgresql"),
    )
    event.listen(
        Shot.__table__,
        "after_create",
        DDL(
            """CREATE TRIGGER trg_shots_previous_same_project
            BEFORE INSERT OR UPDATE OF scene_id, previous_shot_id ON shots
            FOR EACH ROW EXECUTE FUNCTION enforce_shot_previous_same_project()"""
        ).execute_if(dialect="postgresql"),
    )


_install_shot_lineage_ddl()


class ProviderCredential(Base, TimestampMixin):
    __tablename__ = "provider_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=CredentialStatus.ACTIVE.value, index=True, nullable=False
    )
    status_reason: Mapped[str | None] = mapped_column(String(240))
    redacted_fingerprint: Mapped[str | None] = mapped_column(String(80))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ProviderAccount(Base, TimestampMixin):
    __tablename__ = "provider_accounts"
    __table_args__ = (UniqueConstraint("provider", "account_identifier", name="uq_provider_account"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    account_identifier: Mapped[str] = mapped_column(String(320), nullable=False)
    tier: Mapped[str] = mapped_column(String(50), default="FREE", nullable=False)
    credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=AccountStatus.READY.value, index=True, nullable=False
    )
    image_capacity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    video_capacity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    image_inflight: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    video_inflight: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credential_id: Mapped[str | None] = mapped_column(ForeignKey("provider_credentials.id"))
    proxy_id: Mapped[str | None] = mapped_column(String(36))
    worker_id: Mapped[str | None] = mapped_column(String(36), index=True)
    supported_models: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class BrowserWorker(Base, TimestampMixin):
    __tablename__ = "browser_workers"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("provider_accounts.id"), index=True)
    connection_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=WorkerStatus.READY.value, index=True, nullable=False
    )
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    credits: Mapped[int | None] = mapped_column(Integer)
    current_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_jobs: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class WorkerAccessCredential(Base, TimestampMixin):
    """Revocable, worker-scoped credential; only the token digest is persisted."""

    __tablename__ = "worker_access_credentials"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_worker_access_credentials_token_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worker_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkerSocketTicket(Base, TimestampMixin):
    """Short-lived, one-use WebSocket bootstrap ticket derived from a worker credential."""

    __tablename__ = "worker_socket_tickets"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_worker_socket_tickets_token_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    credential_id: Mapped[str] = mapped_column(
        ForeignKey("worker_access_credentials.id", ondelete="CASCADE"), index=True, nullable=False
    )
    worker_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class GenerationJob(Base, TimestampMixin):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        CheckConstraint("quoted_credits >= 0", name="ck_generation_job_quoted_credits"),
        CheckConstraint(
            "provider != 'google_flow' OR provider_job_id IS NULL "
            "OR status IN ('COMPLETED', 'CANCELLED', 'FAILED') "
            "OR (account_id IS NOT NULL AND provider_project_id IS NOT NULL)",
            name="ck_generation_flow_poll_identity",
        ),
        Index(
            "uq_generation_flow_poll_identity",
            "provider",
            "account_id",
            "provider_project_id",
            "provider_job_id",
            unique=True,
            sqlite_where=text("provider = 'google_flow' AND provider_job_id IS NOT NULL"),
            postgresql_where=text("provider = 'google_flow' AND provider_job_id IS NOT NULL"),
        ),
        # The Productions listing: a project's live creations, newest first.
        # Partial, so a project whose history is mostly deleted still reads
        # its remaining creations out of an index the size of what is left.
        Index(
            "ix_generation_jobs_project_live",
            "project_id",
            "created_at",
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("generation_candidates.id"), index=True)
    generation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default=JobStatus.NEW.value, index=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    provider_request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy: Mapped[str] = mapped_column(String(60), default=GenerationPolicy.TEXT_TO_VIDEO.value)
    provider_job_id: Mapped[str | None] = mapped_column(String(500), index=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("provider_accounts.id"), index=True)
    provider_project_id: Mapped[str | None] = mapped_column(String(500), index=True)
    worker_id: Mapped[str | None] = mapped_column(ForeignKey("browser_workers.id"), index=True)
    output_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    retry_category: Mapped[str | None] = mapped_column(String(60))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submission_state: Mapped[str] = mapped_column(String(40), default="NOT_SENT", nullable=False)
    safe_to_retry: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(64), index=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    reservation_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cost_estimate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_cost: Mapped[float | None] = mapped_column(Float)
    workspace_credit_required: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    quoted_credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # Removal from the user's project is a soft delete, always. The row is the
    # anchor every financial and evidential record points at — credit ledger
    # entries, reservation settlements, provider execution records, cost rows,
    # billing evidence and the audit log all carry its id — so erasing it
    # would either orphan or destroy paid history. Setting these two columns
    # takes the creation out of every user-facing surface while leaving that
    # history exactly as it was written.
    #
    # Nothing ever clears them: a provider completion that lands after the
    # deletion still writes its status and output, and the creation stays
    # gone, which is what keeps a cancelled-then-finished job from
    # reappearing in Productions.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    #: The user who removed it. Deliberately not a foreign key: this column is
    #: added to a table that already exists, and SQLite can only acquire a new
    #: foreign key by rebuilding the whole table — a rebuild this one, with its
    #: partial indexes and check constraints, does not deserve for an
    #: attribution field. It is written from the authenticated principal and is
    #: null only for the development bypass, which has no user row.
    deleted_by: Mapped[str | None] = mapped_column(String(36), index=True)


class CreationMediaCleanup(Base, TimestampMixin):
    """Work queue for reclaiming the media of a deleted creation.

    Object storage lives outside the database transaction, so it cannot join
    it: a bucket call inside the deletion would either hold the transaction
    open across a network round trip or, worse, roll the deletion back when
    the bucket is briefly unreachable. The deletion therefore commits with a
    row here, and a sweeper does the storage work afterwards — retried under
    a backoff until the object is genuinely gone.

    The queue points at the *creation*, not at an asset id captured at
    deletion time, so a provider result that lands after the deletion is
    still collected: the sweep re-reads the creation's current output when it
    runs. An asset anything else references is never deleted; the row is
    closed as ``KEPT_SHARED`` instead.
    """

    __tablename__ = "creation_media_cleanups"
    __table_args__ = (
        UniqueConstraint("generation_job_id", name="uq_creation_media_cleanup_job"),
        CheckConstraint(
            "status IN ('PENDING', 'CLAIMED', 'DONE', 'KEPT_SHARED', 'FAILED')",
            name="ck_creation_media_cleanup_status",
        ),
        Index("ix_creation_media_cleanup_due", "status", "next_attempt_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Resolved when the sweep runs, not when the creation is deleted.
    media_asset_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(
        String(20), default="PENDING", server_default="PENDING", nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    claim_id: Mapped[str | None] = mapped_column(String(36))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class GenerationIdempotency(Base, TimestampMixin):
    __tablename__ = "generation_idempotency"
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_generation_idempotency_project_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(250), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.id"), nullable=False)
    provider_job_id: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(40), default="PROCESSING", nullable=False)
    result_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))


class ProviderSynchronousResult(Base, TimestampMixin):
    """A synchronous provider's finished result, held until the poll consumes it.

    A synchronous image API answers with the artefact in the response body:
    there is no remote job to re-read and no URL to fetch. The Gateway is
    submit-then-poll, so the result has to survive the gap between the
    confirmed submission and the poll that completes it. Holding it in the
    worker process meant process death in that window lost an artefact the
    workspace had already been billed for — recoverable only as
    ``RECONCILIATION_REQUIRED``, never as a refund or a silent success.

    Written in the same transaction that confirms the submission, so it exists
    for exactly the outcomes the confirmation exists for, and deleted by the
    completion that consumes it. ``provider_job_id`` and ``attempt_number``
    are what make a stale row unusable: a result belongs to the submission
    that produced it, never to a later attempt.
    """

    __tablename__ = "provider_synchronous_results"
    __table_args__ = (
        UniqueConstraint("generation_job_id", name="uq_provider_sync_result_job"),
        CheckConstraint("attempt_number >= 1", name="ck_provider_sync_result_attempt"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider_job_id: Mapped[str] = mapped_column(String(500), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    output_url: Mapped[str | None] = mapped_column(Text)
    output_mime_type: Mapped[str | None] = mapped_column(String(120))
    error: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ProviderSynchronousResultOutput(Base):
    """One inline artefact of a held synchronous result, in provider order.

    ``ordinal`` 0 is the job's own output asset; the rest are the extra images
    of a batch request, which are registered as project media rather than
    discarded because the workspace paid for them.
    """

    __tablename__ = "provider_synchronous_result_outputs"
    __table_args__ = (
        UniqueConstraint("result_id", "ordinal", name="uq_provider_sync_output_ordinal"),
        CheckConstraint("ordinal >= 0", name="ck_provider_sync_output_ordinal"),
        CheckConstraint("length(content_sha256) = 64", name="ck_provider_sync_output_digest"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    result_id: Mapped[str] = mapped_column(
        ForeignKey("provider_synchronous_results.id", ondelete="CASCADE"), index=True, nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class GenerationEvent(Base):
    __tablename__ = "generation_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class WorkerCommand(Base, TimestampMixin):
    __tablename__ = "worker_commands"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worker_id: Mapped[str] = mapped_column(ForeignKey("browser_workers.id", ondelete="CASCADE"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    message_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True, nullable=False)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_connection_id: Mapped[str | None] = mapped_column(String(100), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaProviderBinding(Base, TimestampMixin):
    __tablename__ = "media_provider_bindings"
    __table_args__ = (
        UniqueConstraint("asset_id", "provider", "account_id", name="uq_asset_provider_account"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("media_assets.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey("provider_accounts.id"), index=True)
    provider_media_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="READY", nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upload_claim_token: Mapped[str | None] = mapped_column(String(36), index=True)
    upload_claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    upload_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderProjectBinding(Base, TimestampMixin):
    __tablename__ = "provider_projects"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PROVISIONING', 'READY', 'DEGRADED', 'MIGRATION_REQUIRED', "
            "'MIGRATING', 'DISABLED', 'FAILED')",
            name="ck_provider_project_status",
        ),
        CheckConstraint("version > 0", name="ck_provider_project_version"),
        CheckConstraint(
            "status IN ('PROVISIONING', 'MIGRATION_REQUIRED', 'FAILED') OR provider_project_id IS NOT NULL",
            name="ck_provider_project_remote_id",
        ),
        Index(
            "uq_flow_active_local_project",
            "local_project_id",
            "provider",
            unique=True,
            sqlite_where=text(
                "provider = 'google_flow' AND status IN "
                "('PROVISIONING', 'READY', 'DEGRADED', 'MIGRATION_REQUIRED', 'MIGRATING')"
            ),
            postgresql_where=text(
                "provider = 'google_flow' AND status IN "
                "('PROVISIONING', 'READY', 'DEGRADED', 'MIGRATION_REQUIRED', 'MIGRATING')"
            ),
        ),
        Index(
            "uq_non_flow_provider_project_account",
            "local_project_id",
            "provider",
            "provider_account_id",
            unique=True,
            sqlite_where=text("provider != 'google_flow'"),
            postgresql_where=text("provider != 'google_flow'"),
        ),
        Index(
            "uq_flow_remote_project_owner",
            "provider",
            "provider_project_id",
            unique=True,
            sqlite_where=text("provider = 'google_flow' AND provider_project_id IS NOT NULL"),
            postgresql_where=text("provider = 'google_flow' AND provider_project_id IS NOT NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    local_project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    provider_account_id: Mapped[str] = mapped_column(ForeignKey("provider_accounts.id"), index=True)
    provider_project_id: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        String(40), default=ProviderProjectBindingStatus.READY.value, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    status_reason: Mapped[str | None] = mapped_column(String(240))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    migration_required_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provisioning_token: Mapped[str | None] = mapped_column(String(64), index=True)
    provisioning_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class FlowMigrationPlan(Base, TimestampMixin):
    __tablename__ = "flow_migration_plans"
    __table_args__ = (
        CheckConstraint(
            "migration_status IN ('PLANNED', 'USER_REVIEW_REQUIRED', 'APPROVED', "
            "'MIGRATING', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name="ck_flow_migration_status",
        ),
        CheckConstraint(
            "verification_status IN ('PENDING', 'USER_REVIEW_REQUIRED', 'VERIFIED', 'FAILED')",
            name="ck_flow_migration_verification",
        ),
        Index(
            "uq_flow_migration_active_binding",
            "source_binding_id",
            unique=True,
            sqlite_where=text(
                "migration_status IN ('PLANNED', 'USER_REVIEW_REQUIRED', 'APPROVED', 'MIGRATING')"
            ),
            postgresql_where=text(
                "migration_status IN ('PLANNED', 'USER_REVIEW_REQUIRED', 'APPROVED', 'MIGRATING')"
            ),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_binding_id: Mapped[str] = mapped_column(
        ForeignKey("provider_projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    local_project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_account_id: Mapped[str] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    target_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="RESTRICT"), index=True
    )
    source_project_id: Mapped[str | None] = mapped_column(String(500))
    target_project_id: Mapped[str | None] = mapped_column(String(500))
    characters_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    instructions_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    assets_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    migration_status: Mapped[str] = mapped_column(
        String(40), default=FlowMigrationStatus.USER_REVIEW_REQUIRED.value, index=True, nullable=False
    )
    verification_status: Mapped[str] = mapped_column(
        String(40),
        default=FlowMigrationVerificationStatus.USER_REVIEW_REQUIRED.value,
        nullable=False,
    )
    trigger_reason: Mapped[str] = mapped_column(String(240), nullable=False)


class ProviderCharacterBinding(Base, TimestampMixin):
    __tablename__ = "provider_character_bindings"
    __table_args__ = (
        UniqueConstraint(
            "character_identity_version_id", "provider", "provider_account_id", name="uq_provider_character"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    character_identity_version_id: Mapped[str] = mapped_column(
        ForeignKey("character_identity_versions.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    provider_account_id: Mapped[str] = mapped_column(ForeignKey("provider_accounts.id"), index=True)
    binding_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="READY", nullable=False)


class ProviderInstructionBinding(Base, TimestampMixin):
    __tablename__ = "provider_instruction_bindings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    provider_account_id: Mapped[str] = mapped_column(ForeignKey("provider_accounts.id"), index=True)
    instruction_name: Mapped[str] = mapped_column(String(240), nullable=False)
    provider_instruction_id: Mapped[str] = mapped_column(String(500), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="READY", nullable=False)


class QAResult(Base, TimestampMixin):
    __tablename__ = "qa_results"
    __table_args__ = (
        # One row per Character Evidence producer run and candidate: concurrent
        # signed callbacks replaying the same report must converge on a single
        # QAResult instead of inserting duplicates. NULL run ids (results not
        # produced by the async evidence path) never collide.
        Index(
            "uq_qa_result_candidate_producer_run",
            "candidate_id",
            "producer_run_id",
            unique=True,
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("generation_candidates.id", ondelete="CASCADE"), index=True
    )
    producer_run_id: Mapped[str | None] = mapped_column(String(64))
    profile: Mapped[str] = mapped_column(String(80), default="DIALOGUE", nullable=False)
    level_reached: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    decision: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    character_score: Mapped[float | None] = mapped_column(Float)
    scene_score: Mapped[float | None] = mapped_column(Float)
    composition_score: Mapped[float | None] = mapped_column(Float)
    action_score: Mapped[float | None] = mapped_column(Float)
    camera_score: Mapped[float | None] = mapped_column(Float)
    lighting_score: Mapped[float | None] = mapped_column(Float)
    narrative_score: Mapped[float | None] = mapped_column(Float)
    hard_failures: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)


class StyleEmbedding(Base, TimestampMixin):
    """Immutable visual-style descriptor bound to one immutable asset version."""

    __tablename__ = "style_embeddings"
    __table_args__ = (
        UniqueConstraint("asset_version_id", "model", name="uq_style_embedding_version_model"),
        CheckConstraint("dimension > 0", name="ck_style_embedding_dimension_positive"),
        CheckConstraint("length(embedding_hash) = 64", name="ck_style_embedding_hash_length"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    asset_version_id: Mapped[str] = mapped_column(
        ForeignKey("asset_versions.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    embedding: Mapped[list[float]] = mapped_column(JSON, default=list, nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(80), nullable=False)
    # The rest of this vector's space. `provider`, `model`, `algorithm_version`
    # and `dimension` above are the other half of it. A similarity score is only
    # meaningful inside one space, and cosine over two unrelated vectors returns
    # a plausible number rather than an error — so the space travels with the
    # vector and is compared before any score is taken.
    model_revision: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    normalization: Mapped[str] = mapped_column(String(40), default="L2", nullable=False)
    distance_metric: Mapped[str] = mapped_column(String(40), default="cosine", nullable=False)
    embedding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_media_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_media_hashes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ProjectStyleLock(Base, TimestampMixin):
    """Append-only confirmation that freezes one STYLE version for a project."""

    __tablename__ = "project_style_locks"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_project_style_lock_project"),
        CheckConstraint(
            "similarity_threshold >= 0 AND similarity_threshold <= 1",
            name="ck_style_lock_similarity_range",
        ),
        CheckConstraint(
            "minimum_similarity_threshold >= 0 AND minimum_similarity_threshold <= 1",
            name="ck_style_lock_minimum_range",
        ),
        CheckConstraint(
            "drift_limit >= 0 AND drift_limit <= 1",
            name="ck_style_lock_drift_range",
        ),
        CheckConstraint(
            "max_low_score_fraction >= 0 AND max_low_score_fraction <= 1",
            name="ck_style_lock_low_fraction_range",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    style_asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    style_version_id: Mapped[str] = mapped_column(
        ForeignKey("asset_versions.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    style_embedding_id: Mapped[str] = mapped_column(
        ForeignKey("style_embeddings.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    # The second style layer, bound to the lock so it stays immutable and
    # self-describing. Null means the project was locked before a semantic
    # embedder was configured; that project keeps the deterministic gate alone
    # rather than silently acquiring a second one whose reference was chosen
    # after the fact.
    # Declared as a plain identifier rather than a ForeignKey, matching
    # `GenerationCandidate.output_asset_id`. SQLite cannot add a foreign key to
    # an existing table without rebuilding it, and rebuilding this one breaks
    # the triggers that guard the style pointer by name. The service only ever
    # writes an embedding id it just created, and the row is append-only.
    semantic_style_embedding_id: Mapped[str | None] = mapped_column(String(36), index=True)
    semantic_similarity_threshold: Mapped[float] = mapped_column(Float, default=0.80, nullable=False)
    similarity_threshold: Mapped[float] = mapped_column(Float, default=0.72, nullable=False)
    minimum_similarity_threshold: Mapped[float] = mapped_column(Float, default=0.55, nullable=False)
    drift_limit: Mapped[float] = mapped_column(Float, default=0.06, nullable=False)
    max_low_score_fraction: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    locked_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class CandidateStyleEvaluation(Base, TimestampMixin):
    """Append-only style-similarity evidence used by the candidate commit gate."""

    __tablename__ = "candidate_style_evaluations"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_candidate_style_evaluation_candidate"),
        CheckConstraint(
            "status IN ('PASS', 'FAIL', 'REVIEW_REQUIRED')",
            name="ck_candidate_style_evaluation_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("generation_candidates.id", ondelete="CASCADE"), index=True, nullable=False
    )
    output_asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    style_lock_id: Mapped[str] = mapped_column(
        ForeignKey("project_style_locks.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    style_version_id: Mapped[str] = mapped_column(
        ForeignKey("asset_versions.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    style_embedding_id: Mapped[str] = mapped_column(
        ForeignKey("style_embeddings.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    # Layer 2's own verdict, kept separate so it is queryable and so a combined
    # status can never hide which layer objected.
    semantic_status: Mapped[str | None] = mapped_column(String(40), index=True)
    semantic_average_similarity: Mapped[float | None] = mapped_column(Float)
    semantic_minimum_similarity: Mapped[float | None] = mapped_column(Float)
    average_similarity: Mapped[float | None] = mapped_column(Float)
    minimum_similarity: Mapped[float | None] = mapped_column(Float)
    p10_similarity: Mapped[float | None] = mapped_column(Float)
    drift_slope: Mapped[float | None] = mapped_column(Float)
    low_score_fraction: Mapped[float | None] = mapped_column(Float)
    sample_positions: Mapped[list[float]] = mapped_column(JSON, default=list, nullable=False)
    sample_scores: Mapped[list[float]] = mapped_column(JSON, default=list, nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(80), nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class CostRecord(Base, TimestampMixin):
    __tablename__ = "cost_records"
    __table_args__ = (Index("ix_cost_records_generation_job_id", "generation_job_id", unique=True),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("generation_candidates.id"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"))
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    duration: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    resolution: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    credits: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_cost: Mapped[float | None] = mapped_column(Float)
    retry_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    wasted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class DecisionRecord(Base):
    __tablename__ = "decision_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    decision_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    input_features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    selected_action: Mapped[str] = mapped_column(String(160), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    model_version: Mapped[str] = mapped_column(String(80), default="rules-v1", nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), default="v1", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Skill(Base, TimestampMixin):
    __tablename__ = "skills"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    category: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)


class SkillVersion(Base, TimestampMixin):
    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("skill_id", "version", name="uq_skill_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"), index=True)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    compatible_tasks: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    compatible_models: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    dependencies: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)


class PromptCompilation(Base, TimestampMixin):
    __tablename__ = "prompt_compilations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    user_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    compiled_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(80), default="v1", nullable=False)
    skill_versions: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    diff_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class PromptRevision(Base, TimestampMixin):
    __tablename__ = "prompt_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    mode: Mapped[str] = mapped_column(String(40), default="IMAGE", index=True, nullable=False)
    original_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    corrected_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    detected_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    reference_asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    preserved_constraints: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    editable_variables: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    changes_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    corrector_version: Mapped[str] = mapped_column(String(80), nullable=False)


class FeatureFlag(Base, TimestampMixin):
    __tablename__ = "feature_flags"
    __table_args__ = (UniqueConstraint("name", "scope_key", name="uq_feature_flag_name_scope_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    scope_key: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ShotMemory(Base, TimestampMixin):
    __tablename__ = "shot_memories"
    __table_args__ = (Index("ix_shot_memories_scope", "project_id", "layer", "scene_id", "memory_type"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    layer: Mapped[str] = mapped_column(String(8), index=True, nullable=False)
    memory_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    text_content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    image_urls: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    video_urls: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    entity_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    scene_id: Mapped[str | None] = mapped_column(ForeignKey("scenes.id"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    asset_version_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    temporal_position: Mapped[float | None] = mapped_column(Float)
    canonical: Mapped[bool] = mapped_column(Boolean, default=False, index=True, nullable=False)
    # JSON keeps Matryoshka output dimension configurable. A production PostgreSQL
    # deployment may add a matching pgvector expression/index without changing this contract.
    embedding: Mapped[list[float]] = mapped_column(JSON, default=list, nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, default=512, nullable=False)
    embedding_provider: Mapped[str] = mapped_column(String(80), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(120), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class EvaluationResult(Base, TimestampMixin):
    __tablename__ = "evaluation_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    generated_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    decision: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    critical_failure: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scores_json: Mapped[dict[str, float]] = mapped_column(JSON, default=dict, nullable=False)
    checks_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    retry_reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    retry_patch: Mapped[str] = mapped_column(Text, default="", nullable=False)
    evidence_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(80), nullable=False)
    judge_provider: Mapped[str] = mapped_column(String(80), default="none", nullable=False)
    judge_model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), default="", index=True, nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), default="", index=True, nullable=False)


class ModelDefinition(Base, TimestampMixin):
    """Provider-neutral model catalogue entry used by business-layer roles."""

    __tablename__ = "model_definitions"
    __table_args__ = (
        UniqueConstraint("logical_name", name="uq_model_definitions_logical_name"),
        UniqueConstraint(
            "provider",
            "provider_model_id",
            "modality",
            name="uq_model_definitions_provider_model_modality",
        ),
        Index("ix_model_definitions_provider_enabled", "provider", "enabled"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    logical_name: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    modality: Mapped[str] = mapped_column(String(50), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    quality_tier: Mapped[str] = mapped_column(String(40), default="STANDARD", nullable=False)
    cost_class: Mapped[str] = mapped_column(String(40), default="STANDARD", nullable=False)
    provider_trust_level: Mapped[str] = mapped_column(String(40), default="STANDARD", nullable=False)
    criticality_allowed: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    live_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    user_visible: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    router_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(40),
        default=ModelLifecycleStatus.CONFIGURED.value,
        server_default=ModelLifecycleStatus.CONFIGURED.value,
        index=True,
        nullable=False,
    )
    pricing_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=False
    )
    # Whether this model's price has been confirmed against the provider's own
    # published rates. UNVERIFIED is the honest default: a number with no source
    # is a guess, and a guess that is 40% of the real price loses money on every
    # call — which is what `estimated_per_second = 0.09` did for Seedance 2.5.
    # A billable model that is UNVERIFIED is refused a paid route rather than
    # quoted from a placeholder.
    pricing_status: Mapped[str] = mapped_column(
        String(24),
        default="UNVERIFIED",
        server_default="UNVERIFIED",
        index=True,
        nullable=False,
    )
    # Where this model stands in the live canary sequence. NOT_RUN is the
    # starting point, VERIFIED_LIVE means one real generation completed and
    # reconciled, and LIVE_BLOCKED_EXTERNAL means the attempt was refused by
    # something outside this repository — an account setting, a balance, a
    # permission. That distinction exists so one blocked provider cannot stall
    # the audit of every model behind it, and so a blocked model is never
    # mistaken later for one that was proven.
    live_canary_status: Mapped[str] = mapped_column(
        String(32), default="NOT_RUN", server_default="NOT_RUN", index=True, nullable=False
    )
    live_canary_detail: Mapped[str] = mapped_column(
        String(500), default="", server_default="", nullable=False
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_live_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    context_window: Mapped[int | None] = mapped_column(Integer)
    max_duration: Mapped[float | None] = mapped_column(Float)
    supported_aspect_ratios: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ModelVerification(Base):
    """Immutable evidence for a model-specific production-protocol verification."""

    __tablename__ = "model_verifications"
    __table_args__ = (
        UniqueConstraint("model_definition_id", "idempotency_key", name="uq_model_verification_key"),
        Index("ix_model_verification_created", "model_definition_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    model_definition_id: Mapped[str] = mapped_column(
        ForeignKey("model_definitions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    operator_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(120), nullable=False)
    result: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    evidence_reference: Mapped[str] = mapped_column(String(500), nullable=False)
    billable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class ProviderControl(Base, TimestampMixin):
    """Persisted provider kill switch; credentials remain in the secret plane."""

    __tablename__ = "provider_controls"
    provider: Mapped[str] = mapped_column(String(80), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    disabled_reason: Mapped[str | None] = mapped_column(String(500))
    changed_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )


def _install_admin_append_only_ddl() -> None:
    for table in (AdminAuditLog.__table__, AdminCreditAdjustment.__table__, ModelVerification.__table__):
        table_name = str(table.name)  # type: ignore[attr-defined]
        for operation in ("UPDATE", "DELETE"):
            event.listen(
                table,
                "after_create",
                DDL(
                    f"CREATE TRIGGER trg_{table_name}_append_only_{operation.lower()} "
                    f"BEFORE {operation} ON {table_name} "
                    f"BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END"
                ).execute_if(dialect="sqlite"),
            )
        event.listen(
            table,
            "after_create",
            DDL(
                "CREATE OR REPLACE FUNCTION enforce_admin_append_only() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'admin audit table is append-only' USING ERRCODE = '23000'; "
                "RETURN OLD; END; $$"
            ).execute_if(dialect="postgresql"),
        )
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER trg_{table_name}_append_only BEFORE UPDATE OR DELETE ON {table_name} "
                "FOR EACH ROW EXECUTE FUNCTION enforce_admin_append_only()"
            ).execute_if(dialect="postgresql"),
        )


_install_admin_append_only_ddl()


class ModelPricingProfile(Base, TimestampMixin):
    """One provider's published price for one model, mode and resolution.

    Replaces a single `estimated_per_second` per model plus a global resolution
    multiplier shared by every provider. That design encoded one vendor's price
    curve as if it were physics: it charged 1080p at 1.30x across the board when
    Ark's own published rates put 1080p at 2.47x its 720p, and it had no 480p
    entry at all, so 480p quoted as though it were 720p.

    Price is stored in the provider's own currency at the provider's own billing
    unit, because that is the only form in which it can be checked against the
    published page. The USD conversion carries its own rate and source, so a
    quote can always be traced back to two dated facts rather than one rounded
    number.

    `estimate_formula` and `settlement_formula` are separate on purpose. Ark
    quotes per second for planning but settles on `usage.completion_tokens`; a
    reservation taken from the estimate and a debit taken from the settlement are
    different numbers, and pretending otherwise is how a ledger drifts.
    """

    __tablename__ = "model_pricing_profiles"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_model_id",
            "input_mode",
            "resolution",
            "effective_from",
            name="uq_model_pricing_profile_scope",
        ),
        CheckConstraint("unit_price >= 0", name="ck_model_pricing_unit_price_nonnegative"),
        CheckConstraint("estimate_unit_price >= 0", name="ck_model_pricing_estimate_nonnegative"),
        CheckConstraint("usd_per_currency > 0", name="ck_model_pricing_fx_positive"),
        CheckConstraint(
            "effective_until IS NULL OR effective_until > effective_from",
            name="ck_model_pricing_effective_window",
        ),
        Index("ix_model_pricing_profiles_lookup", "provider", "provider_model_id", "input_mode"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # "no_video_input" / "video_input" / "default" — the axis a provider actually
    # prices on. Ark charges less per token when the input carries video.
    input_mode: Mapped[str] = mapped_column(String(40), default="default", nullable=False)
    # "" where the model has no resolution axis, e.g. a per-image price.
    resolution: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    # How the provider *bills*: the unit the invoice is computed in.
    billing_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    # How the provider lets you *plan*. Ark bills on completion tokens, which
    # nobody can know before the clip exists, and publishes a per-second typical
    # price for exactly this purpose. The reservation is taken from this; the
    # debit is settled from the one above. Equal to `unit_price` where a provider
    # bills in the same unit it quotes in.
    estimate_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    estimate_unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    usd_per_currency: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    fx_source: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    fx_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimate_formula: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    settlement_formula: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    # A promotion has an end. Writing a discounted rate in as the base price is
    # how a temporary number becomes permanent by accident.
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    source_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str] = mapped_column(String(1000), default="", nullable=False)


class ModelCapabilityProfile(Base, TimestampMixin):
    """Authoritative, persisted capability and manual-quality profile for one model."""

    __tablename__ = "model_capability_profiles"
    __table_args__ = (
        CheckConstraint("max_reference_images >= 0", name="ck_model_capability_max_references"),
        CheckConstraint(
            "min_duration IS NULL OR min_duration > 0",
            name="ck_model_capability_min_duration",
        ),
        CheckConstraint(
            "max_duration IS NULL OR max_duration > 0",
            name="ck_model_capability_max_duration",
        ),
        CheckConstraint(
            "min_duration IS NULL OR max_duration IS NULL OR min_duration <= max_duration",
            name="ck_model_capability_duration_range",
        ),
        CheckConstraint(
            "physics_prior >= 0 AND physics_prior <= 1 "
            "AND identity_prior >= 0 AND identity_prior <= 1 "
            "AND camera_prior >= 0 AND camera_prior <= 1 "
            "AND render_prior >= 0 AND render_prior <= 1 "
            "AND action_prior >= 0 AND action_prior <= 1 "
            "AND dialogue_prior >= 0 AND dialogue_prior <= 1 "
            "AND text_render_prior >= 0 AND text_render_prior <= 1",
            name="ck_model_capability_manual_priors",
        ),
        Index("ix_model_capability_profiles_source", "source"),
    )
    model_definition_id: Mapped[str] = mapped_column(
        ForeignKey("model_definitions.id", ondelete="CASCADE"), primary_key=True
    )
    profile_version: Mapped[str] = mapped_column(String(80), default="1", nullable=False)
    confidence_level: Mapped[str] = mapped_column(String(40), default="initial", nullable=False)
    supported_operations: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    supports_image_generation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_video_generation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_t2v: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_i2v: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_v2v: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_reference_image: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_multi_reference: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_start_frame: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_end_frame: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_start_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_character_reference: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_video_extension: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_camera_instruction: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_audio: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Audio carried *in* as a reference, as opposed to supports_audio, which is
    # audio the model generates.
    supports_reference_voice: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_text_rendering: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_reference_images: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    min_duration: Mapped[float | None] = mapped_column(Float)
    max_duration: Mapped[float | None] = mapped_column(Float)
    supported_aspect_ratios: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    supported_resolutions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    physics_prior: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    identity_prior: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    camera_prior: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    render_prior: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    action_prior: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    dialogue_prior: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    text_render_prior: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="MANUAL_PRIOR", nullable=False)


class ModelRoleBinding(Base, TimestampMixin):
    """Configurable role-to-model binding, optionally scoped to a plan tier."""

    __tablename__ = "model_role_bindings"
    __table_args__ = (
        UniqueConstraint(
            "role",
            "plan_tier",
            "model_definition_id",
            name="uq_model_role_binding_scope_model",
        ),
        Index(
            "ix_model_role_binding_lookup",
            "role",
            "plan_tier",
            "enabled",
            "priority",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    role: Mapped[str] = mapped_column(String(80), nullable=False)
    plan_tier: Mapped[str] = mapped_column(String(40), default="ALL", nullable=False)
    model_definition_id: Mapped[str] = mapped_column(
        ForeignKey("model_definitions.id", ondelete="CASCADE"), nullable=False
    )
    binding_kind: Mapped[str] = mapped_column(String(40), default="PRIMARY", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ProviderBudget(Base, TimestampMixin):
    """Durable provider-level spend ceiling used by low-trust edge routing."""

    __tablename__ = "provider_budgets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    credit_budget_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    actual_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"), nullable=False)
    reserved_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"), nullable=False)
    routing_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ProviderBudgetUsage(Base, TimestampMixin):
    """Exactly-once reserve/settle audit for a provider task."""

    __tablename__ = "provider_budget_usages"
    __table_args__ = (
        UniqueConstraint("provider", "task_id", name="uq_provider_budget_usage_task"),
        Index("ix_provider_budget_usage_lookup", "provider", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    budget_id: Mapped[str] = mapped_column(
        ForeignKey("provider_budgets.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    task_id: Mapped[str] = mapped_column(String(200), nullable=False)
    task_role: Mapped[str] = mapped_column(String(100), nullable=False)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    remaining_budget_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="RESERVED", nullable=False)


class ModelMetric(Base, TimestampMixin):
    __tablename__ = "model_metrics"
    __table_args__ = (
        UniqueConstraint("generation_job_id", "metric_name", name="uq_model_metric_job_name"),
        Index("ix_model_metrics_model_name", "provider", "model_id", "metric_name"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    model_version: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    metric_name: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    value: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class RouterObservation(Base, TimestampMixin):
    """One generation attempt, wide enough to compute a posterior from.

    Written alongside ``model_metrics``, which is unchanged and still drives
    the adaptive router. This table exists because ``model_metrics`` records a
    metric name and a value and cannot say which snapshot ran, what was asked
    of it, or under what conditions — and every one of those changes what an
    outcome means.

    Append-only in practice, and one row per generation job: the unique
    constraint makes a duplicated webhook or a retried worker collapse onto the
    row that is already there instead of counting the same attempt twice.
    """

    __tablename__ = "router_observations"
    __table_args__ = (
        UniqueConstraint("generation_job_id", name="uq_router_observation_job"),
        Index(
            "ix_router_observations_key",
            "provider",
            "model_id",
            "exact_version",
            "task_type",
            "scenario",
        ),
        Index("ix_router_observations_occurred_at", "occurred_at"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_router_obs_latency_nonneg"),
        CheckConstraint(
            "user_rating IS NULL OR (user_rating >= 1 AND user_rating <= 5)",
            name="ck_router_obs_rating_range",
        ),
        CheckConstraint(
            "qc_identity_score IS NULL OR (qc_identity_score >= 0 AND qc_identity_score <= 1)",
            name="ck_router_obs_qc_identity_range",
        ),
        CheckConstraint(
            "qc_motion_score IS NULL OR (qc_motion_score >= 0 AND qc_motion_score <= 1)",
            name="ck_router_obs_qc_motion_range",
        ),
        CheckConstraint(
            "qc_prompt_alignment IS NULL OR (qc_prompt_alignment >= 0 AND qc_prompt_alignment <= 1)",
            name="ck_router_obs_qc_prompt_range",
        ),
        CheckConstraint(
            "qc_temporal_consistency IS NULL OR "
            "(qc_temporal_consistency >= 0 AND qc_temporal_consistency <= 1)",
            name="ck_router_obs_qc_temporal_range",
        ),
        CheckConstraint(
            "generation_success = true OR (qc_identity_score IS NULL AND qc_motion_score IS NULL "
            "AND qc_prompt_alignment IS NULL AND qc_temporal_consistency IS NULL "
            "AND user_rating IS NULL AND accepted_output IS NULL)",
            name="ck_router_obs_failed_has_no_quality",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # Indexed by the explicit ``ix_router_observations_occurred_at`` above; a
    # second ``index=True`` here would emit the same CREATE INDEX twice.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    #: The snapshot, never the alias. An observation that could not name one is
    #: refused by the service rather than defaulted, because attributing it to
    #: whatever the alias resolves to today is the contamination this whole
    #: table exists to avoid.
    exact_version: Mapped[str] = mapped_column(String(120), nullable=False)
    model_is_alias: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    task_type: Mapped[str] = mapped_column(String(8), nullable=False)
    scenario: Mapped[str] = mapped_column(String(40), nullable=False)
    asset_criticality: Mapped[str] = mapped_column(String(40), nullable=False)
    prompt_complexity: Mapped[str] = mapped_column(String(24), nullable=False)
    reference_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    resolution: Mapped[str] = mapped_column(String(32), default="n/a", nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(32), default="n/a", nullable=False)

    generation_success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    provider_failure: Mapped[str | None] = mapped_column(String(120))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_credits: Mapped[float | None] = mapped_column(Float)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(18, 6))

    user_rating: Mapped[int | None] = mapped_column(Integer)
    user_preference_ab: Mapped[str | None] = mapped_column(String(8))
    user_preference_opponent: Mapped[str | None] = mapped_column(String(200))
    regenerated: Mapped[bool | None] = mapped_column(Boolean)
    switched_model: Mapped[bool | None] = mapped_column(Boolean)
    downloaded: Mapped[bool | None] = mapped_column(Boolean)
    accepted_output: Mapped[bool | None] = mapped_column(Boolean)
    used_in_next_shot: Mapped[bool | None] = mapped_column(Boolean)

    qc_identity_score: Mapped[float | None] = mapped_column(Float)
    qc_motion_score: Mapped[float | None] = mapped_column(Float)
    qc_prompt_alignment: Mapped[float | None] = mapped_column(Float)
    qc_temporal_consistency: Mapped[float | None] = mapped_column(Float)

    router_version: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    router_decision_id: Mapped[str | None] = mapped_column(String(64))
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class RouterPosterior(Base, TimestampMixin):
    """One saved offline posterior cell.

    A snapshot of a computation, not a live value. Rows are written by an
    offline run, carry the run that produced them, and are read by operators
    and by the replay harness. The routing path does not query this table.
    """

    __tablename__ = "router_posteriors"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "provider",
            "model_id",
            "exact_version",
            "task_type",
            "scenario",
            "metric_scale_id",
            "outcome_name",
            "level",
            "condition_token",
            name="uq_router_posterior_cell",
        ),
        Index("ix_router_posteriors_lookup", "provider", "model_id", "exact_version", "outcome_name"),
        # Quantiles only. The mean is deliberately *not* required to sit
        # between them: for a heavily skewed Beta — the shape a cell with a
        # long run of identical outcomes takes — the mean can lie outside its
        # own central interval, and a constraint saying otherwise rejects
        # correct arithmetic.
        CheckConstraint(
            "posterior_lower_quantile <= posterior_upper_quantile",
            name="ck_router_posterior_ordered",
        ),
        CheckConstraint("observation_count >= 0", name="ck_router_posterior_count_nonneg"),
        CheckConstraint("effective_sample_size >= 0", name="ck_router_posterior_ess_nonneg"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    engine_version: Mapped[str] = mapped_column(String(60), nullable=False)

    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    exact_version: Mapped[str] = mapped_column(String(120), nullable=False)
    task_type: Mapped[str] = mapped_column(String(8), nullable=False)
    scenario: Mapped[str] = mapped_column(String(40), nullable=False)
    metric_scale_id: Mapped[str] = mapped_column(String(80), nullable=False)
    outcome_name: Mapped[str] = mapped_column(String(48), nullable=False)
    level: Mapped[str] = mapped_column(String(48), nullable=False)
    #: ``duration|resolution|reference_mode`` for a condition-level row, ``-``
    #: otherwise. Part of the unique constraint, so a NULL would let the same
    #: cell be written twice on PostgreSQL.
    condition_token: Mapped[str] = mapped_column(String(120), default="-", nullable=False)

    posterior_mean: Mapped[float] = mapped_column(Float, nullable=False)
    posterior_lower_quantile: Mapped[float] = mapped_column(Float, nullable=False)
    posterior_upper_quantile: Mapped[float] = mapped_column(Float, nullable=False)
    lower_quantile_level: Mapped[float] = mapped_column(Float, nullable=False)
    upper_quantile_level: Mapped[float] = mapped_column(Float, nullable=False)
    effective_sample_size: Mapped[float] = mapped_column(Float, nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    alpha: Mapped[float] = mapped_column(Float, nullable=False)
    beta: Mapped[float] = mapped_column(Float, nullable=False)
    prior_alpha: Mapped[float] = mapped_column(Float, nullable=False)
    prior_beta: Mapped[float] = mapped_column(Float, nullable=False)
    prior_sources: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    prior_version: Mapped[str] = mapped_column(String(80), default="none", nullable=False)
    parent_level: Mapped[str | None] = mapped_column(String(48))
    parent_mean: Mapped[float | None] = mapped_column(Float)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RouterReplayRun(Base, TimestampMixin):
    """The result of one historical replay, kept because a gate depends on it.

    The conservative LCB flag may only be turned on after a replay passes. A
    claim that one did is worth nothing unless the run itself is on file with
    its numbers, so this is where it goes.
    """

    __tablename__ = "router_replay_runs"
    __table_args__ = (
        UniqueConstraint("run_id", "outcome_name", name="uq_router_replay_run_outcome"),
        CheckConstraint("fit_observations >= 0", name="ck_router_replay_fit_nonneg"),
        CheckConstraint("eval_observations >= 0", name="ck_router_replay_eval_nonneg"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    harness_version: Mapped[str] = mapped_column(String(60), nullable=False)
    outcome_name: Mapped[str] = mapped_column(String(48), nullable=False)
    posterior_run_id: Mapped[str | None] = mapped_column(String(64))

    fit_observations: Mapped[int] = mapped_column(Integer, nullable=False)
    eval_observations: Mapped[int] = mapped_column(Integer, nullable=False)
    contexts: Mapped[int] = mapped_column(Integer, nullable=False)
    unscored_contexts: Mapped[int] = mapped_column(Integer, nullable=False)

    baseline_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    posterior_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    coverage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    #: The gate itself. A row with ``passed=false`` is as important as one with
    #: true: it is the evidence that the flag must stay off.
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class ModelBenchmarkResult(Base, TimestampMixin):
    __tablename__ = "model_benchmark_results"
    __table_args__ = (Index("ix_benchmark_model_case", "provider", "model_id", "case_key", "suite_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    suite_version: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    case_key: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    model_version: Mapped[str] = mapped_column(String(80), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scores_json: Mapped[dict[str, float]] = mapped_column(JSON, default=dict, nullable=False)
    evidence_asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class ProductionTrace(Base, TimestampMixin):
    __tablename__ = "production_traces"
    __table_args__ = (Index("ix_production_traces_trace_id", "trace_id", unique=True),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    context_asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    retrieved_memory_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    router_scores_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    generation_latency: Mapped[float | None] = mapped_column(Float)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_cost: Mapped[float | None] = mapped_column(Float)
    evaluation_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    retry_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ModelExecutionRecord(Base):
    """Immutable evidence for every role-runtime provider attempt."""

    __tablename__ = "model_execution_records"
    __table_args__ = (
        CheckConstraint("latency_ms >= 0", name="ck_model_execution_latency_nonnegative"),
        CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name="ck_model_execution_estimated_cost_nonnegative",
        ),
        CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="ck_model_execution_actual_cost_nonnegative",
        ),
        Index("ix_model_execution_project_role_created", "project_id", "role", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model_definition_id: Mapped[str] = mapped_column(
        ForeignKey("model_definitions.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    provider_model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    token_usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    cost_source: Mapped[str] = mapped_column(
        String(40), default=BillingEvidenceSource.UNKNOWN.value, nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(120))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class EmbeddingEvidence(Base):
    """Vector provenance without persisting the vector in an audit/log table."""

    __tablename__ = "embedding_evidence"
    __table_args__ = (
        CheckConstraint("embedding_dimension > 0", name="ck_embedding_evidence_dimension_positive"),
        CheckConstraint("latency_ms >= 0", name="ck_embedding_evidence_latency_nonnegative"),
        UniqueConstraint(
            "model_execution_record_id",
            "asset_id",
            "input_hash",
            name="uq_embedding_evidence_execution_input",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), index=True)
    model_definition_id: Mapped[str] = mapped_column(
        ForeignKey("model_definitions.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    model_execution_record_id: Mapped[str] = mapped_column(
        ForeignKey("model_execution_records.id", ondelete="CASCADE"), index=True, nullable=False
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class ProviderBillingEvidence(Base, TimestampMixin):
    """Provider cost/credit fact with an explicit trust source."""

    __tablename__ = "provider_billing_evidence"
    __table_args__ = (
        UniqueConstraint("generation_job_id", "evidence_key", name="uq_billing_evidence_job_key"),
        CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="ck_billing_evidence_actual_nonnegative",
        ),
        CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name="ck_billing_evidence_estimated_nonnegative",
        ),
        CheckConstraint(
            "provider_credits IS NULL OR provider_credits >= 0",
            name="ck_billing_evidence_credits_nonnegative",
        ),
        Index("ix_billing_evidence_provider_model", "provider", "model", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    cost_record_id: Mapped[str | None] = mapped_column(ForeignKey("cost_records.id"), index=True)
    evidence_key: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(
        String(40), default=BillingEvidenceSource.UNKNOWN.value, index=True, nullable=False
    )
    provider_reference: Mapped[str | None] = mapped_column(String(500))
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    provider_credits: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DecisionOutcomeRecord(Base):
    """Durable training/evaluation join across decision, attempt, QA, cost, and user outcome."""

    __tablename__ = "decision_outcome_records"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_decision_outcome_candidate"),
        Index("ix_decision_outcome_provider_model", "provider", "model", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    shot_id: Mapped[str] = mapped_column(ForeignKey("shots.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("generation_candidates.id", ondelete="CASCADE"), nullable=False
    )
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    qa_result_id: Mapped[str | None] = mapped_column(ForeignKey("qa_results.id"), index=True)
    continuity_decision: Mapped[str] = mapped_column(String(80), nullable=False)
    generation_policy: Mapped[str] = mapped_column(String(80), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    shot_features_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    qa_result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    user_outcome: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    billing_source: Mapped[str] = mapped_column(
        String(40), default=BillingEvidenceSource.UNKNOWN.value, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class TimelineTransition(Base, TimestampMixin):
    __tablename__ = "timeline_transitions"
    __table_args__ = (
        UniqueConstraint("target_shot_id", name="uq_timeline_transition_target_shot"),
        Index("ix_timeline_transition_project_type", "project_id", "transition_type"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    target_shot_id: Mapped[str] = mapped_column(
        ForeignKey("shots.id", ondelete="CASCADE"), index=True, nullable=False
    )
    transition_type: Mapped[str] = mapped_column(
        String(40), default=TimelineTransitionType.CONTINUOUS.value, nullable=False
    )
    branch_key: Mapped[str | None] = mapped_column(String(120))
    reconciliation_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class LiveCanaryPermit(Base, TimestampMixin):
    __tablename__ = "live_canary_permits"
    __table_args__ = (
        CheckConstraint("max_requests > 0", name="ck_live_canary_max_requests_positive"),
        CheckConstraint("max_cost_usd > 0", name="ck_live_canary_max_cost_positive"),
        CheckConstraint("used_requests >= 0", name="ck_live_canary_used_requests_nonnegative"),
        CheckConstraint("reserved_cost_usd >= 0", name="ck_live_canary_reserved_cost_nonnegative"),
        CheckConstraint("actual_cost_usd >= 0", name="ck_live_canary_actual_cost_nonnegative"),
        Index("ix_live_canary_lookup", "provider", "model", "status", "expires_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    max_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    max_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    used_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"), nullable=False)
    actual_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    purpose: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class LiveCanaryUsage(Base, TimestampMixin):
    __tablename__ = "live_canary_usages"
    __table_args__ = (
        UniqueConstraint("permit_id", "idempotency_key", name="uq_live_canary_usage_key"),
        CheckConstraint("estimated_cost_usd >= 0", name="ck_live_canary_usage_estimated_nonnegative"),
        CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="ck_live_canary_usage_actual_nonnegative",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    permit_id: Mapped[str] = mapped_column(
        ForeignKey("live_canary_permits.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    evidence_reference: Mapped[str | None] = mapped_column(String(500))


class ProductionBudgetLedger(Base, TimestampMixin):
    """One spend window of the automatic production budget, for one scope.

    Two scopes exist: ``PLATFORM`` (``scope_key = "platform"``) and one
    ``PROVIDER`` row per provider. Every live spend authorization reserves
    against both rows of its window before any money can move, and settles or
    releases against the same two rows afterwards, so the breaker is a
    conditional update on a row and never a sum that a concurrent request can
    race past. Windows are UTC calendar days: a held-but-unreconciled amount
    stops burdening the platform when its day ends, while the authorization it
    belongs to keeps waiting for the operator's finding.
    """

    __tablename__ = "production_budget_ledgers"
    __table_args__ = (
        UniqueConstraint("scope", "scope_key", "window_start", name="uq_production_budget_window"),
        CheckConstraint("limit_usd >= 0", name="ck_production_budget_limit_nonnegative"),
        CheckConstraint("reserved_usd >= 0", name="ck_production_budget_reserved_nonnegative"),
        CheckConstraint("actual_usd >= 0", name="ck_production_budget_actual_nonnegative"),
        CheckConstraint("window_seconds > 0", name="ck_production_budget_window_positive"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(80), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"), nullable=False)
    actual_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class GenerationSpendAuthorization(Base, TimestampMixin):
    """One single-use USD authorization for one live provider operation.

    A generation's authorization is created in the same transaction as its
    workspace credit reservation, bound to the workspace, the job, the
    provider and the model, with ``max_cost_usd`` taken from the server quote
    and never from the request. A model-role call (director, embeddings) gets
    one per call, sized by the token estimate. The row is the unit the
    platform breaker reserves and settles, and its status mirrors the canary
    usage vocabulary: RESERVED before any transport, UNCERTAIN once the paid
    boundary may have been crossed, SETTLED with a figure, RELEASED with proof
    that nothing left the process.
    """

    __tablename__ = "generation_spend_authorizations"
    __table_args__ = (
        UniqueConstraint("operation_key", name="uq_spend_authorization_operation"),
        UniqueConstraint("generation_job_id", name="uq_spend_authorization_job"),
        Index("ix_spend_authorization_lookup", "provider", "model", "status"),
        Index("ix_spend_authorization_workspace", "workspace_id", "created_at"),
        CheckConstraint("max_cost_usd > 0", name="ck_spend_authorization_max_positive"),
        CheckConstraint("reserved_cost_usd >= 0", name="ck_spend_authorization_reserved_nonnegative"),
        CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="ck_spend_authorization_actual_nonnegative",
        ),
        CheckConstraint("quoted_credits >= 0", name="ck_spend_authorization_credits_nonnegative"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operation_key: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"), index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="RESTRICT")
    )
    model_role: Mapped[str | None] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    max_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    reserved_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    quoted_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pricing_version: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="RESERVED", index=True, nullable=False)
    #: PENDING until the gateway decides at the paid boundary; then PRODUCTION
    #: (the authorization alone fenced the call) or CANARY (an operator permit
    #: fenced it too, because the model had not yet earned VERIFIED_LIVE).
    fence: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    settlement_source: Mapped[str | None] = mapped_column(String(40))
    evidence_reference: Mapped[str | None] = mapped_column(String(500))
    platform_ledger_id: Mapped[str] = mapped_column(
        ForeignKey("production_budget_ledgers.id", ondelete="RESTRICT"), nullable=False
    )
    provider_ledger_id: Mapped[str] = mapped_column(
        ForeignKey("production_budget_ledgers.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class RunAPIBenchmark(Base, TimestampMixin):
    __tablename__ = "runapi_benchmarks"
    __table_args__ = (UniqueConstraint("task_id", name="uq_runapi_benchmark_task"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(String(200), nullable=False)
    task_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_quality: Mapped[float | None] = mapped_column(Float)
    fact_lock_pass: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fallback_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    user_acceptance: Mapped[bool | None] = mapped_column(Boolean)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class StorageReservation(Base, TimestampMixin):
    __tablename__ = "storage_reservations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_storage_reservation_key"),
        CheckConstraint("reserved_bytes > 0", name="ck_storage_reservation_bytes_positive"),
        Index("ix_storage_reservation_status", "workspace_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="RESERVED", nullable=False)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), index=True)
    storage_key: Mapped[str | None] = mapped_column(String(1000))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_password_reset_token_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class AuthLoginThrottle(Base, TimestampMixin):
    __tablename__ = "auth_login_throttles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class CreativeSessionStatus(StrEnum):
    """Lifecycle of one AI-creative-director conversation.

    Every transition is forward-only except ABANDONED; approvals move the
    session, never edits. The stage names mirror the product flow:
    idea -> clarify -> brief -> screenplay -> key visuals -> visual bible ->
    beats -> compile. The screenplay stage (2026-09-02) is where the DIRECTOR
    model writes the treatment and script from the approved brief; the key
    visuals are derived from brief *and* screenplay together.
    """

    INTAKE = "INTAKE"
    CLARIFYING = "CLARIFYING"
    BRIEF_PROPOSED = "BRIEF_PROPOSED"
    BRIEF_APPROVED = "BRIEF_APPROVED"
    SCREENPLAY_PROPOSED = "SCREENPLAY_PROPOSED"
    SCREENPLAY_APPROVED = "SCREENPLAY_APPROVED"
    VISUALS_IN_PROGRESS = "VISUALS_IN_PROGRESS"
    BIBLE_PROPOSED = "BIBLE_PROPOSED"
    BIBLE_LOCKED = "BIBLE_LOCKED"
    BEATS_PROPOSED = "BEATS_PROPOSED"
    COMPILED = "COMPILED"
    ABANDONED = "ABANDONED"


class CreativeFormat(StrEnum):
    SHORT_DRAMA = "SHORT_DRAMA"
    ADVERTISEMENT = "ADVERTISEMENT"
    PRODUCT_SHOWCASE = "PRODUCT_SHOWCASE"
    SOCIAL_SHORT = "SOCIAL_SHORT"
    MUSIC_VISUAL = "MUSIC_VISUAL"
    FASHION_LOOKBOOK = "FASHION_LOOKBOOK"
    BEAUTY_TUTORIAL = "BEAUTY_TUTORIAL"
    CONCEPT_FILM = "CONCEPT_FILM"
    UNSPECIFIED = "UNSPECIFIED"


class CreativeAnchorStatus(StrEnum):
    PENDING = "PENDING"
    GENERATING = "GENERATING"
    READY = "READY"
    FAILED = "FAILED"
    #: An optional anchor the user explicitly chose to go without; recorded,
    #: never inferred. A required anchor can never be skipped.
    SKIPPED = "SKIPPED"
    #: Its content changed after it was derived; a newer version of the same
    #: anchor_key carries the current prompt. Old prompts and images are never
    #: re-used under a new version.
    SUPERSEDED = "SUPERSEDED"


class CreativeActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class CreativeSession(Base, TimestampMixin):
    """One stateful creative-director engagement over a project."""

    __tablename__ = "creative_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('INTAKE', 'CLARIFYING', 'BRIEF_PROPOSED', 'BRIEF_APPROVED', "
            "'SCREENPLAY_PROPOSED', 'SCREENPLAY_APPROVED', "
            "'VISUALS_IN_PROGRESS', 'BIBLE_PROPOSED', 'BIBLE_LOCKED', 'BEATS_PROPOSED', "
            "'COMPILED', 'ABANDONED')",
            name="ck_creative_session_status",
        ),
        CheckConstraint(
            "format IN ('SHORT_DRAMA', 'ADVERTISEMENT', 'PRODUCT_SHOWCASE', 'SOCIAL_SHORT', "
            "'MUSIC_VISUAL', 'FASHION_LOOKBOOK', 'BEAUTY_TUTORIAL', 'CONCEPT_FILM', 'UNSPECIFIED')",
            name="ck_creative_session_format",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=CreativeSessionStatus.INTAKE.value, nullable=False
    )
    format: Mapped[str] = mapped_column(
        String(40), default=CreativeFormat.UNSPECIFIED.value, nullable=False
    )
    #: Head pointers into the append-only revision tables below. They are
    #: projections, not truth: the revision rows are.
    current_brief_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_screenplay_revision: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    current_bible_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_beat_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    compiled_episode_id: Mapped[str | None] = mapped_column(ForeignKey("episodes.id"), nullable=True)


class CreativeTurn(Base, TimestampMixin):
    """Append-only dialogue ledger: what was said, asked, and extracted.

    A director turn is written in the same transaction as the user turn it
    answers, the brief revision it produced and the question states it moved,
    so a model failure can never leave a user message without its result (and
    never counts against the FREE dialogue budget).
    """

    __tablename__ = "creative_turns"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_creative_turn_sequence"),
        UniqueConstraint("session_id", "client_turn_id", name="uq_creative_turn_client_id"),
        CheckConstraint("speaker IN ('USER', 'DIRECTOR')", name="ck_creative_turn_speaker"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("creative_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Questions the director chose to ask on this turn - each carries the gap
    #: code it targets, so a question is never re-asked for an answered gap.
    questions_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    #: The structured brief patch this turn produced (empty for pure replies).
    #: Since 2026-09-02 this is the list of applied brief operations.
    extracted_json: Mapped[dict[str, Any] | list[dict[str, Any]]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    #: MODEL:<role> when a model reasoned about this turn, DETERMINISTIC when
    #: the rules engine did, USER for user turns. Model outage degrades to the
    #: rules engine loudly, never silently.
    reasoner: Mapped[str] = mapped_column(String(60), default="USER", nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    brief_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Client-supplied idempotency key for a user turn: a retried POST with the
    #: same key returns the recorded director reply instead of a second turn.
    client_turn_id: Mapped[str | None] = mapped_column(String(120))
    #: Which Director Skill text the model was given (content-addressed), and
    #: the ModelExecutionRecord that paid for the call - the audit of what the
    #: director actually saw and said.
    skill_version: Mapped[str | None] = mapped_column(String(80))
    skill_content_hash: Mapped[str | None] = mapped_column(String(64))
    model_execution_record_id: Mapped[str | None] = mapped_column(String(36))
    #: How the model's context was assembled: turns included, compression
    #: applied, the preserved facts. Empty for user turns.
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    #: The validated DirectorTurnResult (assumptions, unresolved questions,
    #: creative notes, rejected operations) behind this director turn.
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class CreativeBriefRevision(Base, TimestampMixin):
    """Append-only CreativeBrief revisions; approval freezes one.

    ``provenance_json`` records, per field path, who established the value
    (the user's words, a model inference, a default, a direct edit, an
    accepted assumption), from which turn, by which operation. ``question_state_json``
    is the per-gap question ledger (UNASKED / ASKED / ANSWERED /
    SKIPPED_BY_USER / ASSUMPTION_ACCEPTED) as of this revision.
    """

    __tablename__ = "creative_briefs"
    __table_args__ = (
        UniqueConstraint("session_id", "revision", name="uq_creative_brief_revision"),
        CheckConstraint(
            "status IN ('PROPOSED', 'APPROVED', 'SUPERSEDED')",
            name="ck_creative_brief_status",
        ),
        CheckConstraint("length(content_hash) = 64", name="ck_creative_brief_hash_length"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("creative_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="PROPOSED", nullable=False)
    fields_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    #: The gap report computed for this revision: which fields are missing,
    #: their value weight, and which already have an asked question.
    completeness_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    question_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    #: What produced this revision: TURN (a dialogue round), USER_EDIT (the
    #: brief editor), ASSUMPTION (an accepted assumption), APPROVAL.
    source: Mapped[str] = mapped_column(String(40), default="TURN", server_default="TURN", nullable=False)
    turn_id: Mapped[str | None] = mapped_column(String(36))


class CreativeScreenplayRevision(Base, TimestampMixin):
    """Append-only, model-authored screenplay revisions for one session.

    ``content_json`` is the validated structured screenplay (treatment, hook,
    invariants and variables, characters and relationships, scenes, beats with
    dialogue and one-action ShotIntents, start/end states and continuity
    obligations, product claims and required copy). ``script_text`` is the
    rendering of that structure in the narrative compiler's own vocabulary -
    derived, never edited by hand. Only the exact APPROVED revision is ever
    compiled, and the compiled episode records which one.
    """

    __tablename__ = "creative_screenplays"
    __table_args__ = (
        UniqueConstraint("session_id", "revision", name="uq_creative_screenplay_revision"),
        CheckConstraint(
            "status IN ('PROPOSED', 'APPROVED', 'SUPERSEDED')",
            name="ck_creative_screenplay_status",
        ),
        CheckConstraint("length(content_hash) = 64", name="ck_creative_screenplay_hash_length"),
        CheckConstraint("revision > 0", name="ck_creative_screenplay_revision_positive"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("creative_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="PROPOSED", nullable=False)
    brief_id: Mapped[str] = mapped_column(ForeignKey("creative_briefs.id"), nullable=False)
    #: MODEL:DIRECTOR, DETERMINISTIC (explicit degradation, shown to the
    #: user), or USER_EDIT (the user's own revision of an earlier one).
    reasoner: Mapped[str] = mapped_column(String(60), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    parent_revision: Mapped[int | None] = mapped_column(Integer)
    #: The user's rewrite request that produced this revision, when any.
    user_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    skill_version: Mapped[str | None] = mapped_column(String(80))
    skill_content_hash: Mapped[str | None] = mapped_column(String(64))
    model_execution_record_id: Mapped[str | None] = mapped_column(String(36))
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    script_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VisualBibleVersion(Base, TimestampMixin):
    """Versioned visual bible; LOCKED versions are immutable by contract.

    Locking is the version-lock the product promises after user approval: the
    service refuses any further mutation of a LOCKED row, and later changes
    append a new version that supersedes it. ``lineage_json`` records what the
    lock produced through the platform's own services - the
    CharacterIdentityVersion per character anchor and the ProjectStyleLock -
    and a bible whose lineage is incomplete blocks compilation.
    """

    __tablename__ = "visual_bibles"
    __table_args__ = (
        UniqueConstraint("session_id", "version", name="uq_visual_bible_version"),
        CheckConstraint(
            "status IN ('DRAFT', 'LOCKED', 'SUPERSEDED')",
            name="ck_visual_bible_status",
        ),
        CheckConstraint("version > 0", name="ck_visual_bible_version_positive"),
        CheckConstraint(
            "status != 'LOCKED' OR locked_at IS NOT NULL",
            name="ck_visual_bible_locked_at",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("creative_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT", nullable=False)
    brief_id: Mapped[str] = mapped_column(ForeignKey("creative_briefs.id"), nullable=False)
    screenplay_id: Mapped[str | None] = mapped_column(ForeignKey("creative_screenplays.id"))
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(120))
    lineage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class CreativeVisualAnchor(Base, TimestampMixin):
    """One key visual the director wants generated and bound.

    The anchor is the structured intent; the generation itself always goes
    through the existing Passenger image path (admission, credits, router,
    gateway) - never a direct provider call from creative code. An anchor is
    versioned by content: when the brief or screenplay changes what the anchor
    depicts, a new (anchor_key, version) row is created and the old one is
    SUPERSEDED, so an old prompt or image is never passed off as the new one.
    """

    __tablename__ = "creative_visual_anchors"
    __table_args__ = (
        UniqueConstraint("session_id", "anchor_key", "version", name="uq_creative_anchor_key"),
        CheckConstraint(
            "kind IN ('CHARACTER', 'SCENE', 'STYLE', 'PRODUCT', 'PROP', 'MOOD')",
            name="ck_creative_anchor_kind",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'GENERATING', 'READY', 'FAILED', 'SKIPPED', 'SUPERSEDED')",
            name="ck_creative_anchor_status",
        ),
        CheckConstraint("version > 0", name="ck_creative_anchor_version_positive"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("creative_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    anchor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: Structured prompt parts (subject / style / constraints), composed into a
    #: provider prompt only at action-execution time.
    prompt_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    #: A required anchor must be READY before a visual bible can be proposed;
    #: an optional one may be skipped by the user, on record.
    required: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=CreativeAnchorStatus.PENDING.value, nullable=False
    )
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"))
    media_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    character_id: Mapped[str | None] = mapped_column(ForeignKey("characters.id"))
    failure_code: Mapped[str | None] = mapped_column(String(240))
    skip_reason: Mapped[str | None] = mapped_column(String(240))
    brief_id: Mapped[str | None] = mapped_column(ForeignKey("creative_briefs.id"))
    screenplay_id: Mapped[str | None] = mapped_column(ForeignKey("creative_screenplays.id"))


class CreativeAction(Base, TimestampMixin):
    """Append-only structured actions - the director's only door to execution.

    The creative director never calls a provider; it emits one of these rows
    and the API layer executes it through the existing admission, credit,
    router and gateway chain. The row is the audit that nothing else happened.
    """

    __tablename__ = "creative_actions"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_creative_action_sequence"),
        UniqueConstraint("idempotency_key", name="uq_creative_action_idempotency"),
        CheckConstraint(
            "kind IN ('GENERATE_KEY_VISUAL', 'CREATE_EPISODE', 'COMPILE_EPISODE', "
            "'OPEN_OBLIGATION', 'ESTABLISH_FACT', 'LOCK_CHARACTER_IDENTITY', 'LOCK_PROJECT_STYLE')",
            name="ck_creative_action_kind",
        ),
        CheckConstraint(
            "status IN ('PROPOSED', 'EXECUTED', 'FAILED', 'SKIPPED')",
            name="ck_creative_action_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("creative_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=CreativeActionStatus.PROPOSED.value, nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(250))
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CreativeBeat(Base, TimestampMixin):
    """One beat of a proposed beat plan, with its structured shot intents.

    Since 2026-09-02 beats are materialized from an approved screenplay
    revision (``screenplay_id``) rather than from a fixed scaffold; the
    scaffold survives only as the explicit DETERMINISTIC degradation.
    """

    __tablename__ = "creative_beats"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "plan_revision", "sequence", name="uq_creative_beat_sequence"
        ),
        CheckConstraint(
            "status IN ('PROPOSED', 'APPROVED', 'SUPERSEDED')",
            name="ck_creative_beat_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("creative_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="PROPOSED", nullable=False)
    #: {intent, summary, location, time, characters, shots: [ShotIntent...]}
    beat_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    screenplay_id: Mapped[str | None] = mapped_column(ForeignKey("creative_screenplays.id"))


class CreativeLockStep(Base, TimestampMixin):
    """One step of the visual-bible lock, and what it produced.

    Locking a bible writes real, immutable Canon through three services -
    ProjectStyleService, CharacterIdentityService and the AssetRegistry - none
    of which can share one transaction. A failure part-way therefore leaves
    Canon that cannot be rolled back (asset versions, promotions and style
    locks are append-only by database trigger, and a project has exactly one
    style lock). The answer is not a rollback but a resume: each step has a
    stable idempotency key and records what it produced, so a retry continues
    the missing steps instead of minting a second identity version or a second
    canonical asset version.

    The row alone is not the guarantee - a process can die between the write
    and the COMPLETED stamp - so every step also re-discovers its own output
    from the Canon before doing the work. This table is what makes the resume
    cheap and auditable; discovery is what makes it exactly-once.
    """

    __tablename__ = "creative_lock_steps"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_creative_lock_step_key"),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_creative_lock_step_status",
        ),
        Index("ix_creative_lock_step_bible", "bible_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("creative_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    bible_id: Mapped[str] = mapped_column(
        ForeignKey("visual_bibles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: STYLE, CHARACTER_IDENTITY or SUPPORTING_ASSET.
    step_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The anchor key (or "style:master") this step is about.
    step_key: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(250), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: What the step created or found: identity version id, asset version id,
    #: style lock id. The recovery record a partial lock is judged by.
    produced_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    #: How the step was satisfied: EXECUTED (this attempt did it) or
    #: RECOVERED (a previous attempt had already done it).
    resolution: Mapped[str | None] = mapped_column(String(20))
    last_error: Mapped[str | None] = mapped_column(String(500))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CreativeShotLineage(Base, TimestampMixin):
    """Where one compiled shot came from: brief, screenplay, bible, anchors, locks.

    Written in the compile step for every shot the narrative compiler created
    from an approved screenplay, so a shot can be traced back to the exact
    approved brief and screenplay revisions, the locked visual bible, the key
    visual anchors it depends on, the CharacterIdentityVersions and the
    ProjectStyleLock that bound them.
    """

    __tablename__ = "creative_shot_lineage"
    __table_args__ = (UniqueConstraint("shot_id", name="uq_creative_shot_lineage_shot"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("creative_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    shot_id: Mapped[str] = mapped_column(ForeignKey("shots.id", ondelete="CASCADE"), nullable=False)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False)
    brief_id: Mapped[str] = mapped_column(ForeignKey("creative_briefs.id"), nullable=False)
    screenplay_id: Mapped[str] = mapped_column(ForeignKey("creative_screenplays.id"), nullable=False)
    bible_id: Mapped[str | None] = mapped_column(ForeignKey("visual_bibles.id"))
    beat_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    shot_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    anchor_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    identity_version_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    style_lock_id: Mapped[str | None] = mapped_column(String(36))
    intent_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class EpisodeContinuation(Base, TimestampMixin):
    """The bridge from a finished episode to the next one.

    Holds the computed EpisodeContinuationContext snapshot (what the next
    episode inherits and what it must not), the proposed brief and beats, and
    the compiled result. One row per (project, previous episode, next number),
    so preparation is idempotent and confirmation is replayable.
    """

    __tablename__ = "episode_continuations"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "previous_episode_id",
            "next_episode_number",
            name="uq_episode_continuation_target",
        ),
        CheckConstraint(
            "status IN ('BRIEF_PROPOSED', 'CONFIRMED', 'COMPILED', 'ABANDONED')",
            name="ck_episode_continuation_status",
        ),
        CheckConstraint(
            "continuation_mode IN ('CONTINUOUS', 'TIME_JUMP', 'LOCATION_CHANGE')",
            name="ck_episode_continuation_mode",
        ),
        CheckConstraint("next_episode_number > 1", name="ck_episode_continuation_number"),
        CheckConstraint("length(context_hash) = 64", name="ck_episode_continuation_hash"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    previous_episode_id: Mapped[str] = mapped_column(
        ForeignKey("episodes.id"), index=True, nullable=False
    )
    next_episode_id: Mapped[str | None] = mapped_column(ForeignKey("episodes.id"), index=True)
    next_episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="BRIEF_PROPOSED", nullable=False)
    #: CONTINUOUS inherits the previous ending frame inside the same location;
    #: TIME_JUMP / LOCATION_CHANGE inherit narrative and character state only.
    continuation_mode: Mapped[str] = mapped_column(String(40), nullable=False)
    time_gap: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    new_location: Mapped[str | None] = mapped_column(String(200))
    #: The EpisodeContinuationContext snapshot this proposal reasoned from.
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    context_version: Mapped[str] = mapped_column(String(60), nullable=False)
    brief_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    beats_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    #: Prior proposal revisions, appended when a re-proposal replaces them.
    revisions_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    reasoner: Mapped[str] = mapped_column(String(60), default="DETERMINISTIC", nullable=False)
    script_rendered: Mapped[str] = mapped_column(Text, default="", nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(120))


def _install_character_state_integrity_ddl() -> None:
    """Install the create-all equivalent of migration 0028's state ledger guards."""

    # Metadata-level DDL runs only after every table is available.  This is
    # required because the head/commit guards deliberately inspect each other.
    anchor = Base.metadata
    protected_tables = (
        "character_identity_versions",
        "character_state_versions",
        "character_state_deltas",
        "character_state_validations",
        "character_state_commits",
    )
    for table_name in protected_tables:
        for operation in ("UPDATE", "DELETE"):
            trigger_name = f"trg_{table_name}_immutable_{operation.lower()}"
            event.listen(
                anchor,
                "after_create",
                DDL(
                    f"CREATE TRIGGER IF NOT EXISTS {trigger_name} BEFORE {operation} "
                    f"ON {table_name} BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END"
                ).execute_if(dialect="sqlite"),
            )

    sqlite_statements = (
        """CREATE TRIGGER IF NOT EXISTS trg_character_identity_pointer_insert
        BEFORE INSERT ON characters WHEN NEW.current_identity_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM character_identity_versions AS identity
            WHERE identity.id = NEW.current_identity_version_id
              AND identity.character_id = NEW.id AND identity.status = 'LOCKED'
        ) BEGIN SELECT RAISE(ABORT, 'current identity must be a locked version of the character'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_identity_pointer_update
        BEFORE UPDATE OF id, current_identity_version_id ON characters
        WHEN NEW.current_identity_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM character_identity_versions AS identity
            WHERE identity.id = NEW.current_identity_version_id
              AND identity.character_id = NEW.id AND identity.status = 'LOCKED'
        ) BEGIN SELECT RAISE(ABORT, 'current identity must be a locked version of the character'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_canonical_facts_frozen
        BEFORE UPDATE OF canonical_facts ON characters
        WHEN OLD.current_identity_version_id IS NOT NULL
          AND NEW.canonical_facts IS NOT OLD.canonical_facts
        BEGIN SELECT RAISE(ABORT, 'confirmed canonical facts are immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_state_version_consistency
        BEFORE INSERT ON character_state_versions WHEN
            json_type(NEW.narrative_state_json) != 'object'
            OR json_type(NEW.narrative_state_json, '$.identity') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.canonical_identity') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.face') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.body_proportions') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.canonical_hair') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.canonical_outfit') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.identity_embedding_id') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.canonical_asset_id') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.face') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.hair') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.body') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.body_proportions') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.canonical_hair') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.canonical_outfit') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.outfit.type') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.outfit.design') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.outfit.color') IS NOT NULL
            OR NOT EXISTS (
                SELECT 1 FROM character_identity_versions AS identity
                WHERE identity.id = NEW.identity_version_id
                  AND identity.character_id = NEW.character_id AND identity.status = 'LOCKED'
            )
            OR (NEW.previous_state_version_id IS NULL AND NEW.version != 1)
            OR (NEW.previous_state_version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM character_state_versions AS previous
                WHERE previous.id = NEW.previous_state_version_id
                  AND previous.character_id = NEW.character_id
                  AND previous.state_hash = NEW.previous_state_hash
                  AND ((previous.timeline_scope_key = NEW.timeline_scope_key
                        AND NEW.version = previous.version + 1)
                       OR (previous.timeline_scope_key != NEW.timeline_scope_key AND NEW.version = 1))
            ))
            OR (NEW.source_candidate_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM generation_candidates AS candidate
                JOIN shots AS shot ON shot.id = candidate.shot_id
                JOIN scenes AS scene ON scene.id = shot.scene_id
                JOIN episodes AS episode ON episode.id = scene.episode_id
                WHERE candidate.id = NEW.source_candidate_id
                  AND shot.id = NEW.source_shot_id AND episode.project_id = NEW.project_id
            ))
        BEGIN SELECT RAISE(ABORT, 'character state version is inconsistent'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_state_delta_consistency
        BEFORE INSERT ON character_state_deltas WHEN
            json_type(NEW.patch_json) != 'array'
            OR json_type(NEW.changed_paths_json) != 'array'
            OR json_type(NEW.proposed_state_json) != 'object'
            OR json_type(NEW.proposed_state_json, '$.identity') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.canonical_identity') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.face') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.body_proportions') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.canonical_hair') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.canonical_outfit') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.identity_embedding_id') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.canonical_asset_id') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.face') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.hair') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.body') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.body_proportions') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.canonical_hair') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.canonical_outfit') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.outfit.type') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.outfit.design') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.outfit.color') IS NOT NULL
            OR EXISTS (
                SELECT 1 FROM json_each(NEW.patch_json) AS patch
                WHERE CASE
                   WHEN patch.type != 'object' THEN 1
                   WHEN json_type(patch.value, '$.path') IS NOT 'text' THEN 1
                   WHEN json_extract(patch.value, '$.path') = ''
                   OR json_extract(patch.value, '$.path') = '/'
                   OR json_extract(patch.value, '$.path') NOT LIKE '/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance'
                   OR json_extract(patch.value, '$.path') = '/appearance/outfit'
                   OR json_extract(patch.value, '$.path') = '/identity'
                   OR json_extract(patch.value, '$.path') LIKE '/identity/%%'
                   OR json_extract(patch.value, '$.path') = '/canonical_identity'
                   OR json_extract(patch.value, '$.path') LIKE '/canonical_identity/%%'
                   OR json_extract(patch.value, '$.path') = '/face'
                   OR json_extract(patch.value, '$.path') LIKE '/face/%%'
                   OR json_extract(patch.value, '$.path') = '/body_proportions'
                   OR json_extract(patch.value, '$.path') LIKE '/body_proportions/%%'
                   OR json_extract(patch.value, '$.path') = '/canonical_hair'
                   OR json_extract(patch.value, '$.path') LIKE '/canonical_hair/%%'
                   OR json_extract(patch.value, '$.path') = '/canonical_outfit'
                   OR json_extract(patch.value, '$.path') LIKE '/canonical_outfit/%%'
                   OR json_extract(patch.value, '$.path') = '/identity_embedding_id'
                   OR json_extract(patch.value, '$.path') LIKE '/identity_embedding_id/%%'
                   OR json_extract(patch.value, '$.path') = '/canonical_asset_id'
                   OR json_extract(patch.value, '$.path') LIKE '/canonical_asset_id/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/face'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/face/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/hair'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/hair/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/body'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/body/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/body_proportions'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/body_proportions/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/canonical_hair'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/canonical_hair/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/canonical_outfit'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/canonical_outfit/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/outfit/type'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/outfit/type/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/outfit/design'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/outfit/design/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/outfit/color'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/outfit/color/%%'
                   THEN 1 ELSE 0 END
            )
            OR NOT EXISTS (
                SELECT 1 FROM generation_candidates AS candidate
                JOIN shots AS shot ON shot.id = candidate.shot_id
                JOIN scenes AS scene ON scene.id = shot.scene_id
                JOIN episodes AS episode ON episode.id = scene.episode_id
                WHERE candidate.id = NEW.candidate_id AND shot.id = NEW.shot_id
                  AND episode.project_id = NEW.project_id
            )
            OR NOT EXISTS (
                SELECT 1 FROM timeline_states AS input_state
                WHERE input_state.id = NEW.input_timeline_state_id
                  AND input_state.project_id = NEW.project_id
                  AND input_state.state_kind = 'SHOT_INPUT'
                  AND (input_state.shot_id IS NULL OR input_state.shot_id = NEW.shot_id)
            )
            OR NOT EXISTS (
                SELECT 1 FROM timeline_states AS output_state
                WHERE output_state.id = NEW.planned_output_timeline_state_id
                  AND output_state.project_id = NEW.project_id
                  AND output_state.state_kind = 'SHOT_OUTPUT'
                  AND (output_state.shot_id IS NULL OR output_state.shot_id = NEW.shot_id)
            )
            OR NOT EXISTS (
                SELECT 1 FROM shots AS shot WHERE shot.id = NEW.shot_id
                  AND shot.input_state_id = NEW.input_timeline_state_id
                  AND shot.output_state_id = NEW.planned_output_timeline_state_id
            )
            OR (NEW.proposal_kind NOT IN ('INITIALIZE', 'IDENTITY_REBASE') AND NOT EXISTS (
                SELECT 1 FROM character_state_versions AS base
                WHERE base.id = NEW.base_state_version_id
                  AND base.identity_version_id = NEW.identity_version_id
                  AND base.state_hash = NEW.base_state_hash
            ))
            OR (NEW.supersedes_delta_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM character_state_deltas AS prior
                WHERE prior.id = NEW.supersedes_delta_id
                  AND prior.project_id = NEW.project_id
                  AND prior.character_id = NEW.character_id
                  AND prior.candidate_id = NEW.candidate_id
                  AND prior.proposal_revision < NEW.proposal_revision
            ))
            OR (NEW.model_execution_record_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM model_execution_records AS execution
                WHERE execution.id = NEW.model_execution_record_id
                  AND execution.project_id = NEW.project_id
            ))
        BEGIN SELECT RAISE(ABORT, 'character state delta is inconsistent'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_state_validation_consistency
        BEFORE INSERT ON character_state_validations WHEN
            json_type(NEW.observed_state_json) != 'object'
            OR json_type(NEW.evidence_json) != 'object'
            OR json_type(NEW.violations_json) != 'array'
            OR NOT EXISTS (
                SELECT 1 FROM character_state_deltas AS delta
                WHERE delta.id = NEW.state_delta_id AND delta.project_id = NEW.project_id
                  AND delta.target_state_hash = NEW.validated_target_hash
            )
            OR (NEW.qa_result_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM qa_results AS qa
                JOIN character_state_deltas AS delta ON delta.id = NEW.state_delta_id
                WHERE qa.id = NEW.qa_result_id AND qa.candidate_id = delta.candidate_id
            ))
            OR (NEW.evidence_asset_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM media_assets AS asset
                WHERE asset.id = NEW.evidence_asset_id AND asset.project_id = NEW.project_id
            ))
            OR (NEW.model_execution_record_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM model_execution_records AS execution
                WHERE execution.id = NEW.model_execution_record_id
                  AND execution.project_id = NEW.project_id
            ))
        BEGIN SELECT RAISE(ABORT, 'character state validation is inconsistent'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_state_commit_consistency
        BEFORE INSERT ON character_state_commits WHEN
            NOT EXISTS (
                SELECT 1 FROM character_state_deltas AS delta
                WHERE delta.id = NEW.state_delta_id AND delta.project_id = NEW.project_id
                  AND delta.character_id = NEW.character_id
                  AND delta.timeline_scope_key = NEW.timeline_scope_key
                  AND delta.shot_id = NEW.shot_id AND delta.candidate_id = NEW.candidate_id
                  AND delta.base_state_version_id IS NEW.from_state_version_id
            )
            OR NOT EXISTS (
                SELECT 1 FROM character_state_deltas AS delta
                JOIN character_state_versions AS target ON target.id = NEW.to_state_version_id
                WHERE delta.id = NEW.state_delta_id AND target.project_id = NEW.project_id
                  AND target.character_id = NEW.character_id
                  AND target.timeline_scope_key = NEW.timeline_scope_key
                  AND target.version = delta.target_version
                  AND target.previous_state_version_id IS NEW.from_state_version_id
                  AND target.identity_version_id = delta.identity_version_id
                  AND target.source_shot_id = NEW.shot_id
                  AND target.source_candidate_id = NEW.candidate_id
                  AND target.state_hash = delta.target_state_hash
            )
            OR NOT EXISTS (
                SELECT 1 FROM character_state_validations AS validation
                WHERE validation.id = NEW.policy_validation_id
                  AND validation.state_delta_id = NEW.state_delta_id
                  AND validation.stage = 'POLICY' AND validation.decision = 'PASS'
            )
            OR NOT EXISTS (
                SELECT 1 FROM character_state_validations AS visual
                WHERE visual.id = NEW.visual_validation_id
                  AND visual.state_delta_id = NEW.state_delta_id
                  AND visual.stage = 'VISUAL'
                  AND (visual.decision = 'PASS' OR (
                      visual.decision = 'REVIEW_REQUIRED'
                      AND NEW.human_validation_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM character_state_validations AS human
                          WHERE human.id = NEW.human_validation_id
                            AND human.state_delta_id = NEW.state_delta_id
                            AND human.stage = 'HUMAN_OVERRIDE' AND human.decision = 'PASS'
                      )
                  ))
            )
            OR (NEW.human_validation_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM character_state_validations AS validation
                WHERE validation.id = NEW.human_validation_id
                  AND validation.state_delta_id = NEW.state_delta_id
                  AND validation.stage = 'HUMAN_OVERRIDE' AND validation.decision = 'PASS'
            ))
            OR NOT EXISTS (
                SELECT 1 FROM generation_candidates AS candidate
                JOIN shots AS shot ON shot.id = candidate.shot_id
                WHERE candidate.id = NEW.candidate_id AND candidate.status = 'COMMITTED'
                  AND shot.id = NEW.shot_id AND shot.committed_candidate_id = NEW.candidate_id
            )
            OR ((SELECT COUNT(*) FROM character_state_heads AS head
                 WHERE head.project_id = NEW.project_id AND head.character_id = NEW.character_id
                   AND head.timeline_scope_key = NEW.timeline_scope_key) = 0
                AND NEW.expected_head_version != 0)
            OR ((SELECT COUNT(*) FROM character_state_heads AS head
                 WHERE head.project_id = NEW.project_id AND head.character_id = NEW.character_id
                   AND head.timeline_scope_key = NEW.timeline_scope_key) > 0
                AND NOT EXISTS (
                    SELECT 1 FROM character_state_heads AS head
                    WHERE head.project_id = NEW.project_id AND head.character_id = NEW.character_id
                      AND head.timeline_scope_key = NEW.timeline_scope_key
                      AND head.state_version_id IS NEW.from_state_version_id
                      AND head.lock_version = NEW.expected_head_version
                ))
        BEGIN SELECT RAISE(ABORT, 'character state commit is inconsistent'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_state_head_insert
        BEFORE INSERT ON character_state_heads WHEN
            NEW.lock_version != 1 OR NOT EXISTS (
                SELECT 1 FROM character_state_versions AS version
                JOIN character_state_commits AS commit_row
                  ON commit_row.to_state_version_id = version.id
                WHERE version.id = NEW.state_version_id
                  AND version.project_id = NEW.project_id
                  AND version.character_id = NEW.character_id
                  AND version.timeline_scope_key = NEW.timeline_scope_key
                  AND version.version = 1
                  AND commit_row.expected_head_version = 0
            )
        BEGIN SELECT RAISE(ABORT, 'character state head requires an initial commit'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_state_head_update
        BEFORE UPDATE ON character_state_heads WHEN
            NEW.id != OLD.id OR NEW.project_id != OLD.project_id
            OR NEW.character_id != OLD.character_id
            OR NEW.timeline_scope_key != OLD.timeline_scope_key
            OR NEW.lock_version != OLD.lock_version + 1
            OR NOT EXISTS (
                SELECT 1 FROM character_state_versions AS version
                JOIN character_state_commits AS commit_row
                  ON commit_row.to_state_version_id = version.id
                WHERE version.id = NEW.state_version_id
                  AND version.project_id = NEW.project_id
                  AND version.character_id = NEW.character_id
                  AND version.timeline_scope_key = NEW.timeline_scope_key
                  AND version.version = NEW.lock_version
                  AND commit_row.from_state_version_id = OLD.state_version_id
                  AND commit_row.expected_head_version = OLD.lock_version
            )
        BEGIN SELECT RAISE(ABORT, 'character state head update requires a fresh commit'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_state_head_delete
        BEFORE DELETE ON character_state_heads
        BEGIN SELECT RAISE(ABORT, 'character state heads cannot be deleted'); END""",
    )
    for statement in sqlite_statements:
        event.listen(anchor, "after_create", DDL(statement).execute_if(dialect="sqlite"))

    postgres_statements = (
        """CREATE OR REPLACE FUNCTION enforce_character_state_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '%% is append-only', TG_TABLE_NAME USING ERRCODE = '23000';
            RETURN OLD;
        END; $$""",
        """CREATE OR REPLACE FUNCTION enforce_character_identity_boundary()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.current_identity_version_id IS NOT NULL
               AND NEW.canonical_facts::jsonb IS DISTINCT FROM OLD.canonical_facts::jsonb THEN
                RAISE EXCEPTION 'confirmed canonical facts are immutable' USING ERRCODE = '23514';
            END IF;
            IF NEW.current_identity_version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM character_identity_versions AS identity
                WHERE identity.id = NEW.current_identity_version_id
                  AND identity.character_id = NEW.id AND identity.status = 'LOCKED'
            ) THEN
                RAISE EXCEPTION 'current identity must be a locked version of the character'
                USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END; $$""",
        """CREATE OR REPLACE FUNCTION enforce_character_state_consistency()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE head_count integer;
        BEGIN
            IF TG_TABLE_NAME = 'character_state_versions' THEN
                IF json_typeof(NEW.narrative_state_json) <> 'object'
                   OR NEW.narrative_state_json::jsonb ?| ARRAY[
                       'identity', 'canonical_identity', 'face', 'body_proportions',
                       'canonical_hair', 'canonical_outfit', 'identity_embedding_id',
                       'canonical_asset_id'
                   ]
                   OR NEW.narrative_state_json::jsonb #> '{appearance,face}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,hair}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,body}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,body_proportions}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,canonical_hair}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,canonical_outfit}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,outfit,type}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,outfit,design}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,outfit,color}' IS NOT NULL
                   OR NOT EXISTS (
                       SELECT 1 FROM character_identity_versions AS identity
                       WHERE identity.id = NEW.identity_version_id
                         AND identity.character_id = NEW.character_id AND identity.status = 'LOCKED'
                   )
                   OR (NEW.previous_state_version_id IS NULL AND NEW.version <> 1)
                   OR (NEW.previous_state_version_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM character_state_versions AS previous
                       WHERE previous.id = NEW.previous_state_version_id
                         AND previous.character_id = NEW.character_id
                         AND previous.state_hash = NEW.previous_state_hash
                         AND ((previous.timeline_scope_key = NEW.timeline_scope_key
                               AND NEW.version = previous.version + 1)
                              OR (previous.timeline_scope_key <> NEW.timeline_scope_key
                                  AND NEW.version = 1))
                   ))
                   OR (NEW.source_candidate_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM generation_candidates AS candidate
                       JOIN shots AS shot ON shot.id = candidate.shot_id
                       JOIN scenes AS scene ON scene.id = shot.scene_id
                       JOIN episodes AS episode ON episode.id = scene.episode_id
                       WHERE candidate.id = NEW.source_candidate_id
                         AND shot.id = NEW.source_shot_id AND episode.project_id = NEW.project_id
                   )) THEN
                    RAISE EXCEPTION 'character state version is inconsistent' USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'character_state_deltas' THEN
                IF json_typeof(NEW.patch_json) <> 'array'
                   OR json_typeof(NEW.changed_paths_json) <> 'array'
                   OR json_typeof(NEW.proposed_state_json) <> 'object'
                   OR NEW.proposed_state_json::jsonb ?| ARRAY[
                       'identity', 'canonical_identity', 'face', 'body_proportions',
                       'canonical_hair', 'canonical_outfit', 'identity_embedding_id',
                       'canonical_asset_id'
                   ]
                   OR NEW.proposed_state_json::jsonb #> '{appearance,face}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,hair}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,body}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,body_proportions}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,canonical_hair}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,canonical_outfit}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,outfit,type}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,outfit,design}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,outfit,color}' IS NOT NULL
                   OR EXISTS (
                       SELECT 1 FROM json_array_elements(NEW.patch_json) AS patch
                       WHERE json_typeof(patch) <> 'object'
                           OR COALESCE(json_typeof(patch->'path'), '') <> 'string'
                           OR COALESCE(patch->>'path', '') IN ('', '/', '/appearance', '/appearance/outfit')
                           OR COALESCE(patch->>'path', '') !~ '^/'
                           OR COALESCE(patch->>'path', '') ~
                           '^/(identity|canonical_identity|face|body_proportions|canonical_hair|canonical_outfit|identity_embedding_id|canonical_asset_id)(/|$)'
                           OR COALESCE(patch->>'path', '') ~
                           '^/appearance/(face|hair|body|body_proportions|canonical_hair|canonical_outfit)(/|$)'
                           OR COALESCE(patch->>'path', '') ~
                           '^/appearance/outfit/(type|design|color)(/|$)'
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM generation_candidates AS candidate
                       JOIN shots AS shot ON shot.id = candidate.shot_id
                       JOIN scenes AS scene ON scene.id = shot.scene_id
                       JOIN episodes AS episode ON episode.id = scene.episode_id
                       WHERE candidate.id = NEW.candidate_id AND shot.id = NEW.shot_id
                         AND episode.project_id = NEW.project_id
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM timeline_states AS input_state
                       WHERE input_state.id = NEW.input_timeline_state_id
                         AND input_state.project_id = NEW.project_id
                         AND input_state.state_kind = 'SHOT_INPUT'
                         AND (input_state.shot_id IS NULL OR input_state.shot_id = NEW.shot_id)
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM timeline_states AS output_state
                       WHERE output_state.id = NEW.planned_output_timeline_state_id
                         AND output_state.project_id = NEW.project_id
                         AND output_state.state_kind = 'SHOT_OUTPUT'
                         AND (output_state.shot_id IS NULL OR output_state.shot_id = NEW.shot_id)
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM shots AS shot WHERE shot.id = NEW.shot_id
                         AND shot.input_state_id = NEW.input_timeline_state_id
                         AND shot.output_state_id = NEW.planned_output_timeline_state_id
                   )
                   OR (NEW.proposal_kind NOT IN ('INITIALIZE', 'IDENTITY_REBASE') AND NOT EXISTS (
                       SELECT 1 FROM character_state_versions AS base
                       WHERE base.id = NEW.base_state_version_id
                         AND base.identity_version_id = NEW.identity_version_id
                         AND base.state_hash = NEW.base_state_hash
                   ))
                   OR (NEW.supersedes_delta_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM character_state_deltas AS prior
                       WHERE prior.id = NEW.supersedes_delta_id
                         AND prior.project_id = NEW.project_id
                         AND prior.character_id = NEW.character_id
                         AND prior.candidate_id = NEW.candidate_id
                         AND prior.proposal_revision < NEW.proposal_revision
                   ))
                   OR (NEW.model_execution_record_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM model_execution_records AS execution
                       WHERE execution.id = NEW.model_execution_record_id
                         AND execution.project_id = NEW.project_id
                   )) THEN
                    RAISE EXCEPTION 'character state delta is inconsistent' USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'character_state_validations' THEN
                IF json_typeof(NEW.observed_state_json) <> 'object'
                   OR json_typeof(NEW.evidence_json) <> 'object'
                   OR json_typeof(NEW.violations_json) <> 'array'
                   OR NOT EXISTS (
                       SELECT 1 FROM character_state_deltas AS delta
                       WHERE delta.id = NEW.state_delta_id AND delta.project_id = NEW.project_id
                         AND delta.target_state_hash = NEW.validated_target_hash
                   )
                   OR (NEW.qa_result_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM qa_results AS qa
                       JOIN character_state_deltas AS delta ON delta.id = NEW.state_delta_id
                       WHERE qa.id = NEW.qa_result_id AND qa.candidate_id = delta.candidate_id
                   ))
                   OR (NEW.evidence_asset_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM media_assets AS asset
                       WHERE asset.id = NEW.evidence_asset_id AND asset.project_id = NEW.project_id
                   ))
                   OR (NEW.model_execution_record_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM model_execution_records AS execution
                       WHERE execution.id = NEW.model_execution_record_id
                         AND execution.project_id = NEW.project_id
                   )) THEN
                    RAISE EXCEPTION 'character state validation is inconsistent' USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'character_state_commits' THEN
                IF NOT EXISTS (
                       SELECT 1 FROM character_state_deltas AS delta
                       WHERE delta.id = NEW.state_delta_id AND delta.project_id = NEW.project_id
                         AND delta.character_id = NEW.character_id
                         AND delta.timeline_scope_key = NEW.timeline_scope_key
                         AND delta.shot_id = NEW.shot_id AND delta.candidate_id = NEW.candidate_id
                         AND delta.base_state_version_id IS NOT DISTINCT FROM NEW.from_state_version_id
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM character_state_deltas AS delta
                       JOIN character_state_versions AS target ON target.id = NEW.to_state_version_id
                       WHERE delta.id = NEW.state_delta_id AND target.project_id = NEW.project_id
                         AND target.character_id = NEW.character_id
                         AND target.timeline_scope_key = NEW.timeline_scope_key
                         AND target.version = delta.target_version
                         AND target.previous_state_version_id IS NOT DISTINCT FROM NEW.from_state_version_id
                         AND target.identity_version_id = delta.identity_version_id
                         AND target.source_shot_id = NEW.shot_id
                         AND target.source_candidate_id = NEW.candidate_id
                         AND target.state_hash = delta.target_state_hash
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM character_state_validations AS validation
                       WHERE validation.id = NEW.policy_validation_id
                         AND validation.state_delta_id = NEW.state_delta_id
                         AND validation.stage = 'POLICY' AND validation.decision = 'PASS'
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM character_state_validations AS visual
                       WHERE visual.id = NEW.visual_validation_id
                         AND visual.state_delta_id = NEW.state_delta_id
                         AND visual.stage = 'VISUAL'
                         AND (visual.decision = 'PASS' OR (
                             visual.decision = 'REVIEW_REQUIRED'
                             AND NEW.human_validation_id IS NOT NULL
                             AND EXISTS (
                                 SELECT 1 FROM character_state_validations AS human
                                 WHERE human.id = NEW.human_validation_id
                                   AND human.state_delta_id = NEW.state_delta_id
                                   AND human.stage = 'HUMAN_OVERRIDE' AND human.decision = 'PASS'
                             )
                         ))
                   )
                   OR (NEW.human_validation_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM character_state_validations AS validation
                       WHERE validation.id = NEW.human_validation_id
                         AND validation.state_delta_id = NEW.state_delta_id
                         AND validation.stage = 'HUMAN_OVERRIDE' AND validation.decision = 'PASS'
                   ))
                   OR NOT EXISTS (
                       SELECT 1 FROM generation_candidates AS candidate
                       JOIN shots AS shot ON shot.id = candidate.shot_id
                       WHERE candidate.id = NEW.candidate_id AND candidate.status = 'COMMITTED'
                         AND shot.id = NEW.shot_id AND shot.committed_candidate_id = NEW.candidate_id
                   ) THEN
                    RAISE EXCEPTION 'character state commit is inconsistent' USING ERRCODE = '23514';
                END IF;
                SELECT COUNT(*) INTO head_count FROM character_state_heads AS head
                WHERE head.project_id = NEW.project_id AND head.character_id = NEW.character_id
                  AND head.timeline_scope_key = NEW.timeline_scope_key;
                IF (head_count = 0 AND NEW.expected_head_version <> 0)
                   OR (head_count > 0 AND NOT EXISTS (
                       SELECT 1 FROM character_state_heads AS head
                       WHERE head.project_id = NEW.project_id AND head.character_id = NEW.character_id
                         AND head.timeline_scope_key = NEW.timeline_scope_key
                         AND head.state_version_id IS NOT DISTINCT FROM NEW.from_state_version_id
                         AND head.lock_version = NEW.expected_head_version
                   )) THEN
                    RAISE EXCEPTION 'character state commit head fence is stale' USING ERRCODE = '40001';
                END IF;
            ELSIF TG_TABLE_NAME = 'character_state_heads' THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'character state heads cannot be deleted' USING ERRCODE = '23000';
                ELSIF TG_OP = 'INSERT' THEN
                    IF NEW.lock_version <> 1 OR NOT EXISTS (
                        SELECT 1 FROM character_state_versions AS version
                        JOIN character_state_commits AS commit_row
                          ON commit_row.to_state_version_id = version.id
                        WHERE version.id = NEW.state_version_id
                          AND version.project_id = NEW.project_id
                          AND version.character_id = NEW.character_id
                          AND version.timeline_scope_key = NEW.timeline_scope_key
                          AND version.version = 1 AND commit_row.expected_head_version = 0
                    ) THEN
                        RAISE EXCEPTION 'character state head requires an initial commit'
                        USING ERRCODE = '23514';
                    END IF;
                ELSIF NEW.id IS DISTINCT FROM OLD.id
                   OR NEW.project_id IS DISTINCT FROM OLD.project_id
                   OR NEW.character_id IS DISTINCT FROM OLD.character_id
                   OR NEW.timeline_scope_key IS DISTINCT FROM OLD.timeline_scope_key
                   OR NEW.lock_version <> OLD.lock_version + 1
                   OR NOT EXISTS (
                       SELECT 1 FROM character_state_versions AS version
                       JOIN character_state_commits AS commit_row
                         ON commit_row.to_state_version_id = version.id
                       WHERE version.id = NEW.state_version_id
                         AND version.project_id = NEW.project_id
                         AND version.character_id = NEW.character_id
                         AND version.timeline_scope_key = NEW.timeline_scope_key
                         AND version.version = NEW.lock_version
                         AND commit_row.from_state_version_id = OLD.state_version_id
                         AND commit_row.expected_head_version = OLD.lock_version
                   ) THEN
                    RAISE EXCEPTION 'character state head update requires a fresh commit'
                    USING ERRCODE = '40001';
                END IF;
            END IF;
            RETURN NEW;
        END; $$""",
        """CREATE OR REPLACE TRIGGER trg_character_identity_boundary
        BEFORE INSERT OR UPDATE OF id, current_identity_version_id, canonical_facts ON characters
        FOR EACH ROW EXECUTE FUNCTION enforce_character_identity_boundary()""",
    )
    for statement in postgres_statements:
        event.listen(anchor, "after_create", DDL(statement).execute_if(dialect="postgresql"))
    for table_name in protected_tables:
        event.listen(
            anchor,
            "after_create",
            DDL(
                f"CREATE OR REPLACE TRIGGER trg_{table_name}_immutable "
                f"BEFORE UPDATE OR DELETE ON {table_name} "
                "FOR EACH ROW EXECUTE FUNCTION enforce_character_state_append_only()"
            ).execute_if(dialect="postgresql"),
        )
    for table_name in (
        "character_state_versions",
        "character_state_deltas",
        "character_state_validations",
        "character_state_commits",
    ):
        event.listen(
            anchor,
            "after_create",
            DDL(
                f"CREATE OR REPLACE TRIGGER trg_{table_name}_consistency BEFORE INSERT ON {table_name} "
                "FOR EACH ROW EXECUTE FUNCTION enforce_character_state_consistency()"
            ).execute_if(dialect="postgresql"),
        )
    event.listen(
        anchor,
        "after_create",
        DDL(
            "CREATE OR REPLACE TRIGGER trg_character_state_heads_consistency "
            "BEFORE INSERT OR UPDATE OR DELETE "
            "ON character_state_heads FOR EACH ROW EXECUTE FUNCTION enforce_character_state_consistency()"
        ).execute_if(dialect="postgresql"),
    )


_install_character_state_integrity_ddl()


def _install_project_style_integrity_ddl() -> None:
    """Keep style descriptors, locks, and QA evidence immutable and project-scoped."""

    anchor = CandidateStyleEvaluation.__table__
    sqlite_statements = (
        """CREATE TRIGGER IF NOT EXISTS trg_style_embeddings_consistency
        BEFORE INSERT ON style_embeddings WHEN NOT EXISTS (
            SELECT 1 FROM asset_versions AS version
            JOIN assets AS asset ON asset.id = version.asset_id
            WHERE version.id = NEW.asset_version_id
              AND asset.project_id = NEW.project_id
              AND asset.asset_type = 'STYLE'
        ) BEGIN SELECT RAISE(ABORT, 'style embedding must belong to a STYLE version in the project'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_project_style_locks_consistency
        BEFORE INSERT ON project_style_locks WHEN NOT EXISTS (
            SELECT 1 FROM projects AS project
            JOIN assets AS asset ON asset.project_id = project.id
            JOIN asset_versions AS version
              ON version.id = NEW.style_version_id AND version.asset_id = asset.id
            JOIN style_embeddings AS embedding
              ON embedding.id = NEW.style_embedding_id
             AND embedding.asset_version_id = version.id
             AND embedding.project_id = project.id
            WHERE project.id = NEW.project_id
              AND asset.id = NEW.style_asset_id
              AND asset.asset_type = 'STYLE'
              AND asset.canonical_version_id = version.id
              AND version.status = 'READY'
        ) BEGIN
          SELECT RAISE(ABORT, 'project style lock requires a canonical STYLE version and embedding');
        END""",
        """CREATE TRIGGER IF NOT EXISTS trg_projects_style_lock_update
        BEFORE UPDATE OF canonical_style_version_id ON projects
        WHEN NOT (NEW.canonical_style_version_id IS OLD.canonical_style_version_id) AND (
            OLD.canonical_style_version_id IS NOT NULL
            OR NEW.canonical_style_version_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM project_style_locks AS style_lock
                WHERE style_lock.project_id = NEW.id
                  AND style_lock.style_version_id = NEW.canonical_style_version_id
                  AND style_lock.created_at >= OLD.updated_at
            )
        ) BEGIN
          SELECT RAISE(ABORT, 'project style can only be locked once through a fresh style lock');
        END""",
        """CREATE TRIGGER IF NOT EXISTS trg_candidate_style_evaluations_consistency
        BEFORE INSERT ON candidate_style_evaluations WHEN NOT EXISTS (
            SELECT 1 FROM generation_candidates AS candidate
            JOIN shots AS shot ON shot.id = candidate.shot_id
            JOIN scenes AS scene ON scene.id = shot.scene_id
            JOIN episodes AS episode ON episode.id = scene.episode_id
            JOIN media_assets AS output ON output.id = NEW.output_asset_id
            JOIN project_style_locks AS style_lock ON style_lock.id = NEW.style_lock_id
            WHERE candidate.id = NEW.candidate_id
              AND candidate.output_asset_id = NEW.output_asset_id
              AND episode.project_id = NEW.project_id
              AND output.project_id = NEW.project_id
              AND style_lock.project_id = NEW.project_id
              AND style_lock.style_version_id = NEW.style_version_id
              AND style_lock.style_embedding_id = NEW.style_embedding_id
        ) BEGIN SELECT RAISE(ABORT, 'candidate style evaluation provenance is inconsistent'); END""",
    )
    for statement in sqlite_statements:
        event.listen(anchor, "after_create", DDL(statement).execute_if(dialect="sqlite"))
    for table_name in (
        "style_embeddings",
        "project_style_locks",
        "candidate_style_evaluations",
    ):
        for operation in ("UPDATE", "DELETE"):
            event.listen(
                anchor,
                "after_create",
                DDL(
                    f"CREATE TRIGGER IF NOT EXISTS trg_{table_name}_append_only_{operation.lower()} "
                    f"BEFORE {operation} ON {table_name} "
                    f"BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END"
                ).execute_if(dialect="sqlite"),
            )

    postgres_statements = (
        """CREATE OR REPLACE FUNCTION enforce_project_style_consistency()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME = 'style_embeddings' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM asset_versions AS version
                    JOIN assets AS asset ON asset.id = version.asset_id
                    WHERE version.id = NEW.asset_version_id
                      AND asset.project_id = NEW.project_id
                      AND asset.asset_type = 'STYLE'
                ) THEN
                    RAISE EXCEPTION 'style embedding must belong to a STYLE version in the project'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'project_style_locks' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM projects AS project
                    JOIN assets AS asset ON asset.project_id = project.id
                    JOIN asset_versions AS version
                      ON version.id = NEW.style_version_id AND version.asset_id = asset.id
                    JOIN style_embeddings AS embedding
                      ON embedding.id = NEW.style_embedding_id
                     AND embedding.asset_version_id = version.id
                     AND embedding.project_id = project.id
                    WHERE project.id = NEW.project_id
                      AND asset.id = NEW.style_asset_id
                      AND asset.asset_type = 'STYLE'
                      AND asset.canonical_version_id = version.id
                      AND version.status = 'READY'
                ) THEN
                    RAISE EXCEPTION 'project style lock requires a canonical STYLE version and embedding'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'projects' THEN
                IF NEW.canonical_style_version_id IS DISTINCT FROM OLD.canonical_style_version_id AND (
                    OLD.canonical_style_version_id IS NOT NULL
                    OR NEW.canonical_style_version_id IS NULL
                    OR NOT EXISTS (
                        SELECT 1 FROM project_style_locks AS style_lock
                        WHERE style_lock.project_id = NEW.id
                          AND style_lock.style_version_id = NEW.canonical_style_version_id
                          AND style_lock.created_at >= OLD.updated_at
                    )
                ) THEN
                    RAISE EXCEPTION 'project style can only be locked once through a fresh style lock'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'candidate_style_evaluations' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM generation_candidates AS candidate
                    JOIN shots AS shot ON shot.id = candidate.shot_id
                    JOIN scenes AS scene ON scene.id = shot.scene_id
                    JOIN episodes AS episode ON episode.id = scene.episode_id
                    JOIN media_assets AS output ON output.id = NEW.output_asset_id
                    JOIN project_style_locks AS style_lock ON style_lock.id = NEW.style_lock_id
                    WHERE candidate.id = NEW.candidate_id
                      AND candidate.output_asset_id = NEW.output_asset_id
                      AND episode.project_id = NEW.project_id
                      AND output.project_id = NEW.project_id
                      AND style_lock.project_id = NEW.project_id
                      AND style_lock.style_version_id = NEW.style_version_id
                      AND style_lock.style_embedding_id = NEW.style_embedding_id
                ) THEN RAISE EXCEPTION 'candidate style evaluation provenance is inconsistent'
                    USING ERRCODE = '23514'; END IF;
            END IF;
            RETURN NEW;
        END; $$""",
        """CREATE OR REPLACE FUNCTION enforce_project_style_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '%% is append-only', TG_TABLE_NAME USING ERRCODE = '23000';
            RETURN OLD;
        END; $$""",
        """CREATE TRIGGER trg_style_embeddings_consistency BEFORE INSERT ON style_embeddings
        FOR EACH ROW EXECUTE FUNCTION enforce_project_style_consistency()""",
        """CREATE TRIGGER trg_project_style_locks_consistency BEFORE INSERT ON project_style_locks
        FOR EACH ROW EXECUTE FUNCTION enforce_project_style_consistency()""",
        """CREATE TRIGGER trg_projects_style_lock_update
        BEFORE UPDATE OF canonical_style_version_id ON projects
        FOR EACH ROW EXECUTE FUNCTION enforce_project_style_consistency()""",
        """CREATE TRIGGER trg_candidate_style_evaluations_consistency
        BEFORE INSERT ON candidate_style_evaluations
        FOR EACH ROW EXECUTE FUNCTION enforce_project_style_consistency()""",
    )
    for statement in postgres_statements:
        event.listen(anchor, "after_create", DDL(statement).execute_if(dialect="postgresql"))
    for table_name in (
        "style_embeddings",
        "project_style_locks",
        "candidate_style_evaluations",
    ):
        event.listen(
            anchor,
            "after_create",
            DDL(
                f"CREATE TRIGGER trg_{table_name}_append_only BEFORE UPDATE OR DELETE ON {table_name} "
                "FOR EACH ROW EXECUTE FUNCTION enforce_project_style_append_only()"
            ).execute_if(dialect="postgresql"),
        )


_install_project_style_integrity_ddl()


def _install_payment_ledger_integrity_ddl() -> None:
    """Prevent authenticated webhook receipts and posted credit entries from being rewritten."""

    # Anchored to the metadata, not to one table: these triggers name three
    # different tables and the PostgreSQL ones share a function, so hanging
    # them off a single table's `after_create` makes their success depend on
    # creation order. Metadata-level `after_create` runs once, after every
    # table exists.
    anchor = Base.metadata
    # `depay_webhook_deliveries` belongs here too. Migration 0032 has guarded it
    # on a migrated database since it was created, but this list did not, so a
    # schema built from ORM metadata — which is what the test suite runs on —
    # silently allowed writes production refuses. That drift hid a payment-path
    # bug that only failed once it reached production.
    for table_name in (
        "alchemy_webhook_deliveries",
        "depay_webhook_deliveries",
        "xunhupay_settlements",
        "workspace_credit_ledger_entries",
    ):
        for operation in ("UPDATE", "DELETE"):
            event.listen(
                anchor,
                "after_create",
                DDL(
                    f"CREATE TRIGGER IF NOT EXISTS trg_{table_name}_append_only_{operation.lower()} "
                    f"BEFORE {operation} ON {table_name} "
                    f"BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END"
                ).execute_if(dialect="sqlite"),
            )

    event.listen(
        anchor,
        "after_create",
        DDL(
            "CREATE OR REPLACE FUNCTION enforce_payment_ledger_append_only() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            # `%%` because SQLAlchemy's DDL construct percent-interpolates its
            # statement; a single `%` makes this raise TypeError at create time
            # rather than installing the trigger. The two identical guards in
            # this module already escape it.
            "RAISE EXCEPTION '%% is append-only', TG_TABLE_NAME USING ERRCODE = '23000'; "
            "RETURN OLD; END; $$"
        ).execute_if(dialect="postgresql"),
    )
    for table_name in (
        "alchemy_webhook_deliveries",
        "depay_webhook_deliveries",
        "xunhupay_settlements",
        "workspace_credit_ledger_entries",
    ):
        event.listen(
            anchor,
            "after_create",
            DDL(
                # OR REPLACE, like every other metadata-level trigger in this module:
                # a metadata `after_create` fires on every create_all, including one
                # against an already-built throwaway schema, and PostgreSQL refuses a
                # second plain CREATE TRIGGER with DuplicateObject.
                f"CREATE OR REPLACE TRIGGER trg_{table_name}_append_only "
                f"BEFORE UPDATE OR DELETE ON {table_name} "
                "FOR EACH ROW EXECUTE FUNCTION enforce_payment_ledger_append_only()"
            ).execute_if(dialect="postgresql"),
        )


_install_payment_ledger_integrity_ddl()
