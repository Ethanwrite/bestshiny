from __future__ import annotations

import io
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from asset_registry_core import (
    AssetRegistry,
    AssetVersionNotPromotable,
    CanonicalVersionNotSet,
    VersionMediaInput,
)
from platform_database import Database
from production_domain.models import (
    Asset,
    AssetCanonicalPromotion,
    AssetKind,
    AssetVersion,
    AssetVersionMedia,
    AssetVersionStatus,
    MediaAsset,
    Project,
)
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]


def _media(container, project_id: str, content: bytes, name: str):
    return container.media.register(
        project_id,
        "REFERENCE",
        io.BytesIO(content),
        filename=name,
        mime_type="image/png",
    )[0]


def test_versions_never_become_canonical_without_explicit_promotion(container, project):
    registry = AssetRegistry(container.database)
    master = _media(container, project.id, b"character-master", "master.png")
    profile = _media(container, project.id, b"character-profile", "profile.png")
    asset = registry.create(
        project.id,
        AssetKind.CHARACTER,
        "Lin Jin",
        canonical_metadata={"identity": "approved facts"},
    )
    version_one = registry.add_version(
        asset.id,
        primary_media_asset_id=master.id,
        references=[VersionMediaInput(profile.id, "left_profile", metadata={"angle": 90})],
        continuity_state={"wardrobe": "blue delivery jacket"},
    )

    assert version_one.version == 1
    with pytest.raises(CanonicalVersionNotSet):
        registry.resolve(asset.id)

    promoted = registry.promote(asset.id, version_one.id, reason="User approved the identity sheet")
    assert promoted.canonical_version_id == version_one.id
    resolved = registry.resolve(asset.id)
    assert resolved.version.id == version_one.id
    assert resolved.primary_media.id == master.id
    assert [(item.role, item.media.id) for item in resolved.references] == [("LEFT_PROFILE", profile.id)]

    generated = _media(container, project.id, b"generated-variant", "generated.png")
    version_two = registry.add_version(
        asset.id,
        primary_media_asset_id=generated.id,
        parent_version_id=version_one.id,
        source="GENERATED",
    )
    assert version_two.version == 2
    assert registry.resolve(asset.id).version.id == version_one.id
    assert registry.resolve(asset.id, version_id=version_two.id).version.id == version_two.id

    registry.promote(asset.id, version_two.id, reason="Explicitly selected replacement")
    assert registry.resolve(asset.id).version.id == version_two.id
    with container.database.session() as session:
        assert (
            session.scalar(
                select(func.count(AssetCanonicalPromotion.id)).where(
                    AssetCanonicalPromotion.asset_id == asset.id
                )
            )
            == 2
        )


def test_rejected_version_cannot_be_promoted(container, project):
    registry = AssetRegistry(container.database)
    asset = registry.create(project.id, "PRODUCT", "Serum bottle")
    version = registry.add_version(asset.id, status=AssetVersionStatus.REJECTED)

    with pytest.raises(AssetVersionNotPromotable):
        registry.promote(asset.id, version.id)
    with container.database.session() as session:
        assert session.get(Asset, asset.id).canonical_version_id is None


def test_low_trust_generated_media_cannot_be_laundered_through_user_upload(
    container,
    project,
):
    registry = AssetRegistry(container.database)
    generated = _media(container, project.id, b"runapi-temporary-result", "temporary.png")
    with container.database.session() as session:
        session.get(MediaAsset, generated.id).provider = "runapi"
    character = registry.create(project.id, "CHARACTER", "Temporary edge draft")
    # The browser is allowed to retain a non-canonical draft, but changing the
    # version source to USER_UPLOAD cannot erase immutable media provenance.
    version = registry.add_version(
        character.id,
        primary_media_asset_id=generated.id,
        source="USER_UPLOAD",
    )

    with pytest.raises(AssetVersionNotPromotable, match="low-trust generated media"):
        registry.promote(character.id, version.id, reason="must remain temporary")
    with container.database.session() as session:
        assert session.get(Asset, character.id).canonical_version_id is None


def test_incomplete_generated_media_provenance_fails_closed(container, project):
    registry = AssetRegistry(container.database)
    generated = _media(container, project.id, b"incomplete-provider-origin", "incomplete.png")
    with container.database.session() as session:
        session.get(MediaAsset, generated.id).provider_media_id = "orphaned-provider-media-id"
    character = registry.create(project.id, "CHARACTER", "Incomplete provenance draft")
    version = registry.add_version(character.id, primary_media_asset_id=generated.id)

    with pytest.raises(AssetVersionNotPromotable, match="incomplete provider provenance"):
        registry.promote(character.id, version.id)
    with container.database.session() as session:
        assert session.get(Asset, character.id).canonical_version_id is None


def test_registry_rejects_cross_project_media_and_filters_logical_assets(container, project):
    registry = AssetRegistry(container.database)
    with container.database.session() as session:
        other_project = Project(title="Other project")
        session.add(other_project)
        session.flush()
        other_project_id = other_project.id
    foreign_media = _media(container, other_project_id, b"foreign", "foreign.png")
    character = registry.create(project.id, "character", "Lin Jin")
    scene = registry.create(project.id, "scene", "Hotel canopy")

    with pytest.raises(ValueError, match="different project"):
        registry.add_version(character.id, primary_media_asset_id=foreign_media.id)
    assert [item.id for item in registry.list(project.id, asset_type="CHARACTER")] == [character.id]

    with container.database.session() as session:
        session.get(Asset, scene.id).status = "ARCHIVED"
    assert [item.id for item in registry.list(project.id)] == [character.id]
    assert {item.id for item in registry.list(project.id, include_archived=True)} == {
        character.id,
        scene.id,
    }


def test_asset_version_numbers_are_scoped_to_each_logical_asset(container, project):
    registry = AssetRegistry(container.database)
    first = registry.create(project.id, "PROP", "Phone")
    second = registry.create(project.id, "WARDROBE", "Delivery jacket")

    assert registry.add_version(first.id).version == 1
    assert registry.add_version(first.id).version == 2
    assert registry.add_version(second.id).version == 1
    with container.database.session() as session:
        assert (
            session.scalar(select(func.count(AssetVersion.id)).where(AssetVersion.asset_id == first.id)) == 2
        )


def test_database_rejects_cross_asset_lineage_and_unlogged_canonical_changes(container, project):
    # App startup may call create_all against an already-initialized local DB.
    container.database.create_all()
    registry = AssetRegistry(container.database)
    first = registry.create(project.id, "CHARACTER", "First actor")
    second = registry.create(project.id, "CHARACTER", "Second actor")
    first_version = registry.add_version(first.id)
    second_version = registry.add_version(second.id)

    with pytest.raises(IntegrityError):
        with container.database.session() as session:
            session.execute(
                update(Asset).where(Asset.id == first.id).values(canonical_version_id=second_version.id)
            )
    with pytest.raises(IntegrityError):
        with container.database.session() as session:
            session.execute(
                update(Asset).where(Asset.id == first.id).values(canonical_version_id=first_version.id)
            )
    with pytest.raises(IntegrityError):
        with container.database.session() as session:
            session.add(
                AssetVersion(
                    asset_id=first.id,
                    version=2,
                    parent_version_id=second_version.id,
                )
            )
            session.flush()
    with pytest.raises(IntegrityError):
        with container.database.session() as session:
            session.add(
                AssetCanonicalPromotion(
                    asset_id=first.id,
                    to_version_id=second_version.id,
                    reason="invalid cross-asset transition",
                )
            )
            session.flush()

    assert registry.promote(first.id, first_version.id).canonical_version_id == first_version.id


def test_versions_media_and_promotion_history_are_database_append_only(container, project):
    registry = AssetRegistry(container.database)
    media = _media(container, project.id, b"immutable-master", "immutable.png")
    asset = registry.create(project.id, "PRODUCT", "Immutable bottle")
    version = registry.add_version(
        asset.id,
        primary_media_asset_id=media.id,
        references=[VersionMediaInput(media.id, "DETAIL")],
    )
    registry.promote(asset.id, version.id, reason="Approved")

    with container.database.session() as session:
        promotion_id = session.scalar(
            select(AssetCanonicalPromotion.id).where(AssetCanonicalPromotion.asset_id == asset.id)
        )
        version_media_id = session.scalar(
            select(AssetVersionMedia.id).where(AssetVersionMedia.asset_version_id == version.id)
        )

    protected_statements = (
        update(AssetVersion).where(AssetVersion.id == version.id).values(label="mutated"),
        delete(AssetVersion).where(AssetVersion.id == version.id),
        update(AssetVersionMedia).where(AssetVersionMedia.id == version_media_id).values(role="MUTATED"),
        delete(AssetCanonicalPromotion).where(AssetCanonicalPromotion.id == promotion_id),
    )
    for statement in protected_statements:
        with pytest.raises(IntegrityError):
            with container.database.session() as session:
                session.execute(statement)

    with container.database.session() as session:
        assert session.get(AssetVersion, version.id).label == ""
        assert session.get(AssetCanonicalPromotion, promotion_id) is not None


def test_0008_installs_and_reverses_sqlite_integrity_triggers(tmp_path, monkeypatch):
    database_path = tmp_path / "asset-registry-migration.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))

    command.upgrade(config, "head")
    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        installed = set(
            connection.scalars(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_asset%'")
            )
        )
    assert {
        "trg_assets_canonical_same_asset_update",
        "trg_assets_canonical_requires_promotion_update",
        "trg_asset_versions_append_only_update",
        "trg_asset_canonical_promotions_append_only_delete",
    }.issubset(installed)

    database = Database(database_url)
    registry = AssetRegistry(database)
    with database.session() as session:
        migrated_project = Project(title="Migrated project")
        session.add(migrated_project)
        session.flush()
        project_id = migrated_project.id
    first = registry.create(project_id, "CHARACTER", "First")
    second = registry.create(project_id, "CHARACTER", "Second")
    registry.add_version(first.id)
    second_version = registry.add_version(second.id)
    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.execute(
                update(Asset).where(Asset.id == first.id).values(canonical_version_id=second_version.id)
            )

    command.downgrade(config, "0007_commercial_auth")
    with engine.connect() as connection:
        remaining = set(
            connection.scalars(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_asset%'")
            )
        )
    assert not remaining
    command.upgrade(config, "head")


def test_0008_skips_assetless_recovery_snapshot_but_rejects_partial_registry(tmp_path, monkeypatch):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))

    assetless_path = tmp_path / "assetless-recovery.db"
    assetless_url = f"sqlite:///{assetless_path}"
    assetless_engine = sa.create_engine(assetless_url)
    with assetless_engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        connection.execute(
            sa.text("INSERT INTO alembic_version (version_num) VALUES ('0007_commercial_auth')")
        )
    monkeypatch.setenv("DATABASE_URL", assetless_url)
    command.upgrade(config, "head")
    current_head = ScriptDirectory.from_config(config).get_current_head()
    with assetless_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == current_head
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_asset%'"
                )
            )
            == 0
        )
    assetless_engine.dispose()

    partial_path = tmp_path / "partial-asset-registry.db"
    partial_url = f"sqlite:///{partial_path}"
    partial_engine = sa.create_engine(partial_url)
    with partial_engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        connection.execute(
            sa.text("INSERT INTO alembic_version (version_num) VALUES ('0007_commercial_auth')")
        )
        connection.execute(sa.text("CREATE TABLE assets (id VARCHAR(36) PRIMARY KEY)"))
    monkeypatch.setenv("DATABASE_URL", partial_url)
    with pytest.raises(RuntimeError, match="requires missing tables"):
        command.upgrade(config, "head")
    partial_engine.dispose()
