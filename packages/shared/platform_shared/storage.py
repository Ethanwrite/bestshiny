from __future__ import annotations

import base64
import hashlib
import hmac
import mimetypes
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from urllib.parse import quote, urlencode


@dataclass(frozen=True)
class StoredObject:
    key: str
    local_path: str
    public_url: str | None
    sha256: str
    size: int
    mime_type: str


@dataclass(frozen=True)
class PresignedUpload:
    """Everything a client needs to PUT one object straight to storage."""

    url: str
    method: str
    headers: dict[str, str]
    storage_key: str
    expires_in: int


@dataclass(frozen=True)
class StoredObjectStat:
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

    def presigned_upload(
        self,
        key: str,
        *,
        sha256: str,
        mime_type: str,
        expires_in: int = 900,
    ) -> PresignedUpload | None:
        """A short-lived URL the client PUTs bytes to **directly**.

        Reads already bypass this service; writes are the other half. A user
        uploading a 38 MB plate should not stream it through the control plane
        on the way to a bucket that could have received it directly.

        The checksum is not advisory. It is bound into the presigned request so
        the object store itself rejects bytes that do not hash to ``sha256``,
        which is what makes a client-declared digest trustworthy enough to
        content-address the key with — without this service reading the object.

        ``None`` means this backend cannot accept a direct upload; callers must
        fall back to the multipart endpoint rather than inventing a URL.
        """
        ...

    def stat(self, key: str) -> StoredObjectStat | None:
        """Size and content type as the *store* reports them, or None if absent."""
        ...

    def read_prefix(self, key: str, length: int) -> bytes:
        """The first ``length`` bytes, for header validation without a full read."""
        ...

    def content_sha256(self, key: str, *, max_bytes: int) -> str | None:
        """Hash the stored bytes, or return None when they cannot be read safely."""
        ...

    def presigned_reference_url(
        self,
        key: str,
        *,
        expires_in: int = 900,
        mime_type: str | None = None,
    ) -> str | None:
        """A short-lived URL an external provider can fetch **directly**.

        The point is what is *not* in the path. An external provider fetching
        through the application means every reference byte is read from object
        storage into the API process and streamed out again; a handful of
        concurrent 4K reference edits turns the API into an image CDN, and it is
        the tier that can least afford to be one. Object storage serves the
        bytes; the application only decides who may ask for them.

        ``None`` means this backend cannot hand out a direct URL, which callers
        must treat as "no fetchable reference", not as a reason to proxy.
        """
        ...


class LocalStorage:
    def __init__(
        self,
        root: Path,
        public_base_url: str = "",
        *,
        max_object_bytes: int = 100 * 1024 * 1024,
        reference_signing_key: str = "",
    ):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")
        self.max_object_bytes = max(1, max_object_bytes)
        # Local disk has no origin an external provider can reach, so the only
        # way to expose one is through the application — exactly the proxying
        # object storage exists to avoid. It stays off unless an operator
        # supplies a signing key, and then it is a bounded development
        # affordance rather than the deployment shape.
        self.reference_signing_key = reference_signing_key.strip()

    def presigned_reference_url(
        self,
        key: str,
        *,
        expires_in: int = 900,
        mime_type: str | None = None,
    ) -> str | None:
        del mime_type
        if not (self.reference_signing_key and self.public_base_url):
            return None
        return signed_local_reference_url(
            self.public_base_url,
            key,
            signing_key=self.reference_signing_key,
            expires_in=expires_in,
        )

    def presigned_upload(
        self,
        key: str,
        *,
        sha256: str,
        mime_type: str,
        expires_in: int = 900,
    ) -> PresignedUpload | None:
        """Local disk has no origin a browser can PUT to. Say so."""

        del key, sha256, mime_type, expires_in
        return None

    def stat(self, key: str) -> StoredObjectStat | None:
        try:
            path = self.path_for(key)
        except ValueError:
            return None
        if not path.is_file():
            return None
        return StoredObjectStat(
            size=path.stat().st_size,
            mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )

    def read_prefix(self, key: str, length: int) -> bytes:
        with self.open(key, "rb") as stream:
            return stream.read(max(0, length))

    def content_sha256(self, key: str, *, max_bytes: int) -> str | None:
        digest = hashlib.sha256()
        size = 0
        try:
            with self.open(key, "rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        return None
                    digest.update(chunk)
        except (OSError, ValueError):
            return None
        return digest.hexdigest()

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


def _reference_signature(key: str, expires_at: int, signing_key: str) -> str:
    payload = f"{key}\n{expires_at}".encode()
    digest = hmac.new(signing_key.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def signed_local_reference_url(
    public_base_url: str,
    key: str,
    *,
    signing_key: str,
    expires_in: int = 900,
) -> str:
    expires_at = int(time.time()) + max(1, expires_in)
    query = urlencode(
        {"expires": expires_at, "signature": _reference_signature(key, expires_at, signing_key)}
    )
    return f"{public_base_url.rstrip('/')}/v1/media/reference/{quote(key)}?{query}"


def verify_local_reference_signature(
    key: str,
    *,
    expires: str | int,
    signature: str,
    signing_key: str,
) -> bool:
    if not signing_key:
        return False
    try:
        expires_at = int(expires)
    except (TypeError, ValueError):
        return False
    if expires_at < int(time.time()):
        return False
    return hmac.compare_digest(_reference_signature(key, expires_at, signing_key), signature)


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
        addressing_style: str = "auto",
        enforce_checksum: bool = True,
    ):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.cache_root = cache_root.expanduser().resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")
        self.max_object_bytes = max(1, max_object_bytes)
        # Alibaba OSS's S3-compatible endpoint addresses buckets virtual-hosted
        # (`bucket.s3.oss-<region>.aliyuncs.com`); MinIO wants path style. boto3's
        # "auto" guesses from the endpoint and guesses wrong often enough that
        # this is worth stating rather than discovering as a 404 on every object.
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region,
            aws_access_key_id=access_key_id or None,
            aws_secret_access_key=secret_access_key or None,
            config=Config(
                s3={"addressing_style": addressing_style},
                # botocore >= 1.36 adds a CRC32 trailer to every upload by
                # default, which puts the body on the wire as
                # STREAMING-UNSIGNED-PAYLOAD-TRAILER aws-chunked encoding.
                # Alibaba OSS does not implement that encoding and answers
                # PutObject with `NotImplemented`, so the default breaks every
                # write. "when_required" still sends a checksum whenever the
                # caller asks for one explicitly — which is exactly what
                # `enforce_checksum` does on the presigned path below — so the
                # integrity guarantee is unchanged; only the implicit trailer
                # on plain uploads goes away.
                request_checksum_calculation="when_required",
            ),
        )
        # `x-amz-checksum-sha256` is what makes a client-declared digest safe to
        # content-address a key with: the store rejects bytes that do not hash to
        # it. It is a 2022 addition to the S3 API and not every compatible
        # implementation carries it. Turning it off is a deliberate, recorded
        # downgrade — the key then names content the object is only *claimed* to
        # hold — so verify with `scripts/verify_object_storage.py` before doing it.
        self.enforce_checksum = enforce_checksum

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

    def presigned_reference_url(
        self,
        key: str,
        *,
        expires_in: int = 900,
        mime_type: str | None = None,
    ) -> str | None:
        """A direct, expiring object-storage URL. The application is not in this path.

        This is the deployment shape the reference flow is designed around: the
        provider fetches from object storage, and neither the upload nor the
        fetch passes through the API process.
        """

        # The object was stored with validated Content-Type metadata. Do not
        # add S3's response-content-type override: Alibaba's S3-compatible
        # endpoint signs it but rejects the resulting GET with HTTP 400.
        del mime_type
        params: dict[str, str] = {"Bucket": self.bucket, "Key": key}
        try:
            url = self.client.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=max(1, expires_in),
            )
        except Exception:
            # A presign failure is not a reason to fall back to proxying; the
            # caller treats None as "no fetchable reference" and fails closed.
            return None
        return str(url) if url else None

    def presigned_upload(
        self,
        key: str,
        *,
        sha256: str,
        mime_type: str,
        expires_in: int = 900,
    ) -> PresignedUpload | None:
        """A presigned PUT whose checksum the store enforces.

        `x-amz-checksum-sha256` is the load-bearing part. Without it a client
        could declare one digest and upload different bytes, and since the key
        is content-addressed that would leave an object whose name lies about
        its contents. With it, S3 rejects the mismatch and this service never
        has to read the object to know its hash.
        """

        checksum = base64.b64encode(bytes.fromhex(sha256)).decode()
        params: dict[str, str] = {
            "Bucket": self.bucket,
            "Key": key,
            "ContentType": mime_type,
        }
        if self.enforce_checksum:
            params["ChecksumSHA256"] = checksum
        try:
            url = self.client.generate_presigned_url(
                "put_object",
                Params=params,
                ExpiresIn=max(1, expires_in),
            )
        except Exception:
            return None
        if not url:
            return None
        headers = {"Content-Type": mime_type}
        if self.enforce_checksum:
            headers["x-amz-checksum-sha256"] = checksum
        return PresignedUpload(
            url=str(url),
            method="PUT",
            headers=headers,
            storage_key=key,
            expires_in=max(1, expires_in),
        )

    def stat(self, key: str) -> StoredObjectStat | None:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            return None
        return StoredObjectStat(
            size=int(head.get("ContentLength") or 0),
            mime_type=str(head.get("ContentType") or "application/octet-stream").split(";", 1)[0],
        )

    def read_prefix(self, key: str, length: int) -> bytes:
        """A bounded Range read. Never the whole object."""

        if length <= 0:
            return b""
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key, Range=f"bytes=0-{length - 1}")
        except Exception:
            return b""
        body = response.get("Body")
        return bytes(body.read()) if body is not None else b""

    def content_sha256(self, key: str, *, max_bytes: int) -> str | None:
        """Stream and hash the remote object without trusting a local cache."""

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception:
            return None
        body = response.get("Body")
        if body is None:
            return None
        digest = hashlib.sha256()
        size = 0
        try:
            while chunk := body.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    return None
                digest.update(chunk)
        except Exception:
            return None
        finally:
            body.close()
        return digest.hexdigest()
