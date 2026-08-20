from __future__ import annotations

import hashlib
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from platform_shared import LocalStorage, StorageLimitExceeded


class _SynchronizedStream:
    def __init__(self, payload: bytes, ready: threading.Barrier, written: threading.Barrier):
        self.payload = payload
        self.ready = ready
        self.written = written
        self._reads = 0

    def read(self, _size: int = -1) -> bytes:
        self._reads += 1
        if self._reads == 1:
            self.ready.wait(timeout=3)
            return self.payload
        if self._reads == 2:
            self.written.wait(timeout=3)
        return b""


def test_same_filename_concurrent_uploads_are_isolated(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    payloads = [b"tenant-a" * 4096, b"tenant-b" * 4096]
    ready = threading.Barrier(2)
    written = threading.Barrier(2)

    def upload(payload: bytes):  # type: ignore[no-untyped-def]
        return storage.put(
            _SynchronizedStream(payload, ready, written),  # type: ignore[arg-type]
            filename="image.png",
            mime_type="image/png",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        stored = list(pool.map(upload, payloads))

    for payload, item in zip(payloads, stored, strict=True):
        assert item.sha256 == hashlib.sha256(payload).hexdigest()
        assert Path(item.local_path).read_bytes() == payload
    assert stored[0].key != stored[1].key
    assert not list(tmp_path.glob(".upload-*.tmp"))


def test_oversized_upload_is_rejected_and_temporary_file_is_removed(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path, max_object_bytes=8)

    with pytest.raises(StorageLimitExceeded, match="8-byte"):
        storage.put(io.BytesIO(b"123456789"), filename="too-large.png")

    assert not list(tmp_path.glob(".upload-*.tmp"))
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]
