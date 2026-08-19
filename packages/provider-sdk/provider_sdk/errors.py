from __future__ import annotations

from production_domain.models import RetryCategory


class ProviderError(RuntimeError):
    def __init__(
        self, message: str, category: RetryCategory, *, code: str = "PROVIDER_ERROR", submitted: bool = False
    ):
        super().__init__(message)
        self.category = category
        self.code = code
        self.submitted = submitted


__all__ = ["ProviderError", "RetryCategory"]
