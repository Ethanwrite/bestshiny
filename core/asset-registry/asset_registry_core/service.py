from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

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
from sqlalchemy import func, select


class AssetRegistryError(RuntimeError):
    pass


class CanonicalVersionNotSet(AssetRegistryError):
    pass


class AssetVersionNotPromotable(AssetRegistryError):
    pass


@dataclass(frozen=True)
class VersionMediaInput:
    media_asset_id: str
    role: str = "REFERENCE"
    sort_order: int = 0
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ResolvedMedia:
    media: MediaAsset
    role: str
    sort_order: int
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ResolvedAsset:
    asset: Asset
    version: AssetVersion
    primary_media: MediaAsset | None
    references: tuple[ResolvedMedia, ...]


class AssetRegistry:
    """Unified logical assets layered on top of the existing MediaAsset blob registry."""

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _kind(value: str | AssetKind) -> str:
        try:
            return value.value if isinstance(value, AssetKind) else AssetKind(value.upper()).value
        except ValueError as exc:
            allowed = ", ".join(kind.value for kind in AssetKind)
            raise ValueError(f"unsupported asset type {value!r}; expected one of: {allowed}") from exc

    @staticmethod
    def _version_status(value: str | AssetVersionStatus) -> str:
        try:
            return (
                value.value
                if isinstance(value, AssetVersionStatus)
                else AssetVersionStatus(value.upper()).value
            )
        except ValueError as exc:
            allowed = ", ".join(status.value for status in AssetVersionStatus)
            message = f"unsupported asset version status {value!r}; expected one of: {allowed}"
            raise ValueError(message) from exc

    def create(
        self,
        project_id: str,
        asset_type: str | AssetKind,
        name: str,
        *,
        description: str = "",
        canonical_metadata: Mapping[str, Any] | None = None,
        created_by_user_id: str | None = None,
    ) -> Asset:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("asset name must not be empty")
        kind = self._kind(asset_type)
        with self.database.session() as session:
            if not session.get(Project, project_id):
                raise LookupError("project not found")
            asset = Asset(
                project_id=project_id,
                asset_type=kind,
                name=clean_name,
                description=description.strip(),
                canonical_metadata=dict(canonical_metadata or {}),
                created_by_user_id=created_by_user_id,
            )
            session.add(asset)
            session.flush()
            return asset

    def add_version(
        self,
        asset_id: str,
        *,
        primary_media_asset_id: str | None = None,
        references: Iterable[VersionMediaInput] = (),
        label: str = "",
        metadata: Mapping[str, Any] | None = None,
        continuity_state: Mapping[str, Any] | None = None,
        embedding_refs: Iterable[Mapping[str, Any]] = (),
        source: str = "USER_UPLOAD",
        status: str | AssetVersionStatus = AssetVersionStatus.READY,
        parent_version_id: str | None = None,
        created_by_user_id: str | None = None,
    ) -> AssetVersion:
        reference_inputs = tuple(references)
        status_value = self._version_status(status)
        with self.database.session() as session:
            asset = session.scalar(select(Asset).where(Asset.id == asset_id).with_for_update())
            if not asset:
                raise LookupError("asset not found")
            primary = (
                session.get(MediaAsset, primary_media_asset_id)
                if primary_media_asset_id is not None
                else None
            )
            if primary_media_asset_id and not primary:
                raise LookupError("primary media asset not found")
            if primary and primary.project_id != asset.project_id:
                raise ValueError("primary media asset belongs to a different project")
            if parent_version_id:
                parent = session.get(AssetVersion, parent_version_id)
                if not parent or parent.asset_id != asset.id:
                    raise ValueError("parent version does not belong to this asset")

            seen: set[tuple[str, str]] = set()
            resolved_references: list[tuple[VersionMediaInput, MediaAsset, str]] = []
            for reference in reference_inputs:
                role = reference.role.strip().upper() or "REFERENCE"
                key = (reference.media_asset_id, role)
                if key in seen:
                    raise ValueError(f"duplicate media reference for role {role}")
                seen.add(key)
                media = session.get(MediaAsset, reference.media_asset_id)
                if not media:
                    raise LookupError(f"media asset not found: {reference.media_asset_id}")
                if media.project_id != asset.project_id:
                    raise ValueError("referenced media asset belongs to a different project")
                resolved_references.append((reference, media, role))

            next_version = (
                int(
                    session.scalar(
                        select(func.coalesce(func.max(AssetVersion.version), 0)).where(
                            AssetVersion.asset_id == asset.id
                        )
                    )
                    or 0
                )
                + 1
            )
            version = AssetVersion(
                asset_id=asset.id,
                version=next_version,
                label=label.strip(),
                primary_media_asset_id=primary_media_asset_id,
                parent_version_id=parent_version_id,
                metadata_json=dict(metadata or {}),
                continuity_state=dict(continuity_state or {}),
                embedding_refs=[dict(item) for item in embedding_refs],
                source=source.strip().upper() or "USER_UPLOAD",
                status=status_value,
                created_by_user_id=created_by_user_id,
            )
            session.add(version)
            session.flush()
            for reference, _media, role in resolved_references:
                session.add(
                    AssetVersionMedia(
                        asset_version_id=version.id,
                        media_asset_id=reference.media_asset_id,
                        role=role,
                        sort_order=reference.sort_order,
                        metadata_json=dict(reference.metadata or {}),
                    )
                )
            session.flush()
            # Deliberately do not update asset.canonical_version_id here. Generated or uploaded
            # versions become canonical only through promote().
            return version

    def promote(
        self,
        asset_id: str,
        version_id: str,
        *,
        promoted_by_user_id: str | None = None,
        reason: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> Asset:
        with self.database.session() as session:
            asset = session.scalar(select(Asset).where(Asset.id == asset_id).with_for_update())
            if not asset:
                raise LookupError("asset not found")
            version = session.get(AssetVersion, version_id)
            if not version or version.asset_id != asset.id:
                raise ValueError("version does not belong to this asset")
            if version.status != AssetVersionStatus.READY.value:
                raise AssetVersionNotPromotable("only READY asset versions can become canonical")
            if asset.canonical_version_id == version.id:
                return asset
            previous = asset.canonical_version_id
            session.add(
                AssetCanonicalPromotion(
                    asset_id=asset.id,
                    from_version_id=previous,
                    to_version_id=version.id,
                    promoted_by_user_id=promoted_by_user_id,
                    reason=reason.strip(),
                    metadata_json=dict(metadata or {}),
                )
            )
            # Persist the append-only audit entry first. The database trigger then
            # permits the canonical pointer update only when this exact transition
            # has a fresh, same-asset promotion record in the same transaction.
            session.flush()
            asset.canonical_version_id = version.id
            session.flush()
            return asset

    def resolve(self, asset_id: str, *, version_id: str | None = None) -> ResolvedAsset:
        with self.database.session() as session:
            asset = session.get(Asset, asset_id)
            if not asset:
                raise LookupError("asset not found")
            selected_version_id = version_id or asset.canonical_version_id
            if not selected_version_id:
                raise CanonicalVersionNotSet("asset has no canonical version; promote a version explicitly")
            version = session.get(AssetVersion, selected_version_id)
            if not version or version.asset_id != asset.id:
                raise LookupError("asset version not found")
            primary = (
                session.get(MediaAsset, version.primary_media_asset_id)
                if version.primary_media_asset_id
                else None
            )
            rows = list(
                session.scalars(
                    select(AssetVersionMedia)
                    .where(AssetVersionMedia.asset_version_id == version.id)
                    .order_by(
                        AssetVersionMedia.sort_order,
                        AssetVersionMedia.role,
                        AssetVersionMedia.id,
                    )
                )
            )
            references = tuple(
                ResolvedMedia(
                    media=session.get(MediaAsset, row.media_asset_id),
                    role=row.role,
                    sort_order=row.sort_order,
                    metadata=dict(row.metadata_json),
                )
                for row in rows
            )
            return ResolvedAsset(asset, version, primary, references)

    def list(
        self,
        project_id: str,
        *,
        asset_type: str | AssetKind | None = None,
        include_archived: bool = False,
    ) -> list[Asset]:
        query = select(Asset).where(Asset.project_id == project_id)
        if asset_type is not None:
            query = query.where(Asset.asset_type == self._kind(asset_type))
        if not include_archived:
            query = query.where(Asset.status == "ACTIVE")
        query = query.order_by(Asset.name, Asset.created_at, Asset.id)
        with self.database.session() as session:
            return list(session.scalars(query))
