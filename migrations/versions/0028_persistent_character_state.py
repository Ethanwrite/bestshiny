"""Add persistent, validated, branch-aware character state history.

Revision ID: 0028_persistent_character_state
Revises: 0027_production_evidence_core
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_persistent_character_state"
down_revision: str | None = "0027_production_evidence_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CORE_TABLES = {
    "projects",
    "episodes",
    "scenes",
    "shots",
    "characters",
    "character_identity_versions",
    "generation_candidates",
    "timeline_states",
    "model_execution_records",
    "qa_results",
    "media_assets",
    "users",
}
CORE_ANCHORS = {"projects", "shots", "characters", "timeline_states"}
STATE_TABLES = (
    "character_state_versions",
    "character_state_deltas",
    "character_state_validations",
    "character_state_commits",
    "character_state_heads",
)
PROTECTED_TABLES = (
    "character_identity_versions",
    "character_state_versions",
    "character_state_deltas",
    "character_state_validations",
    "character_state_commits",
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _scalar(sql: str) -> object | None:
    return op.get_bind().execute(sa.text(sql)).scalar()


def _skip_recovery_or_require_complete_core() -> bool:
    tables = _tables()
    state_present = set(STATE_TABLES).intersection(tables)
    if not CORE_ANCHORS.intersection(tables) and not state_present:
        return True
    missing = CORE_TABLES.difference(tables)
    if missing:
        raise RuntimeError(f"Persistent character state migration requires missing tables: {sorted(missing)}")
    if state_present:
        raise RuntimeError(
            f"Persistent character state migration found partial pre-existing tables: {sorted(state_present)}"
        )
    return False


def _validate_existing_identity() -> None:
    if (
        _scalar(
            "SELECT c.id FROM characters AS c "
            "LEFT JOIN character_identity_versions AS identity "
            "ON identity.id = c.current_identity_version_id "
            "WHERE c.current_identity_version_id IS NOT NULL AND "
            "(identity.id IS NULL OR identity.character_id != c.id OR identity.status != 'LOCKED') LIMIT 1"
        )
        is not None
    ):
        raise RuntimeError(
            "Current character identity pointers must reference a locked version of that character"
        )
    if _scalar("SELECT id FROM character_identity_versions WHERE version <= 0 LIMIT 1") is not None:
        raise RuntimeError("Character identity versions must be positive")
    asset_columns = (
        "master_asset_id",
        "front_asset_id",
        "left_profile_asset_id",
        "right_profile_asset_id",
        "three_quarter_left_asset_id",
        "three_quarter_right_asset_id",
        "full_body_asset_id",
    )
    for column in asset_columns:
        if (
            _scalar(
                "SELECT identity.id FROM character_identity_versions AS identity "
                "JOIN characters AS character ON character.id = identity.character_id "
                f"JOIN media_assets AS asset ON asset.id = identity.{column} "
                f"WHERE identity.{column} IS NOT NULL AND asset.project_id != character.project_id LIMIT 1"
            )
            is not None
        ):
            raise RuntimeError("Character identity media must belong to the character project")


def upgrade() -> None:
    if _skip_recovery_or_require_complete_core():
        return
    _validate_existing_identity()
    with op.batch_alter_table("characters") as batch_op:
        batch_op.create_unique_constraint("uq_characters_id_project", ["id", "project_id"])
        batch_op.create_foreign_key(
            "fk_characters_current_identity",
            "character_identity_versions",
            ["current_identity_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("character_identity_versions") as batch_op:
        batch_op.create_unique_constraint("uq_character_identity_id_character", ["id", "character_id"])
        batch_op.create_check_constraint("ck_character_identity_version_positive", "version > 0")

    _create_versions()
    _create_deltas()
    _create_validations()
    _create_commits()
    _create_heads()
    _install_integrity_triggers()


def _timestamps() -> tuple[sa.Column, sa.Column]:  # type: ignore[type-arg]
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def _create_versions() -> None:
    op.create_table(
        "character_state_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("timeline_scope_key", sa.String(length=160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("previous_state_version_id", sa.String(length=36), nullable=True),
        sa.Column("identity_version_id", sa.String(length=36), nullable=False),
        sa.Column("source_shot_id", sa.String(length=36), nullable=True),
        sa.Column("source_candidate_id", sa.String(length=36), nullable=True),
        sa.Column("state_schema_version", sa.String(length=80), nullable=False),
        sa.Column("narrative_state_json", sa.JSON(), nullable=False),
        sa.Column("identity_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("previous_state_hash", sa.String(length=64), nullable=True),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("version > 0", name="ck_character_state_version_positive"),
        sa.CheckConstraint(
            "length(timeline_scope_key) > 0", name="ck_character_state_version_scope_nonempty"
        ),
        sa.CheckConstraint(
            "length(identity_fingerprint) = 64 AND length(state_hash) = 64",
            name="ck_character_state_version_hash_lengths",
        ),
        sa.CheckConstraint(
            "(previous_state_version_id IS NULL AND previous_state_hash IS NULL) OR "
            "(previous_state_version_id IS NOT NULL AND previous_state_hash IS NOT NULL "
            "AND length(previous_state_hash) = 64)",
            name="ck_character_state_version_previous_hash",
        ),
        sa.CheckConstraint(
            "previous_state_version_id IS NULL OR previous_state_version_id != id",
            name="ck_character_state_version_not_self_parent",
        ),
        sa.CheckConstraint(
            "(source_shot_id IS NULL AND source_candidate_id IS NULL) OR "
            "(source_shot_id IS NOT NULL AND source_candidate_id IS NOT NULL)",
            name="ck_character_state_version_source_pair",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["character_id", "project_id"],
            ["characters.id", "characters.project_id"],
            name="fk_character_state_version_character_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["previous_state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_version_previous_character",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["identity_version_id", "character_id"],
            ["character_identity_versions.id", "character_identity_versions.character_id"],
            name="fk_character_state_version_identity_character",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["source_shot_id"], ["shots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_candidate_id"], ["generation_candidates.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "character_id",
            "timeline_scope_key",
            "version",
            name="uq_character_state_version_scope_number",
        ),
        sa.UniqueConstraint("id", "character_id", name="uq_character_state_version_id_character"),
    )
    for column in (
        "project_id",
        "character_id",
        "previous_state_version_id",
        "identity_version_id",
        "source_shot_id",
        "source_candidate_id",
        "state_hash",
    ):
        op.create_index(f"ix_character_state_versions_{column}", "character_state_versions", [column])
    op.create_index(
        "ix_character_state_version_scope",
        "character_state_versions",
        ["project_id", "character_id", "timeline_scope_key", "version"],
    )


def _create_deltas() -> None:
    op.create_table(
        "character_state_deltas",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("timeline_scope_key", sa.String(length=160), nullable=False),
        sa.Column("shot_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("base_state_version_id", sa.String(length=36), nullable=True),
        sa.Column("identity_version_id", sa.String(length=36), nullable=False),
        sa.Column("input_timeline_state_id", sa.String(length=36), nullable=False),
        sa.Column("planned_output_timeline_state_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_revision", sa.Integer(), nullable=False),
        sa.Column("supersedes_delta_id", sa.String(length=36), nullable=True),
        sa.Column("proposal_kind", sa.String(length=40), nullable=False),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("patch_format", sa.String(length=40), nullable=False),
        sa.Column("patch_json", sa.JSON(), nullable=False),
        sa.Column("changed_paths_json", sa.JSON(), nullable=False),
        sa.Column("proposed_state_json", sa.JSON(), nullable=False),
        sa.Column("base_state_hash", sa.String(length=64), nullable=True),
        sa.Column("target_state_hash", sa.String(length=64), nullable=False),
        sa.Column("input_timeline_state_hash", sa.String(length=64), nullable=False),
        sa.Column("planned_output_timeline_state_hash", sa.String(length=64), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=False),
        sa.Column("state_schema_version", sa.String(length=80), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("model_execution_record_id", sa.String(length=36), nullable=True),
        sa.Column("proposed_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("proposal_revision > 0", name="ck_character_state_delta_revision_positive"),
        sa.CheckConstraint("target_version > 0", name="ck_character_state_delta_target_positive"),
        sa.CheckConstraint("length(timeline_scope_key) > 0", name="ck_character_state_delta_scope_nonempty"),
        sa.CheckConstraint("patch_format = 'JSON_PATCH_V1'", name="ck_character_state_delta_patch_format"),
        sa.CheckConstraint(
            "proposal_kind IN ('INITIALIZE', 'NARRATIVE', 'EVIDENCE_DERIVED', 'IDENTITY_REBASE')",
            name="ck_character_state_delta_proposal_kind",
        ),
        sa.CheckConstraint(
            "source_kind IN ('RULES', 'LLM', 'HUMAN', 'VISUAL_EVIDENCE')",
            name="ck_character_state_delta_source_kind",
        ),
        sa.CheckConstraint(
            "length(target_state_hash) = 64 AND length(input_timeline_state_hash) = 64 "
            "AND length(planned_output_timeline_state_hash) = 64",
            name="ck_character_state_delta_hash_lengths",
        ),
        sa.CheckConstraint(
            "(proposal_kind = 'INITIALIZE' AND base_state_version_id IS NULL "
            "AND base_state_hash IS NULL) OR "
            "(proposal_kind != 'INITIALIZE' AND base_state_version_id IS NOT NULL "
            "AND base_state_hash IS NOT NULL AND length(base_state_hash) = 64)",
            name="ck_character_state_delta_base_contract",
        ),
        sa.CheckConstraint(
            "supersedes_delta_id IS NULL OR supersedes_delta_id != id",
            name="ck_character_state_delta_not_self_supersede",
        ),
        sa.CheckConstraint(
            "source_kind NOT IN ('LLM', 'VISUAL_EVIDENCE') OR model_execution_record_id IS NOT NULL",
            name="ck_character_state_delta_model_provenance",
        ),
        sa.CheckConstraint(
            "source_kind != 'HUMAN' OR proposed_by_user_id IS NOT NULL",
            name="ck_character_state_delta_human_provenance",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["character_id", "project_id"],
            ["characters.id", "characters.project_id"],
            name="fk_character_state_delta_character_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["base_state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_delta_base_character",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["identity_version_id", "character_id"],
            ["character_identity_versions.id", "character_identity_versions.character_id"],
            name="fk_character_state_delta_identity_character",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["shot_id"], ["shots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["candidate_id"], ["generation_candidates.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["input_timeline_state_id"], ["timeline_states.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["planned_output_timeline_state_id"], ["timeline_states.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["supersedes_delta_id"], ["character_state_deltas.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["model_execution_record_id"], ["model_execution_records.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["proposed_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_character_state_delta_project_key"),
        sa.UniqueConstraint(
            "candidate_id",
            "character_id",
            "proposal_revision",
            name="uq_character_state_delta_candidate_revision",
        ),
    )
    for column in (
        "project_id",
        "character_id",
        "shot_id",
        "candidate_id",
        "base_state_version_id",
        "identity_version_id",
        "input_timeline_state_id",
        "planned_output_timeline_state_id",
        "supersedes_delta_id",
        "target_state_hash",
        "model_execution_record_id",
        "proposed_by_user_id",
    ):
        op.create_index(f"ix_character_state_deltas_{column}", "character_state_deltas", [column])
    op.create_index(
        "ix_character_state_delta_scope",
        "character_state_deltas",
        ["project_id", "character_id", "timeline_scope_key", "created_at"],
    )


def _create_validations() -> None:
    op.create_table(
        "character_state_validations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("state_delta_id", sa.String(length=36), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=40), nullable=False),
        sa.Column("validator_kind", sa.String(length=40), nullable=False),
        sa.Column("model_execution_record_id", sa.String(length=36), nullable=True),
        sa.Column("qa_result_id", sa.String(length=36), nullable=True),
        sa.Column("evidence_asset_id", sa.String(length=36), nullable=True),
        sa.Column("validated_target_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_state_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("violations_json", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("validated_by_user_id", sa.String(length=36), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("attempt > 0", name="ck_character_state_validation_attempt_positive"),
        sa.CheckConstraint(
            "stage IN ('POLICY', 'VISUAL', 'HUMAN_OVERRIDE')",
            name="ck_character_state_validation_stage",
        ),
        sa.CheckConstraint(
            "decision IN ('PASS', 'REJECT', 'REVIEW_REQUIRED')",
            name="ck_character_state_validation_decision",
        ),
        sa.CheckConstraint(
            "validator_kind IN ('RULE_ENGINE', 'VLM', 'HUMAN')",
            name="ck_character_state_validation_validator",
        ),
        sa.CheckConstraint(
            "length(validated_target_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_character_state_validation_hash_lengths",
        ),
        sa.CheckConstraint(
            "validator_kind != 'VLM' OR model_execution_record_id IS NOT NULL",
            name="ck_character_state_validation_model_provenance",
        ),
        sa.CheckConstraint(
            "validator_kind != 'HUMAN' OR validated_by_user_id IS NOT NULL",
            name="ck_character_state_validation_human_provenance",
        ),
        sa.CheckConstraint(
            "stage != 'POLICY' OR validator_kind = 'RULE_ENGINE'",
            name="ck_character_state_validation_policy_rules",
        ),
        sa.CheckConstraint(
            "stage != 'HUMAN_OVERRIDE' OR validator_kind = 'HUMAN'",
            name="ck_character_state_validation_override_human",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["state_delta_id"], ["character_state_deltas.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["model_execution_record_id"], ["model_execution_records.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["qa_result_id"], ["qa_results.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["evidence_asset_id"], ["media_assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["validated_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "state_delta_id",
            "stage",
            "attempt",
            name="uq_character_state_validation_stage_attempt",
        ),
    )
    for column in (
        "project_id",
        "state_delta_id",
        "decision",
        "model_execution_record_id",
        "qa_result_id",
        "evidence_asset_id",
        "validated_by_user_id",
    ):
        op.create_index(
            f"ix_character_state_validations_{column}",
            "character_state_validations",
            [column],
        )
    op.create_index(
        "ix_character_state_validation_delta",
        "character_state_validations",
        ["state_delta_id", "stage", "created_at"],
    )


def _create_commits() -> None:
    op.create_table(
        "character_state_commits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("timeline_scope_key", sa.String(length=160), nullable=False),
        sa.Column("shot_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("state_delta_id", sa.String(length=36), nullable=False),
        sa.Column("from_state_version_id", sa.String(length=36), nullable=True),
        sa.Column("to_state_version_id", sa.String(length=36), nullable=False),
        sa.Column("policy_validation_id", sa.String(length=36), nullable=False),
        sa.Column("visual_validation_id", sa.String(length=36), nullable=False),
        sa.Column("human_validation_id", sa.String(length=36), nullable=True),
        sa.Column("expected_head_version", sa.Integer(), nullable=False),
        sa.Column("commit_actor", sa.String(length=40), nullable=False),
        sa.Column("committed_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("commit_hash", sa.String(length=64), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("expected_head_version >= 0", name="ck_character_state_commit_head_nonnegative"),
        sa.CheckConstraint(
            "from_state_version_id IS NULL OR from_state_version_id != to_state_version_id",
            name="ck_character_state_commit_distinct_versions",
        ),
        sa.CheckConstraint("length(commit_hash) = 64", name="ck_character_state_commit_hash_length"),
        sa.CheckConstraint("length(trim(reason)) > 0", name="ck_character_state_commit_reason_nonempty"),
        sa.CheckConstraint("commit_actor IN ('SYSTEM', 'HUMAN')", name="ck_character_state_commit_actor"),
        sa.CheckConstraint(
            "commit_actor != 'HUMAN' OR committed_by_user_id IS NOT NULL",
            name="ck_character_state_commit_human_provenance",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["character_id", "project_id"],
            ["characters.id", "characters.project_id"],
            name="fk_character_state_commit_character_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["from_state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_commit_from_character",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_commit_to_character",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["shot_id"], ["shots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["candidate_id"], ["generation_candidates.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["state_delta_id"], ["character_state_deltas.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["policy_validation_id"], ["character_state_validations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["visual_validation_id"], ["character_state_validations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["human_validation_id"], ["character_state_validations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["committed_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_delta_id", name="uq_character_state_commit_delta"),
        sa.UniqueConstraint("to_state_version_id", name="uq_character_state_commit_to_version"),
        sa.UniqueConstraint(
            "candidate_id", "character_id", name="uq_character_state_commit_candidate_character"
        ),
    )
    for column in (
        "project_id",
        "character_id",
        "shot_id",
        "candidate_id",
        "state_delta_id",
        "from_state_version_id",
        "committed_by_user_id",
    ):
        op.create_index(f"ix_character_state_commits_{column}", "character_state_commits", [column])
    op.create_index(
        "ix_character_state_commit_scope",
        "character_state_commits",
        ["project_id", "character_id", "timeline_scope_key", "created_at"],
    )


def _create_heads() -> None:
    op.create_table(
        "character_state_heads",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("timeline_scope_key", sa.String(length=160), nullable=False),
        sa.Column("state_version_id", sa.String(length=36), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("lock_version > 0", name="ck_character_state_head_version_positive"),
        sa.CheckConstraint("length(timeline_scope_key) > 0", name="ck_character_state_head_scope_nonempty"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["character_id", "project_id"],
            ["characters.id", "characters.project_id"],
            name="fk_character_state_head_character_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_head_version_character",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "character_id", "timeline_scope_key", name="uq_character_state_head_scope"
        ),
    )
    op.create_index("ix_character_state_heads_project_id", "character_state_heads", ["project_id"])
    op.create_index("ix_character_state_heads_character_id", "character_state_heads", ["character_id"])
    op.create_index(
        "ix_character_state_head_scope",
        "character_state_heads",
        ["project_id", "character_id", "timeline_scope_key"],
    )


def _install_integrity_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table_name in PROTECTED_TABLES:
            for operation in ("UPDATE", "DELETE"):
                trigger_name = f"trg_{table_name}_immutable_{operation.lower()}"
                op.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {trigger_name} BEFORE {operation} "
                    f"ON {table_name} BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END"
                )
        for statement in SQLITE_INTEGRITY_TRIGGERS:
            op.execute(statement)
        return
    if dialect != "postgresql":
        raise RuntimeError(f"unsupported persistent-character-state database dialect: {dialect}")
    for statement in POSTGRES_INTEGRITY_FUNCTIONS:
        op.execute(statement)
    op.execute(
        "CREATE TRIGGER trg_character_identity_boundary "
        "BEFORE INSERT OR UPDATE OF id, current_identity_version_id, canonical_facts ON characters "
        "FOR EACH ROW EXECUTE FUNCTION enforce_character_identity_boundary()"
    )
    for table_name in PROTECTED_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table_name}_immutable BEFORE UPDATE OR DELETE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION enforce_character_state_append_only()"
        )
    for table_name in STATE_TABLES[:4]:
        op.execute(
            f"CREATE TRIGGER trg_{table_name}_consistency BEFORE INSERT ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION enforce_character_state_consistency()"
        )
    op.execute(
        "CREATE TRIGGER trg_character_state_heads_consistency BEFORE INSERT OR UPDATE OR DELETE "
        "ON character_state_heads FOR EACH ROW EXECUTE FUNCTION enforce_character_state_consistency()"
    )


SQLITE_INTEGRITY_TRIGGERS = (
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
               OR json_extract(patch.value, '$.path') NOT LIKE '/%'
               OR json_extract(patch.value, '$.path') = '/appearance'
               OR json_extract(patch.value, '$.path') = '/appearance/outfit'
               OR json_extract(patch.value, '$.path') = '/identity'
               OR json_extract(patch.value, '$.path') LIKE '/identity/%'
               OR json_extract(patch.value, '$.path') = '/canonical_identity'
               OR json_extract(patch.value, '$.path') LIKE '/canonical_identity/%'
               OR json_extract(patch.value, '$.path') = '/face'
               OR json_extract(patch.value, '$.path') LIKE '/face/%'
               OR json_extract(patch.value, '$.path') = '/body_proportions'
               OR json_extract(patch.value, '$.path') LIKE '/body_proportions/%'
               OR json_extract(patch.value, '$.path') = '/canonical_hair'
               OR json_extract(patch.value, '$.path') LIKE '/canonical_hair/%'
               OR json_extract(patch.value, '$.path') = '/canonical_outfit'
               OR json_extract(patch.value, '$.path') LIKE '/canonical_outfit/%'
               OR json_extract(patch.value, '$.path') = '/identity_embedding_id'
               OR json_extract(patch.value, '$.path') LIKE '/identity_embedding_id/%'
               OR json_extract(patch.value, '$.path') = '/canonical_asset_id'
               OR json_extract(patch.value, '$.path') LIKE '/canonical_asset_id/%'
               OR json_extract(patch.value, '$.path') = '/appearance/face'
               OR json_extract(patch.value, '$.path') LIKE '/appearance/face/%'
               OR json_extract(patch.value, '$.path') = '/appearance/hair'
               OR json_extract(patch.value, '$.path') LIKE '/appearance/hair/%'
               OR json_extract(patch.value, '$.path') = '/appearance/body'
               OR json_extract(patch.value, '$.path') LIKE '/appearance/body/%'
               OR json_extract(patch.value, '$.path') = '/appearance/body_proportions'
               OR json_extract(patch.value, '$.path') LIKE '/appearance/body_proportions/%'
               OR json_extract(patch.value, '$.path') = '/appearance/canonical_hair'
               OR json_extract(patch.value, '$.path') LIKE '/appearance/canonical_hair/%'
               OR json_extract(patch.value, '$.path') = '/appearance/canonical_outfit'
               OR json_extract(patch.value, '$.path') LIKE '/appearance/canonical_outfit/%'
               OR json_extract(patch.value, '$.path') = '/appearance/outfit/type'
               OR json_extract(patch.value, '$.path') LIKE '/appearance/outfit/type/%'
               OR json_extract(patch.value, '$.path') = '/appearance/outfit/design'
               OR json_extract(patch.value, '$.path') LIKE '/appearance/outfit/design/%'
               OR json_extract(patch.value, '$.path') = '/appearance/outfit/color'
               OR json_extract(patch.value, '$.path') LIKE '/appearance/outfit/color/%'
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


POSTGRES_INTEGRITY_FUNCTIONS = (
    """CREATE OR REPLACE FUNCTION enforce_character_state_append_only()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '23000';
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
)


def _drop_integrity_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_character_state_heads_consistency ON character_state_heads")
        for table_name in reversed(STATE_TABLES[:4]):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_consistency ON {table_name}")
        for table_name in reversed(PROTECTED_TABLES):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}")
        op.execute("DROP TRIGGER IF EXISTS trg_character_identity_boundary ON characters")
        op.execute("DROP FUNCTION IF EXISTS enforce_character_state_consistency()")
        op.execute("DROP FUNCTION IF EXISTS enforce_character_identity_boundary()")
        op.execute("DROP FUNCTION IF EXISTS enforce_character_state_append_only()")
        return
    trigger_names = [
        "trg_character_state_head_delete",
        "trg_character_state_head_update",
        "trg_character_state_head_insert",
        "trg_character_state_commit_consistency",
        "trg_character_state_validation_consistency",
        "trg_character_state_delta_consistency",
        "trg_character_state_version_consistency",
        "trg_character_canonical_facts_frozen",
        "trg_character_identity_pointer_update",
        "trg_character_identity_pointer_insert",
    ]
    trigger_names.extend(
        f"trg_{table_name}_immutable_{operation}"
        for table_name in PROTECTED_TABLES
        for operation in ("delete", "update")
    )
    for trigger_name in trigger_names:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")


def downgrade() -> None:
    tables = _tables()
    if not CORE_ANCHORS.intersection(tables) and not set(STATE_TABLES).intersection(tables):
        return
    missing = set(STATE_TABLES).difference(tables)
    if missing:
        raise RuntimeError(f"Persistent character state downgrade requires missing tables: {sorted(missing)}")
    for table_name in STATE_TABLES:
        if _scalar(f'SELECT 1 FROM "{table_name}" LIMIT 1') is not None:
            raise RuntimeError(
                "Persistent character state downgrade would discard audit records from " + table_name
            )
    _drop_integrity_triggers()
    for table_name in reversed(STATE_TABLES):
        op.drop_table(table_name)
    with op.batch_alter_table("character_identity_versions") as batch_op:
        batch_op.drop_constraint("ck_character_identity_version_positive", type_="check")
        batch_op.drop_constraint("uq_character_identity_id_character", type_="unique")
    with op.batch_alter_table("characters") as batch_op:
        batch_op.drop_constraint("fk_characters_current_identity", type_="foreignkey")
        batch_op.drop_constraint("uq_characters_id_project", type_="unique")
