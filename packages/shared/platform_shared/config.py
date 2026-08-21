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
    provider_media_allowed_hosts: str = (
        "google_flow=labs.google,*.googleusercontent.com,*.googleapis.com,*.googlevideo.com"
    )
    s3_endpoint_url: str = ""
    s3_region: str = "us-east-1"
    s3_bucket: str = "ai-director-media"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    public_base_url: str = "http://localhost:8080"
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
    provider_mode: Literal["mock", "recorded", "live"] = "mock"
    allow_live_provider_calls: bool = False
    live_provider_confirmation: str = ""
    provider_http_timeout_seconds: float = 120
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
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
    skills_root: Path = Path("./skills")
    model_infrastructure_config: Path = Path("./config/model-registry/defaults.json")
    feature_voyage_memory: bool = False
    feature_auto_evaluation: bool = False
    feature_adaptive_router: bool = False
    feature_auto_retry: bool = False
    voyage_api_key: str = ""
    voyage_multimodal_model: str = "voyage-multimodal-3.5"
    memory_embedding_dimension: int = 512
    memory_max_characters: int = 12_000
    memory_max_tokens: int = 3_000
    memory_max_images: int = 8
    memory_max_videos: int = 2
    max_auto_retries: int = 2
