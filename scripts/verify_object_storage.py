"""Prove the configured object store before the platform trusts it.

Every claim the media plane rests on is checked here against the *real* bucket,
because "S3-compatible" is a spectrum and the differences are silent until a
generation is billed:

1. a presigned PUT can be issued at all;
2. a client can transfer straight to the store, without the API in the path;
3. **the store enforces the declared digest** — this is the load-bearing one.
   Content-addressed keys are only safe because bytes that do not hash to the
   declared SHA-256 are rejected. A store that ignores `x-amz-checksum-sha256`
   accepts them, and the key then names content the object does not contain;
4. `HEAD` reports a size the completion path can settle a quota hold against;
5. a bounded range read returns the header validation needs;
6. a presigned GET is issued, and the bytes come back identical — this is the
   URL an external provider fetches a reference from.

It writes one small object under a `_preflight/` prefix and deletes it.

    uv run python scripts/verify_object_storage.py

Prints no secret. Exits non-zero if anything the platform depends on fails.
"""

from __future__ import annotations

import hashlib
import io
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402
from platform_shared import S3CompatibleStorage, Settings  # noqa: E402

OK, FAIL, WARN = "ok  ", "FAIL", "WARN"


def _say(mark: str, label: str, detail: str = "") -> None:
    print(f"  [{mark}] {label:32} {detail}")


def _sample() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (320, 180), (28, 96, 152)).save(buffer, format="PNG")
    return buffer.getvalue()


def _put(url: str, payload: bytes, headers: dict[str, str]) -> int:
    request = urllib.request.Request(url, data=payload, method="PUT", headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return int(response.status)


def main() -> int:
    settings = Settings()
    if settings.storage_backend.lower() != "s3":
        print(f"STORAGE_BACKEND is {settings.storage_backend!r}; nothing to verify.")
        return 1
    missing = [
        name
        for name, value in (
            ("S3_ENDPOINT_URL", settings.s3_endpoint_url),
            ("S3_BUCKET", settings.s3_bucket),
            ("S3_ACCESS_KEY_ID", settings.s3_access_key_id),
            ("S3_SECRET_ACCESS_KEY", settings.s3_secret_access_key),
        )
        if not value.strip()
    ]
    if missing:
        print("Not configured yet — fill in: " + ", ".join(missing))
        return 1

    host = urlsplit(settings.s3_endpoint_url).hostname or "?"
    print(f"\n  bucket {settings.s3_bucket} at {host}")
    print(f"  region {settings.s3_region} · addressing {settings.s3_addressing_style} · "
          f"checksum {'on' if settings.s3_enforce_upload_checksum else 'OFF'}\n")

    storage = S3CompatibleStorage(
        bucket=settings.s3_bucket,
        cache_root=settings.storage_root,
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
        public_base_url=settings.public_base_url,
        max_object_bytes=settings.max_upload_bytes,
        addressing_style=settings.s3_addressing_style,
        enforce_checksum=settings.s3_enforce_upload_checksum,
    )

    payload = _sample()
    digest = hashlib.sha256(payload).hexdigest()
    key = f"_preflight/{digest}.png"
    failures = 0

    presigned = storage.presigned_upload(key, sha256=digest, mime_type="image/png", expires_in=600)
    if presigned is None:
        _say(FAIL, "presigned PUT", "the store would not issue one")
        return 1
    _say(OK, "presigned PUT", f"checksum bound: {'x-amz-checksum-sha256' in presigned.headers}")

    try:
        _say(OK, "client transfer", f"HTTP {_put(presigned.url, payload, presigned.headers)}")
    except urllib.error.HTTPError as exc:
        _say(FAIL, "client transfer", f"HTTP {exc.code} — {exc.reason}")
        return 1

    # 3. The one that matters. Declare this digest, send different bytes.
    tampered = storage.presigned_upload(
        f"_preflight/tamper-{digest}.png", sha256=digest, mime_type="image/png", expires_in=600
    )
    enforced = False
    if tampered is not None:
        try:
            _put(tampered.url, payload + b"tampered", tampered.headers)
        except urllib.error.HTTPError:
            enforced = True
    if enforced:
        _say(OK, "digest enforced by store", "mismatched bytes rejected")
    else:
        failures += 1
        _say(
            FAIL,
            "digest enforced by store",
            "MISMATCHED BYTES ACCEPTED — content-addressed keys are not trustworthy here",
        )
        storage.client.delete_object(Bucket=storage.bucket, Key=f"_preflight/tamper-{digest}.png")

    stat = storage.stat(key)
    if stat and stat.size == len(payload):
        _say(OK, "HEAD", f"{stat.size} bytes · {stat.mime_type}")
    else:
        failures += 1
        _say(FAIL, "HEAD", f"reported {stat.size if stat else 'nothing'}, expected {len(payload)}")

    header = storage.read_prefix(key, 65536)
    if header[:8] == payload[:8]:
        _say(OK, "bounded range read", f"{len(header)} bytes")
    else:
        failures += 1
        _say(FAIL, "bounded range read", "returned nothing usable")

    reference = storage.presigned_reference_url(key, expires_in=600, mime_type="image/png")
    if not reference:
        failures += 1
        _say(FAIL, "presigned reference URL", "not issued — providers cannot fetch references")
    else:
        try:
            with urllib.request.urlopen(reference, timeout=60) as response:
                fetched = response.read()
            if fetched == payload:
                _say(OK, "provider can fetch it", f"{len(fetched)} bytes, identical")
            else:
                failures += 1
                _say(FAIL, "provider can fetch it", "bytes differ")
        except urllib.error.HTTPError as exc:
            failures += 1
            _say(FAIL, "provider can fetch it", f"HTTP {exc.code}")
        if urlsplit(reference).scheme != "https":
            _say(WARN, "reference URL is HTTPS", "live mode refuses a non-HTTPS reference")

    storage.client.delete_object(Bucket=storage.bucket, Key=key)
    print()
    if failures:
        print(f"  {failures} check(s) failed — do not run reference-carrying shots against this store.\n")
        return 1
    print("  Storage plane verified. Reference media, direct uploads and renditions are safe here.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
