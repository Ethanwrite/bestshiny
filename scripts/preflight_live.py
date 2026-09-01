"""Report what a live run can and cannot do — before any money is spent.

Makes **no network call** and prints **no secret value**. It reads the same
`Settings` the application does and answers one question per generation path:
if you flipped the gate right now, would this path reach the provider, fail
closed locally, or fail at the provider after being billed?

The last of those is the one worth catching here. A reference-carrying shot
whose reference URL points at `localhost` is not refused by this platform —
it is refused by Alibaba's fetcher, after submission.

    uv run python scripts/preflight_live.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from platform_shared import Settings  # noqa: E402
from provider_sdk import LIVE_PROVIDER_CONFIRMATION  # noqa: E402

READY = "READY"
BLOCKED = "BLOCKED"
RISK = "AT RISK"


def _line(label: str, state: str, detail: str = "") -> str:
    mark = {READY: "ok  ", BLOCKED: "STOP", RISK: "WARN"}[state]
    return f"  [{mark}] {label:34} {detail}"


def _production_mode(settings: Settings) -> bool:
    """Whether `DEPLOYMENT_ENVIRONMENT=production` would even start.

    The container enforces this itself and refuses to boot otherwise, so this
    reports the same two conditions rather than inventing a third opinion.
    """

    print("\n=== Production mode " + "=" * 56)
    is_production = settings.deployment_environment == "production"
    print(
        _line(
            "DEPLOYMENT_ENVIRONMENT",
            READY if is_production else BLOCKED,
            settings.deployment_environment,
        )
    )
    api_key = settings.platform_api_key.encode("utf-8")
    strong_api_key = len(api_key) >= 32 and len(set(api_key)) >= 16
    print(
        _line(
            "PLATFORM_API_KEY",
            READY if strong_api_key else BLOCKED,
            "" if strong_api_key else "production refuses to boot: needs >=32 bytes, >=16 distinct",
        )
    )
    encryption = settings.credential_encryption_key.strip()
    print(
        _line(
            "CREDENTIAL_ENCRYPTION_KEY",
            READY if encryption else BLOCKED,
            "" if encryption else "production refuses to boot: needs a Fernet.generate_key() value",
        )
    )
    print(_line("AUTH_REQUIRED", READY if settings.auth_required else BLOCKED, ""))
    secure_cookies = is_production
    https_origin = urlsplit(settings.public_base_url).scheme == "https"
    if secure_cookies and not https_origin:
        print(
            _line(
                "PUBLIC_BASE_URL over HTTPS",
                BLOCKED,
                "production sets Secure cookies; a browser will not send them over http",
            )
        )
    return is_production and strong_api_key and bool(encryption)


def _payment(settings: Settings) -> None:
    print("\n=== Payment " + "=" * 63)
    alchemy = [
        ("ALCHEMY_WEBHOOK_SIGNING_KEY", bool(settings.alchemy_webhook_signing_key.strip())),
        ("ALCHEMY_WEBHOOK_ID", bool(settings.alchemy_webhook_id.strip())),
        ("ALCHEMY_TREASURY_ADDRESS", bool(settings.alchemy_treasury_address.strip())),
        ("ALCHEMY_CREDITING_DISABLED", settings.alchemy_crediting_enabled is False),
    ]
    for label, ok in alchemy:
        print(_line(label, READY if ok else BLOCKED, ""))
    depay = [
        ("DEPAY_INTEGRATION_ID", bool(settings.depay_integration_id.strip())),
        ("DEPAY_CALLBACK_PUBLIC_KEY", bool(settings.depay_callback_public_key.strip())),
        (
            "DEPAY_DYNAMIC_CONFIG_PRIVATE_KEY",
            bool(settings.depay_dynamic_config_private_key.strip()),
        ),
    ]
    for label, ok in depay:
        print(_line(label, READY if ok else BLOCKED, ""))
    relayer = [
        ("RELAYER_ADDRESS", bool(settings.relayer_address.strip())),
        ("RELAYER_PRIVATE_KEY", bool(settings.relayer_private_key.strip())),
        ("BASE_RPC_URL", settings.base_rpc_url.strip().startswith("https://")),
    ]
    for label, ok in relayer:
        print(_line(label, READY if ok else BLOCKED, ""))
    if not all(ok for _label, ok in alchemy + depay + relayer):
        print(
            "\n  On-chain payment cannot be exercised end to end. Treasury/provider keys and\n"
            "  the Base relayer account must be configured outside the repository."
        )


def main() -> int:
    settings = Settings()
    production_ready = _production_mode(settings)
    _payment(settings)
    print("\n=== Live gate " + "=" * 62)
    gate_parts = [
        ("PROVIDER_MODE=live", settings.provider_mode == "live", settings.provider_mode),
        ("ALLOW_LIVE_PROVIDER_CALLS", settings.allow_live_provider_calls is True, ""),
        (
            "LIVE_PROVIDER_CONFIRMATION",
            settings.live_provider_confirmation == LIVE_PROVIDER_CONFIRMATION,
            "",
        ),
    ]
    gate_open = all(ok for _label, ok, _detail in gate_parts)
    for label, ok, detail in gate_parts:
        print(_line(label, READY if ok else BLOCKED, detail))
    print(
        _line(
            "PLATFORM_API_KEY",
            READY if settings.platform_api_key.strip() else BLOCKED,
            "" if settings.platform_api_key.strip() else "needed to mint a LiveCanaryPermit",
        )
    )
    print(
        "\n  Providers are "
        + ("LIVE — calls will be billed." if gate_open else "offline. Nothing can be billed.")
    )

    print("\n=== Reference media (what every I2V/R2V shot needs) " + "=" * 25)
    s3_ready = bool(
        settings.s3_endpoint_url.strip()
        and settings.s3_access_key_id.strip()
        and settings.s3_secret_access_key.strip()
    )
    # Configured is not the same as reachable. A bucket on localhost presigns
    # perfectly and hands the provider a URL it cannot resolve, so the shot is
    # submitted, billed and then fails at the far end — the one outcome this
    # script exists to catch.
    s3_host = urlsplit(settings.s3_endpoint_url).hostname or ""
    s3_private = s3_host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or s3_host.startswith(
        ("192.168.", "10.", "172.16.", "host.docker.")
    )
    if s3_ready and s3_private:
        print(
            _line(
                "S3 object storage",
                RISK,
                f"{s3_host} is not routable from outside this machine",
            )
        )
        print(
            _line(
                "  → provider fetch",
                BLOCKED,
                "Alibaba cannot resolve this; I2V/R2V would submit, bill, then fail",
            )
        )
    else:
        print(_line("S3 object storage", READY if s3_ready else BLOCKED, settings.s3_bucket or ""))
    public = settings.public_base_url.strip()
    host = urlsplit(public).hostname or ""
    https = urlsplit(public).scheme == "https"
    local = host in {"localhost", "127.0.0.1", "::1", ""}
    if not s3_ready:
        if settings.local_reference_signing_key.strip():
            state = BLOCKED if (gate_open and (not https or local)) else RISK
            why = "signed local route proxies through the API"
            if not https:
                why += "; live mode refuses a non-HTTPS reference URL"
            elif local:
                why += "; a provider cannot fetch localhost"
            print(_line("PUBLIC_BASE_URL fallback", state, why))
        else:
            print(_line("PUBLIC_BASE_URL fallback", BLOCKED, "no signing key; no reference at all"))

    print("\n=== Project style lock " + "=" * 54)
    # Worth its own section because the lock is append-only and a trigger
    # forbids re-locking. With layer 2 enabled the lock now fails closed rather
    # than quietly writing a permanent single-layer gate, so "will locking work"
    # is a question the operator has to be able to answer *before* trying.
    if not settings.feature_semantic_style_lock:
        print(_line("FEATURE_SEMANTIC_STYLE_LOCK", READY, "off — locks are deliberately single-layer"))
    else:
        print(_line("FEATURE_SEMANTIC_STYLE_LOCK", READY, "on — layer 2 is required to lock"))
        embedding_key = settings.openrouter_api_key.strip()
        if not gate_open:
            print(
                _line(
                    "STYLE_SEMANTIC_EMBEDDING",
                    BLOCKED,
                    "PROVIDER_MODE is not live; every lock attempt will be refused",
                )
            )
        elif not embedding_key:
            print(
                _line(
                    "STYLE_SEMANTIC_EMBEDDING",
                    BLOCKED,
                    "OPENROUTER_API_KEY is unset; every lock attempt will be refused",
                )
            )
        else:
            print(
                _line(
                    "STYLE_SEMANTIC_EMBEDDING",
                    RISK,
                    "google/gemini-embedding-2 has never been called; locking is unproven",
                )
            )
        print(
            "\n  A refused lock costs nothing and can be retried. Locking is the one\n"
            "  action here that cannot be undone: the row is append-only and a trigger\n"
            "  forbids replacing it."
        )

    print("\n=== Wan 2.7 paths " + "=" * 58)
    wan_transport = bool(settings.wan_api_key.strip() and settings.wan_dashscope_base_url.strip())
    modes = (
        ("T2V  text only", settings.wan2_7_t2v_model_id, False),
        ("I2V  first frame / clip", settings.wan2_7_i2v_model_id, True),
        ("R2V  frame + references", settings.wan2_7_r2v_model_id, True),
    )
    for label, model_id, needs_reference in modes:
        if not (wan_transport and model_id.strip()):
            print(_line(label, BLOCKED, "transport or model ID not configured"))
        elif needs_reference and not s3_ready:
            print(_line(label, BLOCKED, "needs object storage for its reference URLs"))
        elif needs_reference and s3_private:
            print(_line(label, BLOCKED, "reference URLs point at a host the provider cannot reach"))
        elif not gate_open:
            print(_line(label, RISK, "configured; blocked only by the live gate"))
        else:
            print(_line(label, READY, model_id))
    print(
        _line(
            "Wan 3.0",
            BLOCKED if not settings.wan_video_model_keys.strip() else READY,
            "invitation-only Beta; declare WAN_VIDEO_MODEL_KEYS=wan-3.0=<id>",
        )
    )

    print("\n=== Verdict " + "=" * 63)
    blockers: list[str] = []
    secrets_ready = bool(settings.platform_api_key.strip() and settings.credential_encryption_key.strip())
    if not secrets_ready:
        blockers.append(
            "Production secrets. The container refuses to boot without PLATFORM_API_KEY and\n"
            "    CREDENTIAL_ENCRYPTION_KEY:\n"
            '      python -c "import secrets; print(secrets.token_urlsafe(48))"\n'
            '      python -c "from cryptography.fernet import Fernet;'
            ' print(Fernet.generate_key().decode())"'
        )
    elif not production_ready:
        blockers.append(
            "DEPLOYMENT_ENVIRONMENT=production. Both secrets are in place, so this is now\n"
            "    one line — but production sets Secure cookies, which a browser will not send\n"
            "    over http. Flip it once PUBLIC_BASE_URL is HTTPS, not before."
        )
    if not s3_ready:
        blockers.append(
            "Object storage. Only Wan T2V works without it: every I2V and R2V shot carries a\n"
            "    reference the provider fetches itself, and there is no URL Alibaba can reach.\n"
            "    Direct uploads answer 501 for the same reason."
        )
    elif s3_private:
        blockers.append(
            "A publicly reachable bucket. The storage plane is real and verified — presign,\n"
            "    digest enforcement, HEAD, range read and reference URLs all work — but the\n"
            "    endpoint is local, so a provider cannot fetch what it hands out. Direct\n"
            "    uploads and the rendition plane are fully testable now; live I2V/R2V needs\n"
            "    Alibaba OSS, R2 or S3 with an HTTPS endpoint."
        )
    if not gate_open:
        blockers.append(
            "The live gate. PROVIDER_MODE is the last switch and it is yours to throw:\n"
            "      PROVIDER_MODE=live      # every provider transport becomes billable"
        )
    blockers.append(
        "After adding any credential, open the models it unlocks — a restart will not:\n"
        "      curl -XPOST -H 'Authorization: Bearer $PLATFORM_API_KEY' \\\n"
        "        localhost:8080/internal/models/reconcile-live          # report\n"
        "      ...same URL with ?apply=true                            # write"
    )
    for index, item in enumerate(blockers, start=1):
        print(f"  {index}. {item}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
