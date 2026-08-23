"""Direct-to-storage upload: the write half of keeping the API out of the media path.

Reads already bypass this service. These pin the writes:

- the client PUTs to object storage and this process never sees the body;
- what the server decided (project, asset type, key) cannot be retargeted by the
  client between authorizing and completing;
- size comes from the store, not from the client's claim;
- the digest is enforced *by the store*, which is the only reason a
  client-declared SHA-256 is safe to content-address a key with;
- validation reads a bounded header, never the whole object.
"""

from __future__ import annotations

import hashlib
import io

import pytest
from media_service import (
    DirectUploadConflict,
    DirectUploadNotFinished,
    DirectUploadService,
    DirectUploadUnsupported,
)
from media_service.direct_upload import _SHA256_LENGTH
from PIL import Image, ImageDraw
from platform_shared import (
    MEDIA_HEADER_BYTES,
    PresignedUpload,
    StoredObjectStat,
    UnsafeMediaUpload,
    validate_direct_upload_header,
)


def _png(width: int = 320, height: int = 240) -> bytes:
    image = Image.new("RGB", (width, height), (30, 90, 160))
    ImageDraw.Draw(image).ellipse((10, 10, width - 10, height - 10), fill=(220, 80, 40))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class FakeObjectStore:
    """Stands in for S3, and counts what the API reads.

    The point of the counters: a direct upload should cost this process one
    `HEAD` and one bounded `GET`, never a full-object read.
    """

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.presigned: list[str] = []
        self.head_calls = 0
        self.prefix_reads: list[int] = []
        self.enforce_checksum = True

    def presigned_upload(self, key, *, sha256, mime_type, expires_in=900):  # type: ignore[no-untyped-def]
        self.presigned.append(key)
        return PresignedUpload(
            url=f"https://bucket.example/{key}?signature=abc",
            method="PUT",
            headers={"Content-Type": mime_type, "x-amz-checksum-sha256": sha256},
            storage_key=key,
            expires_in=expires_in,
        )

    def client_put(self, key: str, payload: bytes, mime_type: str, *, declared_sha: str) -> bool:
        """The client's own transfer. Returns False when the store rejects it."""

        if self.enforce_checksum and hashlib.sha256(payload).hexdigest() != declared_sha:
            return False
        self.objects[key] = (payload, mime_type)
        return True

    def stat(self, key):  # type: ignore[no-untyped-def]
        self.head_calls += 1
        entry = self.objects.get(key)
        return None if entry is None else StoredObjectStat(size=len(entry[0]), mime_type=entry[1])

    def read_prefix(self, key, length):  # type: ignore[no-untyped-def]
        self.prefix_reads.append(length)
        entry = self.objects.get(key)
        return b"" if entry is None else entry[0][:length]

    def open(self, key, mode="rb"):  # type: ignore[no-untyped-def]
        raise AssertionError("a direct upload must never read the whole object through the API")

    def put(self, stream, *, filename, mime_type=None):  # type: ignore[no-untyped-def]
        raise AssertionError("a direct upload must never stream bytes through the API")


@pytest.fixture
def store() -> FakeObjectStore:
    return FakeObjectStore()


@pytest.fixture
def uploads(container, store):  # type: ignore[no-untyped-def]
    return DirectUploadService(
        container.database,
        store,
        max_upload_bytes=64 * 1024 * 1024,
        max_image_pixels=50_000_000,
    )


def _authorize(uploads, project_id, payload, **overrides):  # type: ignore[no-untyped-def]
    request = {
        "project_id": project_id,
        "workspace_id": None,
        "created_by_user_id": None,
        "asset_type": "CHARACTER_REFERENCE",
        "filename": "plate.png",
        "mime_type": "image/png",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "idempotency_key": "upload-1",
    }
    request.update(overrides)
    return uploads.authorize(**request)


# --- 1. The bytes never pass through this process ----------------------------


def test_the_api_never_reads_the_object_body(container, project, uploads, store) -> None:  # type: ignore[no-untyped-def]
    payload = _png()
    authorized = _authorize(uploads, project.id, payload)

    # The client's transfer. `FakeObjectStore.open`/`put` assert if the service
    # ever tries to move these bytes itself.
    assert store.client_put(
        authorized.presigned.storage_key,
        payload,
        "image/png",
        declared_sha=hashlib.sha256(payload).hexdigest(),
    )

    upload = uploads.pending(authorized.upload_id)
    size, mime_type = uploads.verify_object(upload)

    assert size == len(payload)
    assert mime_type == "image/png"
    # One HEAD and one bounded range read. Never the object.
    assert store.head_calls == 1
    assert store.prefix_reads == [MEDIA_HEADER_BYTES]


def test_a_local_disk_backend_refuses_rather_than_inventing_a_url(container, project) -> None:  # type: ignore[no-untyped-def]
    service = DirectUploadService(
        container.database,
        container.storage,
        max_upload_bytes=1024 * 1024,
        max_image_pixels=50_000_000,
    )
    with pytest.raises(DirectUploadUnsupported, match="multipart upload endpoint"):
        _authorize(service, project.id, _png())


# --- 2. The store enforces the digest ---------------------------------------


def test_the_presigned_put_carries_the_checksum_the_store_will_enforce(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    uploads,  # type: ignore[no-untyped-def]
) -> None:
    payload = _png()
    digest = hashlib.sha256(payload).hexdigest()
    authorized = _authorize(uploads, project.id, payload)

    assert authorized.presigned.headers["x-amz-checksum-sha256"] == digest
    # Content-addressed, which is only safe because the store rejects a mismatch.
    assert authorized.presigned.storage_key.startswith(f"{digest[:2]}/{digest}")


def test_bytes_that_do_not_match_the_declared_digest_never_land(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    uploads,  # type: ignore[no-untyped-def]
) -> None:
    """Without this the key would name content the object does not contain."""

    payload = _png()
    authorized = _authorize(uploads, project.id, payload)

    rejected = store_put_mismatch(authorized, uploads)
    assert rejected is False

    upload = uploads.pending(authorized.upload_id)
    with pytest.raises(DirectUploadNotFinished, match="not present in storage"):
        uploads.verify_object(upload)


def store_put_mismatch(authorized, uploads):  # type: ignore[no-untyped-def]
    other = _png(64, 64)
    return uploads.storage.client_put(
        authorized.presigned.storage_key,
        other,
        "image/png",
        declared_sha=authorized.presigned.headers["x-amz-checksum-sha256"],
    )


@pytest.mark.parametrize("digest", ["", "abc", "z" * _SHA256_LENGTH])
def test_a_malformed_digest_is_rejected_before_anything_is_authorized(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    uploads,  # type: ignore[no-untyped-def]
    digest: str,
) -> None:
    with pytest.raises(ValueError, match="64 hexadecimal"):
        _authorize(uploads, project.id, _png(), sha256=digest)


# --- 3. Size and type come from the store, not the client -------------------


def test_the_declared_size_is_not_trusted_at_completion(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    uploads,  # type: ignore[no-untyped-def]
    store,  # type: ignore[no-untyped-def]
) -> None:
    payload = _png()
    authorized = _authorize(uploads, project.id, payload, size_bytes=10)
    store.client_put(
        authorized.presigned.storage_key,
        payload,
        "image/png",
        declared_sha=hashlib.sha256(payload).hexdigest(),
    )

    upload = uploads.pending(authorized.upload_id)
    size, _mime = uploads.verify_object(upload)

    assert upload.declared_size_bytes == 10
    assert size == len(payload), "completion must use the store's size, not the claim"


def test_an_object_that_was_never_uploaded_cannot_be_completed(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    uploads,  # type: ignore[no-untyped-def]
) -> None:
    authorized = _authorize(uploads, project.id, _png())
    upload = uploads.pending(authorized.upload_id)
    with pytest.raises(DirectUploadNotFinished):
        uploads.verify_object(upload)


def test_content_that_lies_about_its_format_is_rejected_from_the_header(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    uploads,  # type: ignore[no-untyped-def]
    store,  # type: ignore[no-untyped-def]
) -> None:
    payload = b"<html><body>not an image</body></html>" * 32
    authorized = _authorize(uploads, project.id, payload)
    store.client_put(
        authorized.presigned.storage_key,
        payload,
        "image/png",
        declared_sha=hashlib.sha256(payload).hexdigest(),
    )

    upload = uploads.pending(authorized.upload_id)
    with pytest.raises(UnsafeMediaUpload, match="does not match its declared format"):
        uploads.verify_object(upload)


# --- 4. The client cannot retarget an authorized upload ---------------------


def test_reusing_an_idempotency_key_for_different_content_is_refused(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    uploads,  # type: ignore[no-untyped-def]
) -> None:
    _authorize(uploads, project.id, _png())
    with pytest.raises(DirectUploadConflict, match="already used for a different upload"):
        _authorize(uploads, project.id, _png(64, 64))


def test_the_authorized_scope_is_server_held_not_client_supplied(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    uploads,  # type: ignore[no-untyped-def]
) -> None:
    """Completion carries only a row id, so nothing about the target can move."""

    payload = _png()
    authorized = _authorize(uploads, project.id, payload, asset_type="character_reference")
    upload = uploads.pending(authorized.upload_id)

    assert upload.project_id == project.id
    assert upload.asset_type == "CHARACTER_REFERENCE"
    assert upload.storage_key == authorized.presigned.storage_key
    assert upload.sha256 == hashlib.sha256(payload).hexdigest()


def test_an_authorization_replay_returns_the_same_upload(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    uploads,  # type: ignore[no-untyped-def]
) -> None:
    payload = _png()
    first = _authorize(uploads, project.id, payload)
    second = _authorize(uploads, project.id, payload)
    assert first.upload_id == second.upload_id


def test_content_the_project_already_holds_is_reported_so_the_transfer_can_be_skipped(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    uploads,  # type: ignore[no-untyped-def]
) -> None:
    payload = _png()
    existing = container.media.register(
        project.id,
        "CHARACTER_REFERENCE",
        io.BytesIO(payload),
        filename="plate.png",
        mime_type="image/png",
    )[0]

    authorized = _authorize(uploads, project.id, payload, idempotency_key="dedup-1")

    assert authorized.existing_asset_id == existing.id


# --- 5. Header validation, on its own ---------------------------------------


def test_header_validation_bounds_pixels_without_decoding_the_whole_image() -> None:
    payload = _png(40, 30)
    with pytest.raises(UnsafeMediaUpload, match="dimensions exceed"):
        validate_direct_upload_header(
            payload[:MEDIA_HEADER_BYTES],
            filename="plate.png",
            declared_mime="image/png",
            asset_type="IMAGE",
            size_bytes=len(payload),
            max_bytes=10**8,
            max_image_pixels=100,
        )


def test_header_validation_rejects_a_mime_that_disagrees_with_the_filename() -> None:
    payload = _png()
    with pytest.raises(UnsafeMediaUpload, match="does not match its filename"):
        validate_direct_upload_header(
            payload[:MEDIA_HEADER_BYTES],
            filename="plate.png",
            declared_mime="image/jpeg",
            asset_type="IMAGE",
            size_bytes=len(payload),
            max_bytes=10**8,
        )


def test_header_validation_rejects_an_image_for_a_video_only_asset_type() -> None:
    payload = _png()
    with pytest.raises(UnsafeMediaUpload, match="accepts video only"):
        validate_direct_upload_header(
            payload[:MEDIA_HEADER_BYTES],
            filename="plate.png",
            declared_mime="image/png",
            asset_type="VIDEO",
            size_bytes=len(payload),
            max_bytes=10**8,
        )


def test_header_validation_uses_the_store_reported_size_for_its_bound() -> None:
    from platform_shared import StorageLimitExceeded

    payload = _png()
    with pytest.raises(StorageLimitExceeded):
        validate_direct_upload_header(
            payload[:MEDIA_HEADER_BYTES],
            filename="plate.png",
            declared_mime="image/png",
            asset_type="IMAGE",
            size_bytes=10**9,
            max_bytes=1024,
        )


# --- 6. Adoption registers metadata, not bytes ------------------------------


def test_adopting_a_stored_object_records_it_without_moving_it(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
) -> None:
    payload = _png(200, 150)
    digest = hashlib.sha256(payload).hexdigest()
    key = f"{digest[:2]}/{digest}.png"
    container.storage.path_for(key).parent.mkdir(parents=True, exist_ok=True)
    container.storage.path_for(key).write_bytes(payload)

    asset, reused = container.media.adopt_stored_object(
        project.id,
        "CHARACTER_REFERENCE",
        key,
        sha256=digest,
        mime_type="image/png",
        size_bytes=len(payload),
    )

    assert reused is False
    assert asset.storage_key == key
    assert asset.sha256 == digest
    assert asset.size_bytes == len(payload)
    # Dimensions come from the header, not from a full decode.
    assert (asset.width, asset.height) == (200, 150)
    assert asset.metadata_json["source"] == "direct_upload"


# --- 7. The HTTP surface ----------------------------------------------------


def test_the_http_flow_authorizes_then_adopts_without_the_body(container, project, store) -> None:  # type: ignore[no-untyped-def]
    """The endpoints a client actually uses, with a store that refuses proxying."""

    import hashlib as _hashlib

    from fastapi.testclient import TestClient
    from video_platform_api.main import create_app

    payload = _png(400, 300)
    digest = _hashlib.sha256(payload).hexdigest()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store

    with TestClient(create_app(container)) as client:
        authorized = client.post(
            "/v1/assets/uploads",
            headers={"Idempotency-Key": "http-upload-1"},
            json={
                "project_id": project.id,
                "asset_type": "CHARACTER_REFERENCE",
                "filename": "plate.png",
                "mime_type": "image/png",
                "sha256": digest,
                "size_bytes": len(payload),
            },
        )
        assert authorized.status_code == 201, authorized.text
        issued = authorized.json()
        assert issued["method"] == "PUT"
        assert issued["headers"]["x-amz-checksum-sha256"] == digest
        assert issued["existing_asset_id"] is None

        # The transfer the client performs itself; nothing here goes near the API.
        assert store.client_put(issued["storage_key"], payload, "image/png", declared_sha=digest)

        completed = client.post(f"/v1/assets/uploads/{issued['upload_id']}/complete")
        assert completed.status_code == 200, completed.text
        body = completed.json()
        assert body["sha256"] == digest
        assert body["reused"] is False

        # Completing twice returns the same asset rather than a second one.
        replay = client.post(f"/v1/assets/uploads/{issued['upload_id']}/complete")
        assert replay.status_code == 200
        assert replay.json()["id"] == body["id"]
        assert replay.json()["reused"] is True

    asset = container.media.get(body["id"])
    assert asset is not None and (asset.width, asset.height) == (400, 300)


def test_completing_before_the_client_uploads_is_a_conflict(container, project, store) -> None:  # type: ignore[no-untyped-def]
    import hashlib as _hashlib

    from fastapi.testclient import TestClient
    from video_platform_api.main import create_app

    payload = _png()
    digest = _hashlib.sha256(payload).hexdigest()
    container.storage = store
    container.direct_uploads.storage = store

    with TestClient(create_app(container)) as client:
        issued = client.post(
            "/v1/assets/uploads",
            headers={"Idempotency-Key": "http-upload-2"},
            json={
                "project_id": project.id,
                "asset_type": "CHARACTER_REFERENCE",
                "filename": "plate.png",
                "mime_type": "image/png",
                "sha256": digest,
                "size_bytes": len(payload),
            },
        ).json()
        response = client.post(f"/v1/assets/uploads/{issued['upload_id']}/complete")

    assert response.status_code == 409
    assert "not present in storage" in response.json()["detail"]


def test_local_disk_reports_that_direct_upload_is_unavailable(container, project) -> None:  # type: ignore[no-untyped-def]
    """A deployment without object storage gets a clear answer, not a broken URL."""

    import hashlib as _hashlib

    from fastapi.testclient import TestClient
    from video_platform_api.main import create_app

    payload = _png()
    with TestClient(create_app(container)) as client:
        response = client.post(
            "/v1/assets/uploads",
            headers={"Idempotency-Key": "http-upload-3"},
            json={
                "project_id": project.id,
                "asset_type": "CHARACTER_REFERENCE",
                "filename": "plate.png",
                "mime_type": "image/png",
                "sha256": _hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            },
        )

    assert response.status_code == 501
    assert "multipart upload endpoint" in response.json()["detail"]
