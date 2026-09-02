from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/platform.db"
    storage_root: Path = Path("./data/media")
    storage_backend: str = "local"
    max_upload_bytes: int = 100 * 1024 * 1024
    max_upload_request_overhead_bytes: int = 65_536
    max_image_pixels: int = 50_000_000
    max_provider_download_bytes: int = 100 * 1024 * 1024
    # A provider with no entry here cannot deliver a result: the transfer stage
    # fails closed, which is the right default for a host nobody has confirmed
    # and the wrong one for a provider the platform actually routes to. Only
    # `google_flow` was ever listed, so every OpenRouter, Ark and DashScope
    # generation reached `COMPLETED` at the provider — billed — and then failed
    # at the fetch with "provider media host is not allowlisted".
    #
    # `openrouter.ai` is the host OpenRouter's own `/v1/videos/{id}` response
    # returns for a finished clip, read from a real completed job. DashScope
    # stays unlisted until a canary shows what its return host is: a guessed
    # host is either a hole in an SSRF fence or another silent failure.
    #
    # `*.tos-cn-beijing.volces.com` is the host Ark's images API returned for a
    # real completed, billed generation on 2026-08-30
    # (`ark-acg-cn-beijing.tos-cn-beijing.volces.com`, job e2d97342 on
    # production) — observed, not guessed, which is the bar this list demands.
    provider_media_allowed_hosts: str = (
        "google_flow=labs.google,*.googleusercontent.com,*.googleapis.com,*.googlevideo.com;"
        "openrouter=openrouter.ai,*.openrouter.ai;"
        "seedance=*.tos-cn-beijing.volces.com"
    )
    s3_endpoint_url: str = ""
    s3_region: str = "us-east-1"
    s3_bucket: str = "ai-director-media"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    # "virtual" for Alibaba OSS (bucket.s3.oss-<region>.aliyuncs.com), "path" for
    # MinIO, "auto" to let boto3 guess. Stating it beats discovering a 404.
    s3_addressing_style: Literal["auto", "virtual", "path"] = "auto"
    # Bind x-amz-checksum-sha256 into the presigned PUT so the store rejects
    # bytes that do not hash to the declared digest. Turn off only for a store
    # that does not implement it, and know that the content-addressed key then
    # names content the object is merely claimed to hold.
    s3_enforce_upload_checksum: bool = True
    # Alibaba OSS accepts the S3 checksum header but does not enforce it. When
    # enabled, direct-upload completion streams the object once from storage
    # and verifies the SHA-256 before adopting the content-addressed key.
    s3_verify_upload_sha256_on_complete: bool = False
    public_base_url: str = "http://localhost:8080"
    # How long a provider-facing reference URL stays valid. It is a short-lived
    # object-storage credential, never a stored field on the asset row.
    reference_url_ttl_seconds: int = 900
    # Local disk cannot issue an object-storage URL. Setting this turns on a
    # signed, expiring route on *this* service so local development can exercise
    # reference edits — which means the API does proxy those bytes. It is a
    # development affordance, not the deployment shape: configure S3-compatible
    # storage in production and leave this empty.
    local_reference_signing_key: str = ""
    # How long an authorized direct upload stays open. It bounds both the
    # presigned PUT and the quota hold taken against it.
    direct_upload_ttl_seconds: int = 3600
    # A RESERVED storage hold older than this that no PENDING upload accounts
    # for is reported for reconciliation. It is never released automatically:
    # a hold can outlive its request deliberately, when registration succeeded
    # and settlement failed, and releasing that one would make real storage
    # unaccounted.
    storage_reservation_stale_after_seconds: int = 86_400
    # How often the worker reclaims uploads whose authorized window closed
    # without completing. `POST /internal/maintenance/expired-uploads` runs the
    # same sweep on demand; this is what makes it a closed loop in production
    # rather than an endpoint someone has to remember. Set to 0 to disable and
    # drive it entirely from cron or an operator.
    expired_upload_sweep_interval_seconds: int = 300
    # Uploads reclaimed per sweep. A bound, not a target: whatever is left over
    # is picked up by the next one.
    expired_upload_sweep_limit: int = 200
    # How long a staged generation artefact may sit unadopted before the
    # sweeper may reclaim it. Generous on purpose: a slot is only deletable
    # once its job is terminal or unknown and no media row references it, so
    # the TTL is about giving an operator's reconciliation a window, not about
    # correctness.
    generation_staging_ttl_seconds: int = 86_400
    # How often the worker sweeps the generation staging area.
    # `POST /internal/maintenance/generation-staging` runs the same sweep on
    # demand. Set to 0 to disable and drive it from cron or an operator.
    generation_staging_sweep_interval_seconds: int = 3600
    # Staged objects deleted per sweep. A bound, not a target.
    generation_staging_sweep_limit: int = 500
    web_origins: str = "http://localhost:3000,http://127.0.0.1:18081"
    platform_api_key: str = ""
    deployment_environment: Literal["development", "test", "production"] = "production"
    auth_required: bool = True
    auth_session_ttl_days: int = 30
    credential_encryption_key: str = ""
    worker_credential_ttl_seconds: int = 86_400
    worker_socket_ticket_ttl_seconds: int = 60
    worker_poll_interval_seconds: float = 1.0
    generation_poll_interval_seconds: float = 2.0
    worker_heartbeat_timeout_seconds: int = 30
    generation_claim_lease_seconds: int = 300
    browser_command_timeout_seconds: int = 75
    flow_api_base: str = "https://aisandbox-pa.googleapis.com"
    flow_api_key: str = ""
    flow_project_id: str = ""
    # Reviewed "model=runtime_key" pairs; "{duration}" expands to the requested
    # seconds. Undeclared models are rejected instead of silently degrading.
    flow_video_model_keys: str = ""
    provider_mode: Literal["mock", "recorded", "live"] = "mock"
    allow_live_provider_calls: bool = False
    live_provider_confirmation: str = ""
    provider_http_timeout_seconds: float = 120
    # Character Evidence is an isolated Modal CV service. These settings are
    # deliberately unrelated to provider routing: no OpenRouter/Ark transport
    # may satisfy this boundary and there is no local production fallback.
    character_evidence_base_url: str = ""
    character_evidence_api_key: str = ""
    character_evidence_callback_signing_key: str = ""
    character_evidence_threshold_version: str = "character-evidence-thresholds-2026-08-27-v1"
    character_evidence_http_timeout_seconds: float = 15.0
    character_evidence_operating_mode: Literal["shadow", "advisory", "soft_gate", "automatic_gate"] = (
        "shadow"
    )
    # Explicit deployability switch. True keeps the fail-closed production
    # startup checks (HTTPS base URL, key entropy, shadow mode) exactly as
    # they are. False is a *declared operator decision* to run without the
    # Modal service — the producer is not built, submissions stay PENDING and
    # visible, and nothing fails open silently. It exists because the Modal
    # deployment is blocked on external HTTPS reachability and a release must
    # be able to state that fact in configuration rather than in a crash loop.
    character_evidence_enabled: bool = True
    # Maintenance loop cadence for the durable submission lifecycle
    # (enqueue -> dispatch -> ACCEPTED-timeout scan). 0 disables the sweep.
    character_evidence_sweep_interval_seconds: int = 300
    character_evidence_sweep_limit: int = 50
    # An ACCEPTED job whose signed callback has not arrived within this window
    # becomes RECONCILIATION_REQUIRED and waits for an operator.
    character_evidence_callback_timeout_seconds: int = 1800
    # Dispatch attempts (each is one authenticated POST) before a submission
    # is marked FAILED rather than retried.
    character_evidence_max_submission_attempts: int = 5
    # How far back the enqueue scan looks for candidates with registered
    # video output and no submission row.
    character_evidence_backfill_hours: int = 72
    # Derived-rendition garbage collection. Only idle copies whose constraint
    # profile no current provider declares are eligible; originals never are.
    # 0 disables the worker sweep (the internal endpoint still works).
    rendition_gc_interval_seconds: int = 3600
    rendition_gc_limit: int = 100
    # A rendition served inside this window is never collected — it also keeps
    # references handed to in-flight generations alive. Default seven days.
    rendition_gc_min_idle_seconds: int = 7 * 24 * 3600
    # How long one sweeper's claim on a row stays exclusive before a crashed
    # sweep becomes re-claimable by another worker.
    rendition_gc_lease_seconds: int = 600
    # Asynchronous full-content verification of directly uploaded media.
    # 0 disables the worker sweep (the internal endpoint still works).
    media_verification_interval_seconds: int = 60
    media_verification_limit: int = 20
    # A VERIFYING claim older than this lapses and the asset re-verifies —
    # a worker that crashed mid-decode cannot strand an upload.
    media_verification_lease_seconds: int = 900
    # FREE-plan hard usage gates, enforced server-side (browser payloads cannot
    # widen them). Totals per FREE workspace — upgrading the plan lifts them —
    # except the director-round limit, which is per creative session.
    free_plan_max_images: int = 3
    free_plan_max_director_turns: int = 10
    free_plan_max_prompt_optimizations: int = 5
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # The project's image-generation model, served by POST /images.
    openrouter_image_model_id: str = "openai/gpt-image-2"
    # Optional operator-declared envelopes, "model=batch:references[:ratios]".
    # Reviewed built-ins already cover the shipped image models; a model absent
    # from the merged table is rejected instead of guessed at.
    openrouter_image_model_keys: str = ""
    # Cost-affecting and therefore never left to the provider. gpt-image-2 bills
    # per output token, and quality moves that from 196 tokens to 7024 — a 36x
    # range that the provider default (auto) picks from without telling anyone.
    # The pricing profile is written for the value set here, so changing it must
    # be accompanied by a repriced profile; a test ties the two together.
    openrouter_image_quality: Literal["low", "medium", "high"] = "low"
    # OpenRouter defaults this to true and bills the audio rate for it, which is
    # double the silent rate on Veo 3.1. Stated explicitly so the bill matches
    # the quote rather than a default that can move.
    openrouter_video_generate_audio: bool = True
    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    doubao_model_id: str = ""
    seedance_model_id: str = ""
    wan_api_key: str = ""
    wan_openai_base_url: str = ""
    wan_dashscope_base_url: str = ""
    wan_chat_model_id: str = ""
    wan2_7_t2v_model_id: str = ""
    wan2_7_i2v_model_id: str = ""
    wan2_7_r2v_model_id: str = ""
    # Reviewed "model[:mode]=dashscope_model" pairs; modes are t2v, i2v, r2v.
    wan_video_model_keys: str = ""
    runapi_api_key: str = ""
    runapi_base_url: str = ""
    runapi_model_id: str = ""
    runapi_chat_path: str = "/v1/chat/completions"
    runapi_image_path: str = "/v1/images/generations"
    runapi_video_path: str = "/v1/videos"
    runapi_budget_usd: Decimal = Decimal("10")
    allow_runapi_edge_calls: bool = False
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model_id: str = ""
    alchemy_webhook_signing_key: str = ""
    alchemy_webhook_id: str = ""
    alchemy_network: Literal["BASE_MAINNET", "BASE_SEPOLIA"] = "BASE_MAINNET"
    alchemy_treasury_address: str = ""
    alchemy_crediting_enabled: bool = False
    alchemy_usdc_microunits_per_credit: int = 10_000
    reown_project_id: str = ""
    legacy_wallet_payments_enabled: bool = False
    wallet_challenge_ttl_seconds: int = 300
    payment_intent_ttl_minutes: int = 30
    # The DePay Managed Integration the widget pays through, and the object
    # whose Dynamic Configuration calls back to us for a per-order amount.
    depay_integration_id: str = ""
    # The older fixed-offer payment link. Kept because callbacks for payments
    # started against it are still in flight and identify themselves by it.
    depay_payment_link_url: str = ""
    depay_link_id: str = ""
    # DePay issues a key pair per object. This is the Managed Integration's
    # public key, used to verify everything it sends us — Dynamic Configuration
    # requests and its payment callbacks. Found on app.depay.com under the
    # integration itself, not under Dynamic Configuration (that field holds the
    # public half of *our* signing key).
    depay_integration_public_key: str = ""
    # The retired payment link's public key. Kept because callbacks for payments
    # started against the link are still in flight and are signed by it.
    depay_callback_public_key: str = ""
    # DePay verifies our Dynamic Configuration *response* with the public half
    # of this key (RSA-PSS/SHA-256, salt length 64). Without it the widget
    # cannot be priced per order, so checkout fails closed.
    depay_dynamic_config_private_key: str = ""
    depay_checkout_ttl_minutes: int = 1_440
    # DePay deducts its fee before forwarding, so Treasury nets less than the
    # buyer was charged (1.5% on the 2026-08-30 payment). Settlement accepts a
    # shortfall up to this allowance against the order snapshot; beyond it the
    # payment is a mismatch, not a fee. 200 bps = 2%.
    depay_max_provider_fee_bps: int = 200
    # XunHuPay (虎皮椒) CNY checkout. The app secret is server-only and signs
    # both outbound orders and inbound asynchronous notifications.
    xunhupay_app_id: str = ""
    xunhupay_app_secret: str = ""
    xunhupay_gateway_url: str = "https://api.xunhupay.com/payment/do.html"
    xunhupay_notify_url: str = ""
    xunhupay_return_url: str = ""
    xunhupay_checkout_ttl_minutes: int = 30
    xunhupay_timeout_seconds: int = 15
    # Base USDC EIP-3009 relay. The browser signs typed authorization data;
    # only the relayer key reaches the server and pays Base ETH gas.
    relayer_address: str = ""
    relayer_private_key: str = ""
    base_rpc_url: str = ""
    relayer_authorization_ttl_seconds: int = 900
    relayer_min_confirmations: int = 1
    relayer_rpc_timeout_seconds: int = 15
    relayer_max_gas_limit: int = 200_000
    relayer_max_fee_per_gas_wei: int = 5_000_000_000
    relayer_sweep_interval_seconds: int = 5
    relayer_sweep_limit: int = 50
    skills_root: Path = Path("./skills")
    model_infrastructure_config: Path = Path("./config/model-registry/defaults.json")
    # The hand-authored scene_type -> champion/fallback table the video router
    # selects within. Loaded and validated at container build; a missing or
    # malformed table fails startup rather than silently reverting routing
    # policy to open scoring.
    scene_champion_config: Path = Path("./config/model-registry/scene-champions.json")
    # Layer 2 of the style lock. Off by default: it is a paid embedding call per
    # locked style and per evaluated candidate, and it changes what "committable"
    # means, so switching it on is a deliberate act.
    feature_semantic_style_lock: bool = False
    # Whether the External Evidence Registry may influence routing scores.
    # Off by default: the registry ships as a read-only data asset first, so
    # that publishing it changes nothing about which model gets picked. Turning
    # it on affects the holistic `visual_quality` dimension for the models that
    # have exact-version public evidence, plus the per-dimension priors for
    # `veo-3.1-fast`, which is the only video model that has any.
    feature_external_prior: bool = False
    # Whether the offline production posterior may lower a routing score
    # through a conservative lower confidence bound. Off by default, and the
    # default is the point: with it off the router receives exactly the
    # evidence it received before this existed, so publishing the machinery
    # changes no decision. Turning it on additionally requires a replay on
    # file that passed — see `docs/ROUTER_EVIDENCE.md`.
    feature_router_lcb: bool = False
    feature_voyage_memory: bool = False
    feature_auto_evaluation: bool = False
    feature_adaptive_router: bool = False
    feature_auto_retry: bool = False
    memory_embedding_dimension: int = 512
    memory_max_characters: int = 12_000
    memory_max_tokens: int = 3_000
    memory_max_images: int = 8
    memory_max_videos: int = 2
    max_auto_retries: int = 2
