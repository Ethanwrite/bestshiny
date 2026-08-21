from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderSubmission:
    provider_job_id: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderJob:
    provider_job_id: str
    status: str
    progress: float = 0
    output_url: str | None = None
    output_mime_type: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderHealth:
    ok: bool
    detail: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderPollIdentity:
    """Server-owned routing identity for a single remote-job poll."""

    local_generation_job_id: str
    provider_account_id: str
    provider_project_id: str
    provider_job_id: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.local_generation_job_id,
                self.provider_account_id,
                self.provider_project_id,
                self.provider_job_id,
            )
        ):
            raise ValueError("provider poll identity fields cannot be empty")


class GenerationProvider(ABC):
    name: str

    @abstractmethod
    async def generate_image(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission: ...

    @abstractmethod
    async def generate_video(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission: ...

    @abstractmethod
    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str: ...

    @abstractmethod
    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str) -> bool: ...

    @abstractmethod
    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
        poll_identity: ProviderPollIdentity | None = None,
    ) -> ProviderJob: ...

    @abstractmethod
    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool: ...

    @abstractmethod
    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None: ...

    @abstractmethod
    async def health(self) -> ProviderHealth: ...
