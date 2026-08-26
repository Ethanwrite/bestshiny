"""Grant the Web origins a browser-side PUT against the media bucket.

Direct upload takes the API out of the media path: the browser transfers bytes
straight to object storage under a presigned URL. That only works if the bucket
itself answers the preflight, and a bucket with no CORS configuration answers
`OPTIONS` with 403 — which is what `verify_object_storage.py` reports as

    [FAIL] browser CORS · http://localhost:3000   HTTP 403

The rule is derived from what the platform actually sends, not from a wildcard:
origins come from `WEB_ORIGINS`, and the allowed request headers are the ones
`S3CompatibleStorage.presigned_upload` binds into the signature. `ETag` is
exposed because the browser has to read it back to report a completed transfer.

    uv run python scripts/configure_object_storage_cors.py            # plan only
    uv run python scripts/configure_object_storage_cors.py --apply

`PutBucketCors` replaces the whole configuration rather than merging, so an
existing configuration this script did not write is printed and left alone
unless `--force` says otherwise. Prints no secret.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from botocore.exceptions import ClientError  # noqa: E402
from platform_shared import S3CompatibleStorage, Settings  # noqa: E402

# What a presigned PUT puts on the wire. `Content-Type` and the SHA-256 checksum
# are signed headers, so the browser cannot drop them to dodge a preflight; both
# have to be allowed by name or the transfer never starts. Content-MD5 rides
# along for the stores that take it instead of the checksum header.
UPLOAD_HEADERS = ["content-type", "content-md5", "x-amz-checksum-sha256"]

# GET/HEAD are here because renditions and reference images are read back into
# the page from the same bucket. POST and DELETE are not: nothing in the browser
# is allowed to create or remove an object except through a presigned PUT.
METHODS = ["PUT", "GET", "HEAD"]

EXPOSE = ["ETag", "x-amz-checksum-sha256", "x-oss-request-id"]

MAX_AGE_SECONDS = 3000


def _rules(origins: list[str]) -> list[dict[str, object]]:
    return [
        {
            "AllowedOrigins": origins,
            "AllowedMethods": METHODS,
            "AllowedHeaders": UPLOAD_HEADERS,
            "ExposeHeaders": EXPOSE,
            "MaxAgeSeconds": MAX_AGE_SECONDS,
        }
    ]


def _normalise(rules: list[dict[str, object]]) -> list[dict[str, object]]:
    """Compare rules by content, so re-running is a no-op rather than a rewrite."""

    def key(value: object) -> object:
        return sorted(str(item).lower() for item in value) if isinstance(value, list) else value

    return [{field: key(value) for field, value in sorted(rule.items())} for rule in rules]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the rule; otherwise print the plan")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace a CORS configuration this script did not write",
    )
    args = parser.parse_args()

    settings = Settings()
    if settings.storage_backend != "s3":
        print(f"\n  STORAGE_BACKEND is {settings.storage_backend!r}; nothing to configure.\n")
        return 0

    origins = [origin.strip().rstrip("/") for origin in settings.web_origins.split(",") if origin.strip()]
    if not origins:
        print("\n  WEB_ORIGINS is empty — there is no browser origin to grant.\n")
        return 1

    storage = S3CompatibleStorage(
        bucket=settings.s3_bucket,
        cache_root=settings.storage_root,
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
        public_base_url=settings.public_base_url,
        addressing_style=settings.s3_addressing_style,
        enforce_checksum=settings.s3_enforce_upload_checksum,
    )

    wanted = _rules(origins)
    print(f"\n  bucket   {settings.s3_bucket}")
    print(f"  endpoint {settings.s3_endpoint_url}")
    print("  rule     " + json.dumps(wanted[0], indent=2).replace("\n", "\n           "))

    try:
        current = storage.client.get_bucket_cors(Bucket=settings.s3_bucket).get("CORSRules", [])
    except ClientError as exc:
        code = exc.response["Error"].get("Code", "")
        if code not in {"NoSuchCORSConfiguration", "NoSuchCORSConfigurationError"}:
            print(f"\n  [FAIL] GetBucketCors — {code}: {exc.response['Error'].get('Message', '')}\n")
            return 1
        current = []

    if _normalise(current) == _normalise(wanted):
        print("\n  Already configured. Nothing to do.\n")
        return 0
    if current and not args.force:
        print("\n  This bucket already carries a CORS configuration:\n")
        print("  " + json.dumps(current, indent=2).replace("\n", "\n  "))
        print("\n  PutBucketCors replaces it wholesale. Re-run with --force to do that.\n")
        return 1

    if not args.apply:
        print("\n  Plan only. Re-run with --apply to write it.\n")
        return 0

    try:
        storage.client.put_bucket_cors(
            Bucket=settings.s3_bucket,
            CORSConfiguration={"CORSRules": wanted},
        )
    except ClientError as exc:
        error = exc.response["Error"]
        print(f"\n  [FAIL] PutBucketCors — {error.get('Code', '')}: {error.get('Message', '')}")
        print("  The service account needs oss:PutBucketCors, or set the rule in the OSS console.\n")
        return 1

    applied = storage.client.get_bucket_cors(Bucket=settings.s3_bucket).get("CORSRules", [])
    print("\n  Applied. Bucket now reports:\n")
    print("  " + json.dumps(applied, indent=2).replace("\n", "\n  "))
    print("\n  Confirm with: uv run python scripts/verify_object_storage.py\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
