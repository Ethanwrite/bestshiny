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
import time

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
        self.full_hash_reads = 0
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

    def content_sha256(self, key, *, max_bytes):  # type: ignore[no-untyped-def]
        self.full_hash_reads += 1
        entry = self.objects.get(key)
        if entry is None or len(entry[0]) > max_bytes:
            return None
        return hashlib.sha256(entry[0]).hexdigest()

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


def test_completion_hashes_the_object_when_the_store_does_not_enforce_sha256(
    container,
    project,
    store,  # type: ignore[no-untyped-def]
) -> None:
    payload = _png()
    service = DirectUploadService(
        container.database,
        store,
        max_upload_bytes=64 * 1024 * 1024,
        max_image_pixels=50_000_000,
        verify_sha256_on_complete=True,
    )
    authorized = _authorize(service, project.id, payload)
    store.enforce_checksum = False
    assert store.client_put(
        authorized.presigned.storage_key,
        _png(64, 64),
        "image/png",
        declared_sha=hashlib.sha256(payload).hexdigest(),
    )

    with pytest.raises(DirectUploadNotFinished, match="SHA-256 does not match"):
        service.verify_object(service.pending(authorized.upload_id))
    assert store.full_hash_reads == 1


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


# --- 8. The reservation a failed authorization must not keep ----------------
#
# Nothing is in the bucket until the client PUTs, and it cannot PUT without the
# presigned URL. A hold that outlives a failed authorization is therefore
# capacity no upload will ever use — and because WorkspaceStorageQuota reads a
# RESERVED row as "already in progress", the same Idempotency-Key can never be
# retried either. That combination made every 501 on a local-disk deployment
# burn a workspace's quota permanently.


def _workspace_client(container, email: str, *, raise_server_exceptions: bool = True):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient
    from video_platform_api.main import create_app

    container.settings.auth_required = True
    client = TestClient(create_app(container), raise_server_exceptions=raise_server_exceptions)
    client.__enter__()
    registered = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "correct horse battery staple",
            "display_name": "Upload Owner",
        },
    )
    assert registered.status_code == 201, registered.text
    issued = registered.json()
    headers = {"Authorization": f"Bearer {issued['access_token']}"}
    project = client.post("/v1/projects", headers=headers, json={"title": "Uploads"}).json()
    return client, headers, project["id"], issued["user"]["workspaces"][0]["id"]


def _authorize_request(project_id: str, payload: bytes) -> dict:
    return {
        "project_id": project_id,
        "asset_type": "CHARACTER_REFERENCE",
        "filename": "plate.png",
        "mime_type": "image/png",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _quota(container, workspace_id: str) -> tuple[int, int, list[str]]:  # type: ignore[no-untyped-def]
    from production_domain.models import StorageReservation, Workspace
    from sqlalchemy import select

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        reservations = list(
            session.scalars(select(StorageReservation).where(StorageReservation.workspace_id == workspace_id))
        )
        assert workspace is not None
        return (
            workspace.reserved_storage_bytes,
            workspace.used_storage_bytes,
            [item.status for item in reservations],
        )


def test_an_authorization_that_fails_does_not_keep_the_workspace_hold(container) -> None:  # type: ignore[no-untyped-def]
    """Local disk cannot presign, and a 501 must not cost the workspace capacity."""

    payload = _png()
    client, headers, project_id, workspace_id = _workspace_client(container, "hold-501@example.com")
    try:
        refused = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "hold-501"},
            json=_authorize_request(project_id, payload),
        )
        assert refused.status_code == 501, refused.text

        reserved, used, statuses = _quota(container, workspace_id)
        assert (reserved, used) == (0, 0)
        assert statuses == ["RELEASED"]

        # The key itself stays spent — same rule as the multipart path — but the
        # client is told what to do instead of being met with a hold that never
        # clears.
        retried = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "hold-501"},
            json=_authorize_request(project_id, payload),
        )
        assert retried.status_code == 409
        assert "submit a new key" in retried.json()["detail"]
        assert _quota(container, workspace_id)[:2] == (0, 0)

        # A fresh key is not blocked by the earlier failure.
        assert (
            client.post(
                "/v1/assets/uploads",
                headers={**headers, "Idempotency-Key": "hold-501-retry"},
                json=_authorize_request(project_id, payload),
            ).status_code
            == 501
        )
        assert _quota(container, workspace_id)[:2] == (0, 0)
    finally:
        client.__exit__(None, None, None)


def test_re_authorizing_replays_the_upload_instead_of_holding_capacity_twice(
    container,  # type: ignore[no-untyped-def]
    store,  # type: ignore[no-untyped-def]
) -> None:
    """A lost response is a replay of one upload, not a second one."""

    payload = _png()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store
    client, headers, project_id, workspace_id = _workspace_client(container, "replay@example.com")
    try:
        first = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "replay-1"},
            json=_authorize_request(project_id, payload),
        )
        assert first.status_code == 201, first.text
        second = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "replay-1"},
            json=_authorize_request(project_id, payload),
        )
        assert second.status_code == 201, second.text
        assert second.json()["upload_id"] == first.json()["upload_id"]
        assert second.json()["storage_key"] == first.json()["storage_key"]

        # One upload, one hold — never two.
        reserved, used, statuses = _quota(container, workspace_id)
        assert (reserved, used) == (len(payload), 0)
        assert statuses == ["RESERVED"]

        # The replayed URL never outlives the deadline the response reports.
        assert second.json()["expires_at"] == first.json()["expires_at"]
    finally:
        client.__exit__(None, None, None)


def test_completing_before_the_put_lands_leaves_the_session_usable(container, store) -> None:  # type: ignore[no-untyped-def]
    """Polling early must not kill a presigned URL that still works."""

    payload = _png()
    digest = hashlib.sha256(payload).hexdigest()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store
    client, headers, project_id, workspace_id = _workspace_client(container, "early@example.com")
    try:
        issued = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "early-1"},
            json=_authorize_request(project_id, payload),
        ).json()

        early = client.post(f"/v1/assets/uploads/{issued['upload_id']}/complete", headers=headers)
        assert early.status_code == 409
        assert "not present in storage" in early.json()["detail"]

        # The window is still open, so the hold and the row both survive.
        reserved, _used, statuses = _quota(container, workspace_id)
        assert (reserved, statuses) == (len(payload), ["RESERVED"])

        # The transfer the client was still running when it polled.
        assert store.client_put(issued["storage_key"], payload, "image/png", declared_sha=digest)
        completed = client.post(f"/v1/assets/uploads/{issued['upload_id']}/complete", headers=headers)
        assert completed.status_code == 200, completed.text
        assert completed.json()["sha256"] == digest

        reserved, used, statuses = _quota(container, workspace_id)
        assert (reserved, used, statuses) == (0, len(payload), ["SETTLED"])
    finally:
        client.__exit__(None, None, None)


def test_a_closed_window_reclaims_its_hold_instead_of_holding_it_forever(
    container,  # type: ignore[no-untyped-def]
    store,  # type: ignore[no-untyped-def]
) -> None:
    """An expired session can never complete, so its capacity is not reserved."""

    from datetime import UTC, datetime, timedelta

    from production_domain.models import DirectUpload, DirectUploadStatus

    payload = _png()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store
    client, headers, project_id, workspace_id = _workspace_client(container, "expired@example.com")
    try:
        issued = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "expired-1"},
            json=_authorize_request(project_id, payload),
        ).json()
        with container.database.session() as session:
            row = session.get(DirectUpload, issued["upload_id"])
            row.expires_at = datetime.now(UTC) - timedelta(minutes=1)

        # Completing an expired session that never received bytes is terminal.
        stale = client.post(f"/v1/assets/uploads/{issued['upload_id']}/complete", headers=headers)
        assert stale.status_code == 409

        reserved, used, statuses = _quota(container, workspace_id)
        assert (reserved, used, statuses) == (0, 0, ["RELEASED"])
        with container.database.session() as session:
            row = session.get(DirectUpload, issued["upload_id"])
            assert row.status == DirectUploadStatus.ABANDONED.value
    finally:
        client.__exit__(None, None, None)


def test_re_authorizing_a_closed_window_reclaims_it_and_asks_for_a_new_key(
    container,  # type: ignore[no-untyped-def]
    store,  # type: ignore[no-untyped-def]
) -> None:
    payload = _png()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store
    from datetime import UTC, datetime, timedelta

    from production_domain.models import DirectUpload, DirectUploadStatus

    client, headers, project_id, workspace_id = _workspace_client(container, "reauth@example.com")
    try:
        issued = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "reauth-1"},
            json=_authorize_request(project_id, payload),
        ).json()
        with container.database.session() as session:
            session.get(DirectUpload, issued["upload_id"]).expires_at = datetime.now(UTC) - timedelta(
                seconds=1
            )

        again = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "reauth-1"},
            json=_authorize_request(project_id, payload),
        )
        assert again.status_code == 409
        assert "new Idempotency-Key" in again.json()["detail"]

        reserved, used, statuses = _quota(container, workspace_id)
        assert (reserved, used, statuses) == (0, 0, ["RELEASED"])
        with container.database.session() as session:
            row = session.get(DirectUpload, issued["upload_id"])
            assert row.status == DirectUploadStatus.ABANDONED.value
    finally:
        client.__exit__(None, None, None)


# --- 9. One owner per completion, one transaction ---------------------------


def test_only_one_completion_settles_the_hold(container, store) -> None:  # type: ignore[no-untyped-def]
    """Two completions of one upload must not settle it twice, or settle zero.

    The loser of the adopt race is told `reused=True`, which settles zero bytes.
    If it settled first, the winner's settlement was swallowed as a replay and
    the workspace never accounted for the object at all.
    """

    payload = _png()
    digest = hashlib.sha256(payload).hexdigest()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store
    client, headers, project_id, workspace_id = _workspace_client(container, "one-owner@example.com")
    try:
        issued = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "one-owner-1"},
            json=_authorize_request(project_id, payload),
        ).json()
        assert store.client_put(issued["storage_key"], payload, "image/png", declared_sha=digest)

        first = client.post(f"/v1/assets/uploads/{issued['upload_id']}/complete", headers=headers)
        second = client.post(f"/v1/assets/uploads/{issued['upload_id']}/complete", headers=headers)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.json()["id"] == first.json()["id"]

        reserved, used, statuses = _quota(container, workspace_id)
        assert (reserved, used, statuses) == (0, len(payload), ["SETTLED"])
    finally:
        client.__exit__(None, None, None)


def test_a_failed_settlement_rolls_back_the_whole_completion(container, store, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Adopt, complete and settle are one transaction or none of them."""

    from media_service import WorkspaceStorageQuota
    from production_domain.models import DirectUpload, DirectUploadStatus, MediaAsset
    from sqlalchemy import func, select

    payload = _png()
    digest = hashlib.sha256(payload).hexdigest()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store
    client, headers, project_id, workspace_id = _workspace_client(
        container, "atomic@example.com", raise_server_exceptions=False
    )
    try:
        issued = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "atomic-1"},
            json=_authorize_request(project_id, payload),
        ).json()
        assert store.client_put(issued["storage_key"], payload, "image/png", declared_sha=digest)

        def fail_settle(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("fixture settlement failure")

        monkeypatch.setattr(WorkspaceStorageQuota, "settle_in", fail_settle)
        failed = client.post(f"/v1/assets/uploads/{issued['upload_id']}/complete", headers=headers)
        assert failed.status_code == 500
    finally:
        client.__exit__(None, None, None)

    with container.database.session() as session:
        # No half-committed asset, and the upload can still be retried.
        assert session.scalar(select(func.count(MediaAsset.id))) == 0
        row = session.get(DirectUpload, issued["upload_id"])
        assert row.status == DirectUploadStatus.PENDING.value
        assert row.media_asset_id is None
    reserved, used, statuses = _quota(container, workspace_id)
    assert (reserved, used, statuses) == (len(payload), 0, ["RESERVED"])


def test_a_lost_authorization_insert_race_replays_instead_of_erroring(  # type: ignore[no-untyped-def]
    container,
    project,
    uploads,
    monkeypatch,
) -> None:
    """A project with no workspace has no reservation to serialize the race on.

    `authorize` reads for an existing row and inserts in two separate
    transactions. A concurrent first authorization landing in between made the
    loser's insert violate the unique constraint, and the raw `IntegrityError`
    surfaced as a 500.

    The race is produced, not described: the winner is hidden from the loser's
    *first* lookup only, so the insert really does collide with
    `uq_direct_upload_idempotency` and the recovery really does have to find the
    winner on its second lookup. An earlier version of this test wrapped
    `authorize` in a function that flipped a flag and then called through
    unchanged — it hid nothing, raised no IntegrityError, and passed whether or
    not the recovery path existed at all.
    """

    from media_service.direct_upload import DirectUploadService
    from production_domain.models import DirectUpload
    from sqlalchemy import func, select

    payload = _png()
    winner = _authorize(uploads, project.id, payload, idempotency_key="race-1")

    lookups = {"count": 0}
    original = DirectUploadService._existing_authorization

    def blind_read(session, project_id, idempotency_key):  # type: ignore[no-untyped-def]
        lookups["count"] += 1
        if lookups["count"] == 1:
            # The read happened before the winner committed.
            return None
        return original(session, project_id, idempotency_key)

    monkeypatch.setattr(
        DirectUploadService, "_existing_authorization", staticmethod(blind_read)
    )
    collisions: list[str] = []
    original_replay = DirectUploadService._replay

    def counting_replay(self, upload, **kwargs):  # type: ignore[no-untyped-def]
        collisions.append(upload.id)
        return original_replay(self, upload, **kwargs)

    monkeypatch.setattr(DirectUploadService, "_replay", counting_replay)

    loser = _authorize(uploads, project.id, payload, idempotency_key="race-1")
    # Two lookups: the blinded one, then the recovery that found the winner.
    assert lookups["count"] == 2
    # And the answer came from the replay path, not from a second insert.
    assert collisions == [winner.upload_id]
    assert loser.upload_id == winner.upload_id
    assert loser.presigned.storage_key == winner.presigned.storage_key

    with container.database.session() as session:
        assert (
            session.scalar(
                select(func.count(DirectUpload.id)).where(
                    DirectUpload.idempotency_key == "race-1"
                )
            )
            == 1
        )


def test_the_sweeper_reclaims_a_window_the_client_never_came_back_to(container, store) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    from production_domain.models import DirectUpload, DirectUploadStatus

    payload = _png()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store
    container.settings.platform_api_key = "sweeper-key"
    client, headers, project_id, workspace_id = _workspace_client(container, "sweep@example.com")
    try:
        issued = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "sweep-1"},
            json=_authorize_request(project_id, payload),
        ).json()
        # The client PUTs and then walks away without completing.
        assert store.client_put(
            issued["storage_key"],
            payload,
            "image/png",
            declared_sha=hashlib.sha256(payload).hexdigest(),
        )
        with container.database.session() as session:
            session.get(DirectUpload, issued["upload_id"]).expires_at = datetime.now(UTC) - timedelta(hours=2)

        assert _quota(container, workspace_id)[0] == len(payload)

        swept = client.post(
            "/internal/maintenance/expired-uploads",
            headers={"Authorization": "Bearer sweeper-key"},
        )
        assert swept.status_code == 200, swept.text
        body = swept.json()
        assert body["swept_count"] == 1
        assert body["swept"][0]["upload_id"] == issued["upload_id"]
        assert body["swept"][0]["reservation_released"] is True
        # The bytes stay in the bucket; deleting them is not a sweeper's call.
        assert body["swept"][0]["orphaned_object"] is True

        reserved, used, statuses = _quota(container, workspace_id)
        assert (reserved, used, statuses) == (0, 0, ["RELEASED"])
        with container.database.session() as session:
            row = session.get(DirectUpload, issued["upload_id"])
            assert row.status == DirectUploadStatus.ABANDONED.value

        # Idempotent: a second sweep finds nothing left to do.
        again = client.post(
            "/internal/maintenance/expired-uploads",
            headers={"Authorization": "Bearer sweeper-key"},
        )
        assert again.json()["swept_count"] == 0
    finally:
        client.__exit__(None, None, None)


def test_the_sweeper_reports_a_stale_hold_rather_than_releasing_it(container, store) -> None:  # type: ignore[no-untyped-def]
    """A hold whose registration succeeded and settlement failed is deliberate.

    Releasing that one would make real storage unaccounted, so the sweeper
    surfaces it for an operator instead of deciding.
    """

    from datetime import UTC, datetime, timedelta

    from production_domain.models import StorageReservation

    container.settings.platform_api_key = "sweeper-key"
    container.settings.storage_reservation_stale_after_seconds = 60
    client, headers, project_id, workspace_id = _workspace_client(container, "stale@example.com")
    try:
        from media_service import WorkspaceStorageQuota

        reservation = WorkspaceStorageQuota(container.database).reserve(
            workspace_id=workspace_id,
            project_id=project_id,
            byte_count=4096,
            idempotency_key="orphan-hold",
        )
        with container.database.session() as session:
            session.get(StorageReservation, reservation.id).created_at = datetime.now(UTC) - timedelta(
                hours=6
            )

        body = client.post(
            "/internal/maintenance/expired-uploads",
            headers={"Authorization": "Bearer sweeper-key"},
        ).json()
        reported = body["reservations_needing_reconciliation"]
        assert [item["reservation_id"] for item in reported] == [reservation.id]
        assert reported[0]["reserved_bytes"] == 4096
        # Reported, not released.
        assert _quota(container, workspace_id)[0] == 4096
    finally:
        client.__exit__(None, None, None)


def test_completion_uses_the_lineage_the_authorization_recorded(container, store) -> None:  # type: ignore[no-untyped-def]
    """The upload row's decision is consumed, not re-derived."""

    from media_service import lineage_key
    from production_domain.models import DirectUpload

    payload = _png()
    digest = hashlib.sha256(payload).hexdigest()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store
    client, headers, project_id, _workspace_id = _workspace_client(container, "lineage@example.com")
    try:
        character = client.post(
            f"/v1/projects/{project_id}/characters",
            headers=headers,
            json={"name": "Lead"},
        )
        character_id = character.json()["id"] if character.status_code < 300 else None
        request = _authorize_request(project_id, payload)
        if character_id:
            request["character_id"] = character_id
        issued = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "lineage-1"},
            json=request,
        )
        assert issued.status_code == 201, issued.text
        issued = issued.json()
        assert store.client_put(issued["storage_key"], payload, "image/png", declared_sha=digest)
        adopted = client.post(f"/v1/assets/uploads/{issued['upload_id']}/complete", headers=headers)
        assert adopted.status_code == 200, adopted.text
    finally:
        client.__exit__(None, None, None)

    with container.database.session() as session:
        row = session.get(DirectUpload, issued["upload_id"])
        stored_lineage = row.lineage_key
    asset = container.media.get(adopted.json()["id"])
    assert asset is not None
    assert asset.lineage_key == stored_lineage
    assert stored_lineage == lineage_key(character_id=character_id)


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


# --- Reclaiming an abandoned upload -----------------------------------------
#
# The sweep had the right effect and the wrong shape. It read expired rows with
# no lock, released the hold in one transaction and abandoned the row in
# another, which left this interleaving open:
#
# ```text
# sweeper  reads the upload, still PENDING
# complete locks the upload row and begins adopting it
# sweeper  releases the reservation and commits
# complete calls settle_in, finds it RELEASED -> StorageReservationConflict
# ```
#
# A client that had uploaded correctly got a 500. These pin the fix: one
# transaction per upload, the completion path's lock order, and the expiry
# predicate re-read under the lock rather than trusted from the unlocked scan.


def _expire(container, upload_id, *, hours: int = 2):  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    from production_domain.models import DirectUpload

    with container.database.session() as session:
        session.get(DirectUpload, upload_id).expires_at = datetime.now(UTC) - timedelta(hours=hours)


def _sweep(container):  # type: ignore[no-untyped-def]
    from media_service import WorkspaceStorageQuota, sweep_expired_uploads

    return sweep_expired_uploads(
        database=container.database,
        uploads=container.direct_uploads,
        quota=WorkspaceStorageQuota(container.database),
    )


def test_claiming_an_expired_upload_rechecks_the_predicate_under_the_lock(container, store) -> None:  # type: ignore[no-untyped-def]
    """The unlocked scan proposes; the locked re-read decides.

    Both ways a candidate can stop being sweepable between the two: a completion
    moved it out of `PENDING`, and a re-authorization moved its window forward.
    Acting on the scan's answer is exactly what let the sweep reclaim a hold a
    completion already owned.
    """

    from datetime import UTC, datetime, timedelta

    from production_domain.models import DirectUpload, DirectUploadStatus

    payload = _png()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store
    client, headers, project_id, workspace_id = _workspace_client(container, "recheck@example.com")
    try:
        issued = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "recheck-1"},
            json=_authorize_request(project_id, payload),
        ).json()
        upload_id = issued["upload_id"]
        _expire(container, upload_id)

        # It is a candidate right now.
        assert container.direct_uploads.expired() == [upload_id]

        # A completion got there first.
        with container.database.session() as session:
            session.get(DirectUpload, upload_id).status = DirectUploadStatus.COMPLETED.value
        with container.database.session() as session:
            claim = container.direct_uploads.claim_expired(session, upload_id)
        assert claim.claimed is False
        with container.database.session() as session:
            assert session.get(DirectUpload, upload_id).status == DirectUploadStatus.COMPLETED.value

        # And a window that moved forward is a live session, not the sweeper's.
        with container.database.session() as session:
            row = session.get(DirectUpload, upload_id)
            row.status = DirectUploadStatus.PENDING.value
            row.expires_at = datetime.now(UTC) + timedelta(hours=1)
        with container.database.session() as session:
            assert container.direct_uploads.claim_expired(session, upload_id).claimed is False

        # The hold is untouched throughout.
        assert _quota(container, workspace_id)[0] == len(payload)
    finally:
        client.__exit__(None, None, None)


def test_the_abandon_and_the_release_commit_together_or_not_at_all(  # type: ignore[no-untyped-def]
    container, store, monkeypatch
) -> None:
    """A hold that cannot be released leaves the row sweepable, not half-swept.

    The old sweep abandoned in a second transaction, so a failed release still
    produced `ABANDONED` + `RESERVED` — a row nothing would ever look at again
    holding capacity nothing would ever give back.
    """

    from media_service import StorageReservationConflict, WorkspaceStorageQuota
    from production_domain.models import DirectUpload, DirectUploadStatus

    payload = _png()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store
    client, headers, project_id, workspace_id = _workspace_client(container, "atomic@example.com")
    try:
        issued = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "atomic-1"},
            json=_authorize_request(project_id, payload),
        ).json()
        upload_id = issued["upload_id"]
        _expire(container, upload_id)

        def refuse(self, session, reservation_id):  # type: ignore[no-untyped-def]
            raise StorageReservationConflict("workspace storage counters changed unexpectedly")

        monkeypatch.setattr(WorkspaceStorageQuota, "release_in", refuse)
        result = _sweep(container)
        assert result.swept == []
        assert [item["upload_id"] for item in result.contended] == [upload_id]

        with container.database.session() as session:
            assert session.get(DirectUpload, upload_id).status == DirectUploadStatus.PENDING.value
        assert _quota(container, workspace_id) == (len(payload), 0, ["RESERVED"])

        # The next sweep, with the conflict gone, finishes the job.
        monkeypatch.undo()
        result = _sweep(container)
        assert [item["upload_id"] for item in result.swept] == [upload_id]
        assert result.swept[0]["reservation_released"] is True
        with container.database.session() as session:
            assert session.get(DirectUpload, upload_id).status == DirectUploadStatus.ABANDONED.value
        assert _quota(container, workspace_id) == (0, 0, ["RELEASED"])
    finally:
        client.__exit__(None, None, None)


def test_a_completed_upload_is_never_swept_even_once_its_window_closed(container, store) -> None:  # type: ignore[no-untyped-def]
    """Expiry is about the presigned PUT, not about the asset it produced."""

    from production_domain.models import DirectUpload, DirectUploadStatus

    payload = _png()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store
    client, headers, project_id, workspace_id = _workspace_client(container, "done@example.com")
    try:
        issued = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "done-1"},
            json=_authorize_request(project_id, payload),
        ).json()
        assert store.client_put(
            issued["storage_key"],
            payload,
            "image/png",
            declared_sha=hashlib.sha256(payload).hexdigest(),
        )
        completed = client.post(
            f"/v1/assets/uploads/{issued['upload_id']}/complete", headers=headers
        )
        assert completed.status_code == 200, completed.text
        _expire(container, issued["upload_id"])

        result = _sweep(container)
        assert result.swept == []
        with container.database.session() as session:
            row = session.get(DirectUpload, issued["upload_id"])
            assert row.status == DirectUploadStatus.COMPLETED.value
        # Settled, not released: the bytes are real and accounted for.
        assert _quota(container, workspace_id) == (0, len(payload), ["SETTLED"])
    finally:
        client.__exit__(None, None, None)


def test_a_reservation_conflict_on_completion_answers_409_not_500(  # type: ignore[no-untyped-def]
    container, store, monkeypatch
) -> None:
    """Settlement is fail-closed, so a conflict must stay an answer.

    The expiry sweep was the reachable cause and no longer races here, but a
    conflict raised out of `settle_in` used to leave the endpoint with no
    handler at all: the client that had uploaded correctly saw a stack trace.
    """

    from media_service import StorageReservationConflict, WorkspaceStorageQuota
    from production_domain.models import DirectUpload, DirectUploadStatus, MediaAsset
    from sqlalchemy import func, select

    payload = _png()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store
    client, headers, project_id, workspace_id = _workspace_client(container, "conflict@example.com")
    try:
        issued = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "conflict-1"},
            json=_authorize_request(project_id, payload),
        ).json()
        assert store.client_put(
            issued["storage_key"],
            payload,
            "image/png",
            declared_sha=hashlib.sha256(payload).hexdigest(),
        )

        def conflict(self, session, reservation_id, **kwargs):  # type: ignore[no-untyped-def]
            raise StorageReservationConflict("released reservation cannot be settled")

        monkeypatch.setattr(WorkspaceStorageQuota, "settle_in", conflict)
        response = client.post(
            f"/v1/assets/uploads/{issued['upload_id']}/complete", headers=headers
        )
        assert response.status_code == 409
        assert "cannot be settled" in response.json()["detail"]
    finally:
        client.__exit__(None, None, None)

    # The transaction rolled back: no asset, and no half-moved upload row.
    with container.database.session() as session:
        assert session.scalar(select(func.count(MediaAsset.id))) == 0
        assert (
            session.get(DirectUpload, issued["upload_id"]).status
            != DirectUploadStatus.COMPLETED.value
        )


def test_the_worker_runs_the_sweep_on_its_own_schedule(container, store) -> None:  # type: ignore[no-untyped-def]
    """"The sweep exists" and "the sweep runs" were two different claims.

    Only the first was true: `POST /internal/maintenance/expired-uploads` made
    an abandoned upload reclaimable and nothing called it, so in production the
    hold still sat there until an operator remembered. The worker loop is what
    closes it, and it runs the same implementation the endpoint does.
    """

    from generation_gateway.worker import sweep_expired_uploads_once
    from production_domain.models import DirectUpload, DirectUploadStatus

    payload = _png()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store
    client, headers, project_id, workspace_id = _workspace_client(container, "worker@example.com")
    try:
        issued = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "worker-1"},
            json=_authorize_request(project_id, payload),
        ).json()
        _expire(container, issued["upload_id"])
        assert _quota(container, workspace_id)[0] == len(payload)

        assert sweep_expired_uploads_once(container) == 1

        with container.database.session() as session:
            assert (
                session.get(DirectUpload, issued["upload_id"]).status
                == DirectUploadStatus.ABANDONED.value
            )
        assert _quota(container, workspace_id) == (0, 0, ["RELEASED"])
        # Idempotent, because the loop will call it again in five minutes.
        assert sweep_expired_uploads_once(container) == 0
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_the_worker_loop_sweeps_on_its_interval_and_survives_a_failure() -> None:
    """The loop must schedule it, and maintenance must never take the loop down."""

    import asyncio
    from types import SimpleNamespace

    from generation_gateway import worker as worker_module

    settings = SimpleNamespace(
        worker_poll_interval_seconds=0,
        expired_upload_sweep_interval_seconds=3600,
        expired_upload_sweep_limit=200,
        storage_reservation_stale_after_seconds=86_400,
        generation_staging_sweep_interval_seconds=3600,
        generation_staging_ttl_seconds=86_400,
        generation_staging_sweep_limit=500,
    )
    container = SimpleNamespace(
        settings=settings,
        gateway=SimpleNamespace(recover_after_restart=lambda: None),
    )
    calls = {"sweeps": 0, "staging_sweeps": 0, "jobs": 0}

    def exploding_sweep(_container):  # type: ignore[no-untyped-def]
        calls["sweeps"] += 1
        raise RuntimeError("object storage is unreachable")

    def exploding_staging_sweep(_container):  # type: ignore[no-untyped-def]
        calls["staging_sweeps"] += 1
        raise RuntimeError("object storage is unreachable")

    async def no_jobs(_container):  # type: ignore[no-untyped-def]
        calls["jobs"] += 1
        if calls["jobs"] >= 3:
            raise asyncio.CancelledError
        return False

    worker_module.sweep_expired_uploads_once = exploding_sweep  # type: ignore[assignment]
    worker_module.sweep_generation_staging_once = exploding_staging_sweep  # type: ignore[assignment]
    worker_module.process_next_job = no_jobs  # type: ignore[assignment]
    try:
        with pytest.raises(asyncio.CancelledError):
            await worker_module.run_loop(container)
    finally:
        importlib_reload = __import__("importlib").reload
        importlib_reload(worker_module)

    # Due immediately on start — a worker that restarts often would otherwise
    # never reach its first sweep — and once only, on an hour's interval.
    assert calls["sweeps"] == 1
    assert calls["staging_sweeps"] == 1
    # And the job loop kept running through both failures.
    assert calls["jobs"] == 3


@pytest.mark.postgres_only
def test_the_sweep_blocks_behind_a_completion_that_already_owns_the_row(  # type: ignore[no-untyped-def]
    container, store, monkeypatch
) -> None:
    """The reported interleaving, forced rather than left to scheduling.

    The sweeper is released at the exact moment the completion holds the upload
    row and is about to settle its hold — the window the old sweep drove
    straight through, because it took the reservation without ever touching the
    row. Now `claim_expired` asks for the same row first, so the sweeper waits,
    and by the time it reads the row the completion has committed and the row
    says `COMPLETED`.

    Needs two transactions genuinely running at once, which is why it is
    PostgreSQL-only: SQLite serialises them and the situation cannot be built.
    """

    import threading

    from media_service import WorkspaceStorageQuota
    from production_domain.models import DirectUpload, DirectUploadStatus

    payload = _png()
    container.storage = store
    container.direct_uploads.storage = store
    container.media.storage = store
    client, headers, project_id, workspace_id = _workspace_client(container, "race@example.com")
    try:
        issued = client.post(
            "/v1/assets/uploads",
            headers={**headers, "Idempotency-Key": "sweep-race-1"},
            json=_authorize_request(project_id, payload),
        ).json()
        assert store.client_put(
            issued["storage_key"],
            payload,
            "image/png",
            declared_sha=hashlib.sha256(payload).hexdigest(),
        )
        # The window closed while the client was still finishing. Completing is
        # still correct — the object is there — and this is what makes the row a
        # sweep candidate and a completion candidate at the same instant.
        _expire(container, issued["upload_id"])

        completion_holds_the_row = threading.Event()
        swept: dict[str, object] = {}

        def sweeper() -> None:
            completion_holds_the_row.wait(10)
            swept["result"] = _sweep(container)

        original_settle = WorkspaceStorageQuota.settle_in

        def settle_with_a_sweeper_racing(self, session, reservation_id, **kwargs):  # type: ignore[no-untyped-def]
            completion_holds_the_row.set()
            # Long enough for the sweeper to read its candidates and block on
            # the row lock this transaction is holding.
            time.sleep(0.5)
            return original_settle(self, session, reservation_id, **kwargs)

        monkeypatch.setattr(WorkspaceStorageQuota, "settle_in", settle_with_a_sweeper_racing)
        thread = threading.Thread(target=sweeper, daemon=True)
        thread.start()
        response = client.post(
            f"/v1/assets/uploads/{issued['upload_id']}/complete", headers=headers
        )
        thread.join(20)
        assert not thread.is_alive()
    finally:
        client.__exit__(None, None, None)

    # The client uploaded correctly and is told so. This was the 500.
    assert response.status_code == 200, response.text
    # The sweeper found the row already owned and left it alone.
    assert swept["result"].swept == []
    assert [item["upload_id"] for item in swept["result"].contended] == [issued["upload_id"]]
    with container.database.session() as session:
        assert (
            session.get(DirectUpload, issued["upload_id"]).status
            == DirectUploadStatus.COMPLETED.value
        )
    # Settled, never released: no torn PENDING/RELEASED pair, no unaccounted bytes.
    assert _quota(container, workspace_id) == (0, len(payload), ["SETTLED"])
