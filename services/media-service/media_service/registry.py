from __future__ import annotations

import io
import subprocess
from typing import BinaryIO

import httpx
from PIL import Image
from platform_database import Database
from platform_shared import StorageProvider
from production_domain.models import MediaAsset, MediaProviderBinding, utcnow
from provider_sdk import GenerationProvider
from sqlalchemy import select


class MediaRegistry:
    def __init__(self, database: Database, storage: StorageProvider):
        self.database = database
        self.storage = storage

    @staticmethod
    def _image_dimensions(path: str, mime_type: str) -> tuple[int | None, int | None]:
        if not mime_type.startswith("image/"):
            return None, None
        try:
            with Image.open(path) as image:
                return image.width, image.height
        except Exception:
            return None, None

    @staticmethod
    def _video_duration(path: str, mime_type: str) -> float | None:
        if not mime_type.startswith("video/"):
            return None
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=nw=1:nk=1",
                    path,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            return float(result.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def register(
        self,
        project_id: str,
        asset_type: str,
        stream: BinaryIO,
        *,
        filename: str,
        mime_type: str | None = None,
        character_id: str | None = None,
        scene_id: str | None = None,
        shot_id: str | None = None,
        parent_asset_id: str | None = None,
        generation_candidate_id: str | None = None,
        metadata: dict | None = None,
    ) -> tuple[MediaAsset, bool]:
        stored = self.storage.put(stream, filename=filename, mime_type=mime_type)
        with self.database.session() as session:
            existing = session.scalar(
                select(MediaAsset).where(
                    MediaAsset.project_id == project_id,
                    MediaAsset.sha256 == stored.sha256,
                    MediaAsset.asset_type == asset_type,
                )
            )
            if existing:
                return existing, True
            width, height = self._image_dimensions(stored.local_path, stored.mime_type)
            asset = MediaAsset(
                project_id=project_id,
                asset_type=asset_type,
                sha256=stored.sha256,
                storage_key=stored.key,
                local_path=stored.local_path,
                public_url=stored.public_url,
                mime_type=stored.mime_type,
                width=width,
                height=height,
                duration=self._video_duration(stored.local_path, stored.mime_type),
                character_id=character_id,
                scene_id=scene_id,
                shot_id=shot_id,
                parent_asset_id=parent_asset_id,
                generation_candidate_id=generation_candidate_id,
                metadata_json=metadata or {},
            )
            session.add(asset)
            session.flush()
            return asset, False

    def get(self, asset_id: str) -> MediaAsset | None:
        with self.database.session() as session:
            return session.get(MediaAsset, asset_id)

    async def resolve_provider_media(
        self, asset_id: str, provider: GenerationProvider, *, account_id: str, worker_id: str
    ) -> tuple[str, bool]:
        with self.database.session() as session:
            asset = session.get(MediaAsset, asset_id)
            if asset is None:
                raise LookupError(f"media asset not found: {asset_id}")
            binding = session.scalar(
                select(MediaProviderBinding).where(
                    MediaProviderBinding.asset_id == asset_id,
                    MediaProviderBinding.provider == provider.name,
                    MediaProviderBinding.account_id == account_id,
                )
            )
            local_path = asset.local_path
            mime_type = asset.mime_type
            storage_key = asset.storage_key
            binding_id = binding.id if binding else None
            existing_media_id = binding.provider_media_id if binding else None
        if existing_media_id:
            if await provider.validate_asset(existing_media_id, account_id=account_id, worker_id=worker_id):
                with self.database.session() as session:
                    current = session.get(MediaProviderBinding, binding_id)
                    if current:
                        current.last_validated_at = utcnow()
                        current.status = "READY"
                return existing_media_id, True
        path = local_path or str(self.storage.path_for(storage_key))
        provider_media_id = await provider.upload_asset(
            {"asset_id": asset_id, "local_path": path, "mime_type": mime_type},
            account_id=account_id,
            worker_id=worker_id,
        )
        with self.database.session() as session:
            current = session.scalar(
                select(MediaProviderBinding).where(
                    MediaProviderBinding.asset_id == asset_id,
                    MediaProviderBinding.provider == provider.name,
                    MediaProviderBinding.account_id == account_id,
                )
            )
            if current:
                current.provider_media_id = provider_media_id
                current.status = "READY"
                current.last_validated_at = utcnow()
            else:
                session.add(
                    MediaProviderBinding(
                        asset_id=asset_id,
                        provider=provider.name,
                        account_id=account_id,
                        provider_media_id=provider_media_id,
                        last_validated_at=utcnow(),
                    )
                )
            asset = session.get(MediaAsset, asset_id)
            if asset:
                asset.provider = provider.name
                asset.provider_media_id = provider_media_id
        return provider_media_id, False

    async def download_and_register(
        self,
        project_id: str,
        asset_type: str,
        url: str,
        *,
        filename: str,
        provider: str,
        provider_media_id: str,
        shot_id: str | None = None,
        generation_candidate_id: str | None = None,
    ) -> MediaAsset:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            content = response.content
            mime_type = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
        asset, _ = self.register(
            project_id,
            asset_type,
            io.BytesIO(content),
            filename=filename,
            mime_type=mime_type,
            shot_id=shot_id,
            generation_candidate_id=generation_candidate_id,
            metadata={"source_url": url, "provider": provider},
        )
        with self.database.session() as session:
            current = session.get(MediaAsset, asset.id)
            current.provider = provider
            current.provider_media_id = provider_media_id
            session.flush()
            return current
