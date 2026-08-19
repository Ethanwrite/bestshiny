from __future__ import annotations

import hashlib
import mimetypes
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


class StorageProvider(Protocol):
    def put(self, stream: BinaryIO, *, filename: str, mime_type: str | None = None) -> StoredObject: ...
    def open(self, key: str, mode: str = "rb") -> BinaryIO: ...
    def path_for(self, key: str) -> Path: ...


class LocalStorage:
    def __init__(self, root: Path, public_base_url: str = ""):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")

    def put(self, stream: BinaryIO, *, filename: str, mime_type: str | None = None) -> StoredObject:
        safe_name = Path(filename).name or "asset.bin"
        suffix = Path(safe_name).suffix.lower()
        temporary = self.root / f".upload-{hashlib.sha256(safe_name.encode()).hexdigest()[:12]}.tmp"
        digest = hashlib.sha256()
        size = 0
        with temporary.open("wb") as output:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                output.write(chunk)
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
