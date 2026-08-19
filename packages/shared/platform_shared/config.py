from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/platform.db"
    storage_root: Path = Path("./data/media")
    public_base_url: str = "http://localhost:8080"
    platform_api_key: str = ""
    credential_encryption_key: str = ""
    worker_poll_interval_seconds: float = 1.0
    worker_heartbeat_timeout_seconds: int = 30
    browser_command_timeout_seconds: int = 75
    flow_api_base: str = "https://aisandbox-pa.googleapis.com"
    flow_api_key: str = ""
    flow_project_id: str = ""
    skills_root: Path = Path("./skills")
