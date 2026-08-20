from __future__ import annotations

import hashlib
import mimetypes
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast


@dataclass(frozen=True)
class StoredObject:
    key: str
    local_path: str
    public_url: str | None
    sha256: str
    size: int
    mime_type: str


class StorageLimitExceeded(ValueError):
    def __init__(self, limit_bytes: int):
        super().__init__(f"upload exceeds the {limit_bytes}-byte object limit")
        self.limit_bytes = limit_bytes


class StorageProvider(Protocol):
    def put(self, stream: BinaryIO, *, filename: str, mime_type: str | None = None) -> StoredObject: ...
    def open(self, key: str, mode: str = "rb") -> BinaryIO: ...
    def path_for(self, key: str) -> Path: ...


class LocalStorage:
    def __init__(
        self,
        root: Path,
        public_base_url: str = "",
        *,
        max_object_bytes: int = 100 * 1024 * 1024,
    ):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")
        self.max_object_bytes = max(1, max_object_bytes)

    def put(self, stream: BinaryIO, *, filename: str, mime_type: str | None = None) -> StoredObject:
        safe_name = Path(filename).name or "asset.bin"
        suffix = Path(safe_name).suffix.lower()
        digest = hashlib.sha256()
        size = 0
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.root,
                prefix=".upload-",
                suffix=".tmp",
                delete=False,
            ) as output:
                temporary = Path(output.name)
                while chunk := stream.read(1024 * 1024):
                    if size + len(chunk) > self.max_object_bytes:
                        raise StorageLimitExceeded(self.max_object_bytes)
                    digest.update(chunk)
                    size += len(chunk)
                    output.write(chunk)
        except BaseException:
            if temporary:
                temporary.unlink(missing_ok=True)
            raise
        assert temporary is not None
        sha = digest.hexdigest()
        key = f"{sha[:2]}/{sha}{suffix}"
        destination = self.path_for(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            temporary.unlink(missing_ok=True)
        else:
            temporary.replace(destination)
        actual_mime = mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        public_url = f"{self.public_base_url}/v1/storage/{key}" if self.public_base_url else None
        return StoredObject(key, str(destination), public_url, sha, size, actual_mime)

    def path_for(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if self.root not in candidate.parents:
            raise ValueError("invalid storage key")
        return candidate

    def open(self, key: str, mode: str = "rb") -> BinaryIO:
        return cast(BinaryIO, self.path_for(key).open(mode))


class S3CompatibleStorage:
    """S3/R2/MinIO-compatible storage with a content-addressed local processing cache."""

    def __init__(
        self,
        *,
        bucket: str,
        cache_root: Path,
        endpoint_url: str = "",
        region: str = "us-east-1",
        access_key_id: str = "",
        secret_access_key: str = "",
        public_base_url: str = "",
        max_object_bytes: int = 100 * 1024 * 1024,
    ):
        import boto3

        self.bucket = bucket
        self.cache_root = cache_root.expanduser().resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")
        self.max_object_bytes = max(1, max_object_bytes)
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region,
            aws_access_key_id=access_key_id or None,
            aws_secret_access_key=secret_access_key or None,
        )

    def put(self, stream: BinaryIO, *, filename: str, mime_type: str | None = None) -> StoredObject:
        safe_name = Path(filename).name or "asset.bin"
        suffix = Path(safe_name).suffix.lower()
        digest = hashlib.sha256()
        size = 0
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.cache_root, delete=False) as output:
                temporary = Path(output.name)
                while chunk := stream.read(1024 * 1024):
                    if size + len(chunk) > self.max_object_bytes:
                        raise StorageLimitExceeded(self.max_object_bytes)
                    digest.update(chunk)
                    size += len(chunk)
                    output.write(chunk)
        except BaseException:
            if temporary:
                temporary.unlink(missing_ok=True)
            raise
        assert temporary is not None
        sha = digest.hexdigest()
        key = f"{sha[:2]}/{sha}{suffix}"
        destination = self.path_for(key, download=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            temporary.unlink(missing_ok=True)
        else:
            temporary.replace(destination)
        actual_mime = mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        self.client.upload_file(str(destination), self.bucket, key, ExtraArgs={"ContentType": actual_mime})
        public_url = f"{self.public_base_url}/v1/storage/{key}" if self.public_base_url else None
        return StoredObject(key, str(destination), public_url, sha, size, actual_mime)

    def path_for(self, key: str, *, download: bool = True) -> Path:
        candidate = (self.cache_root / key).resolve()
        if self.cache_root not in candidate.parents:
            raise ValueError("invalid storage key")
        if download and not candidate.exists():
            candidate.parent.mkdir(parents=True, exist_ok=True)
            self.client.download_file(self.bucket, key, str(candidate))
        return candidate

    def open(self, key: str, mode: str = "rb") -> BinaryIO:
        return cast(BinaryIO, self.path_for(key).open(mode))
