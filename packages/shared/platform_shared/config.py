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
    # returns for a finished clip, read from a real completed job. Ark and
    # DashScope stay unlisted until a canary shows what theirs are: a guessed
    # host is either a hole in an SSRF fence or another silent failure.
    provider_media_allowed_hosts: str = (
        "google_flow=labs.google,*.googleusercontent.com,*.googleapis.com,*.googlevideo.com;"
        "openrouter=openrouter.ai,*.openrouter.ai"
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
    depay_payment_link_url: str = ""
    depay_link_id: str = ""
    depay_callback_public_key: str = ""
    depay_checkout_ttl_minutes: int = 1_440
    depay_offer_amount_usdc: Decimal = Decimal("30")
    depay_offer_credits: int = 3_000
    depay_offer_upgrade_plan: Literal["PRO"] = "PRO"
    skills_root: Path = Path("./skills")
    model_infrastructure_config: Path = Path("./config/model-registry/defaults.json")
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
