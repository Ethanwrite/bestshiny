"""Prove the configured object store before the platform trusts it.

Every claim the media plane rests on is checked here against the *real* bucket,
because "S3-compatible" is a spectrum and the differences are silent until a
generation is billed:

1. a presigned PUT can be issued at all;
2. a client can transfer straight to the store, without the API in the path;
3. a browser can preflight that PUT from every configured Web origin;
4. **the declared digest is enforced or verified** — content-addressed keys
   are safe only when mismatched bytes are rejected by the store, or the
   completion path reads the object once and verifies its SHA-256 before
   adoption;
5. `HEAD` reports a size the completion path can settle a quota hold against;
6. a bounded range read returns the header validation needs;
7. a presigned GET is issued, and the bytes come back identical — this is the
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


def _cors_preflight(
    url: str,
    *,
    origin: str,
    upload_headers: dict[str, str],
) -> tuple[bool, str]:
    requested_headers = sorted(header.lower() for header in upload_headers)
    request = urllib.request.Request(
        url,
        method="OPTIONS",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": ",".join(requested_headers),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            allowed_origin = response.headers.get("Access-Control-Allow-Origin", "").strip()
            allowed_methods = {
                item.strip().upper()
                for item in response.headers.get("Access-Control-Allow-Methods", "").split(",")
                if item.strip()
            }
            allowed_headers = {
                item.strip().lower()
                for item in response.headers.get("Access-Control-Allow-Headers", "").split(",")
                if item.strip()
            }
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    origin_ok = allowed_origin in {"*", origin}
    method_ok = "PUT" in allowed_methods
    headers_ok = "*" in allowed_headers or set(requested_headers).issubset(allowed_headers)
    missing = []
    if not origin_ok:
        missing.append("origin")
    if not method_ok:
        missing.append("PUT")
    if not headers_ok:
        missing.append("headers " + ",".join(requested_headers))
    return not missing, "allowed" if not missing else "missing " + "; ".join(missing)


def _probe_content_md5(storage, payload: bytes, orphans: list[str]) -> tuple[str, str, str]:  # type: ignore[no-untyped-def]
    """Does this store at least reject a wrong `Content-MD5`?

    Alibaba documents Content-MD5 and CRC64 rather than the `x-amz-checksum-*`
    family, so a store that ignores the SHA-256 checksum may still refuse a
    mismatched MD5. That is worth knowing, but it is a *weaker* guarantee: it
    proves the bytes arrived unchanged, not that they hash to the SHA-256 the
    key is named after. Only a read-back at completion establishes the latter.
    """

    import base64

    key = "_preflight/md5-probe.bin"
    honest_md5 = base64.b64encode(hashlib.md5(payload).digest()).decode()  # noqa: S324
    try:
        url = storage.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": storage.bucket, "Key": key, "ContentMD5": honest_md5},
            ExpiresIn=600,
        )
    except Exception:
        return (WARN, "  └ Content-MD5 fallback", "the store would not presign one")
    try:
        _put(url, payload + b"tampered", {"Content-MD5": honest_md5})
    except urllib.error.HTTPError:
        return (
            OK,
            "  └ Content-MD5 fallback",
            "available — proves transit integrity, NOT that bytes match the key's SHA-256",
        )
    orphans.append(key)
    return (
        FAIL,
        "  └ Content-MD5 fallback",
        "not enforced either — completion must read the object back and hash it",
    )


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
    print(
        f"  region {settings.s3_region} · addressing {settings.s3_addressing_style} · "
        f"checksum {'on' if settings.s3_enforce_upload_checksum else 'OFF'}\n"
    )

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
    orphans: list[str] = []

    presigned = storage.presigned_upload(key, sha256=digest, mime_type="image/png", expires_in=600)
    if presigned is None:
        _say(FAIL, "presigned PUT", "the store would not issue one")
        return 1

    # Structural, no extra permission: a virtual-hosted URL puts the bucket in
    # the hostname, a path-style one puts it in the path. The transfer below is
    # the real proof; this names the mismatch when the transfer fails.
    presign_host = urlsplit(presigned.url).hostname or ""
    looks_virtual = presign_host.startswith(f"{settings.s3_bucket}.")
    wanted_virtual = settings.s3_addressing_style == "virtual"
    if settings.s3_addressing_style == "auto" or looks_virtual == wanted_virtual:
        _say(OK, "addressing style", f"{'virtual' if looks_virtual else 'path'}-hosted URL issued")
    else:
        failures += 1
        _say(
            FAIL,
            "addressing style",
            f"configured {settings.s3_addressing_style}, URL is "
            f"{'virtual' if looks_virtual else 'path'}-hosted",
        )
    _say(OK, "presigned PUT", f"sha256 checksum bound: {'x-amz-checksum-sha256' in presigned.headers}")

    web_origins = [origin.strip().rstrip("/") for origin in settings.web_origins.split(",") if origin.strip()]
    if not web_origins:
        failures += 1
        _say(FAIL, "browser CORS", "WEB_ORIGINS has no frontend origin to verify")
    for origin in web_origins:
        allowed, detail = _cors_preflight(
            presigned.url,
            origin=origin,
            upload_headers=presigned.headers,
        )
        if not allowed:
            failures += 1
        _say(OK if allowed else FAIL, f"browser CORS · {origin}", detail)

    try:
        _say(OK, "client transfer", f"HTTP {_put(presigned.url, payload, presigned.headers)}")
    except urllib.error.HTTPError as exc:
        _say(FAIL, "client transfer", f"HTTP {exc.code} — {exc.reason}")
        return 1

    # The one that matters. Declare this digest, send different bytes.
    tamper_key = f"_preflight/tamper-{digest}.png"
    tampered = storage.presigned_upload(tamper_key, sha256=digest, mime_type="image/png", expires_in=600)
    enforced = False
    if tampered is not None:
        try:
            _put(tampered.url, payload + b"tampered", tampered.headers)
            orphans.append(tamper_key)
        except urllib.error.HTTPError:
            enforced = True
    completion_verified = False
    if enforced:
        _say(OK, "checksum enforcement", "mismatched bytes rejected by the store")
    else:
        _say(WARN, "checksum enforcement", "store accepted mismatched bytes")
        # Content-MD5 proves transit integrity, not content-addressed identity.
        _say(*_probe_content_md5(storage, payload, orphans))
        if settings.s3_verify_upload_sha256_on_complete:
            actual = storage.content_sha256(key, max_bytes=settings.max_upload_bytes)
            tampered_actual = storage.content_sha256(
                tamper_key,
                max_bytes=settings.max_upload_bytes,
            )
            completion_verified = actual == digest and tampered_actual not in {None, digest}
            if completion_verified:
                _say(OK, "completion SHA-256", "valid object accepted; mismatched object rejected")
            else:
                failures += 1
                _say(FAIL, "completion SHA-256", "full-object verification did not prove identity")
        else:
            failures += 1
            _say(
                FAIL,
                "completion SHA-256",
                "enable S3_VERIFY_UPLOAD_SHA256_ON_COMPLETE for this backend",
            )

    stat = storage.stat(key)
    if stat and stat.size == len(payload):
        _say(OK, "HEAD", f"{stat.size} bytes · {stat.mime_type}")
    else:
        failures += 1
        _say(FAIL, "HEAD", f"reported {stat.size if stat else 'nothing'}, expected {len(payload)}")

    header = storage.read_prefix(key, 65536)
    if header[:8] == payload[:8]:
        _say(OK, "range GET", f"{len(header)} bytes, bounded")
    else:
        failures += 1
        _say(FAIL, "range GET", "returned nothing usable")

    reference = storage.presigned_reference_url(key, expires_in=600, mime_type="image/png")
    if not reference:
        failures += 1
        _say(FAIL, "reference URL", "not issued — providers cannot fetch references")
    else:
        try:
            with urllib.request.urlopen(reference, timeout=60) as response:
                fetched = response.read()
            if fetched == payload:
                _say(OK, "reference URL", f"fetched {len(fetched)} bytes, identical")
            else:
                failures += 1
                _say(FAIL, "reference URL", "bytes differ from what was uploaded")
        except urllib.error.HTTPError as exc:
            failures += 1
            _say(FAIL, "reference URL", f"HTTP {exc.code}")
        if urlsplit(reference).scheme == "https":
            _say(OK, "reference URL HTTPS", "")
        else:
            failures += 1
            _say(FAIL, "reference URL HTTPS", "live mode refuses a non-HTTPS reference")

    # The platform never deletes an object, so the service role is not required
    # to carry oss:DeleteObject. Only this script needs it, and being denied is
    # information rather than a failure.
    orphans.append(key)
    undeleted = []
    for orphan in orphans:
        try:
            storage.client.delete_object(Bucket=storage.bucket, Key=orphan)
        except Exception:
            undeleted.append(orphan)
    if undeleted:
        _say(WARN, "probe cleanup", "oss:DeleteObject not granted; remove by hand:")
        for orphan in undeleted:
            print(f"           {orphan}")

    print()
    if not failures:
        print("  Storage plane verified. Browser uploads, reference media and renditions are safe here.\n")
        return 0
    print(f"  {failures} check(s) failed.\n")
    if not enforced and not completion_verified:
        print(
            "  The store does not enforce the SHA-256 the presigned PUT declares. Content-MD5\n"
            "  and CRC64 are what Alibaba documents, and neither settles the question this\n"
            "  design depends on: a content-addressed key asserts `object bytes hash to the\n"
            "  SHA-256 in the key`, and transit integrity is a different, weaker claim. ETag\n"
            "  is explicitly not a data-integrity guarantee. The remaining honest option is to\n"
            "  read the object back at completion and hash it, refusing adoption on mismatch —\n"
            "  which costs one full download per upload and gives back part of why writes\n"
            "  bypass the API. Send this output before changing any storage code.\n"
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
