from __future__ import annotations

from typing import Any

from production_domain.models import RetryCategory

from .base import (
    GenerationProvider,
    ProviderHealth,
    ProviderJob,
    ProviderPollIdentity,
    ProviderSubmission,
)
from .errors import ProviderError


class NotConfiguredProvider(GenerationProvider):
    def __init__(self, name: str):
        self.name = name

    def _error(self) -> ProviderError:
        return ProviderError(
            f"{self.name} provider is reserved but not configured in V1",
            RetryCategory.PERMANENT_ERROR,
            code="PROVIDER_NOT_CONFIGURED",
        )

    async def generate_image(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        raise self._error()

    async def generate_video(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        raise self._error()

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str:
        raise self._error()

    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str) -> bool:
        raise self._error()

    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
        poll_identity: ProviderPollIdentity | None = None,
    ) -> ProviderJob:
        del poll_identity
        raise self._error()

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool:
        raise self._error()

    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None:
        raise self._error()

    async def health(self) -> ProviderHealth:
        return ProviderHealth(False, f"{self.name} is not configured in V1")
