from __future__ import annotations

from dataclasses import dataclass

from production_domain.models import RetryCategory


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    delay_seconds: int
    requires_user_action: bool = False


class RetryPolicy:
    automatic = {
        RetryCategory.TRANSIENT_NETWORK: 5,
        RetryCategory.WORKER_DISCONNECT: 5,
        RetryCategory.RATE_LIMIT: 60,
        RetryCategory.PROVIDER_BUSY: 20,
    }

    def decide(
        self, category: RetryCategory, attempt_count: int, max_attempts: int, *, submitted: bool = False
    ) -> RetryDecision:
        if submitted:
            return RetryDecision(False, 0, requires_user_action=True)
        if category == RetryCategory.CREDENTIAL_EXPIRED:
            return RetryDecision(False, 0, requires_user_action=True)
        if category not in self.automatic or attempt_count >= max_attempts:
            return RetryDecision(False, 0)
        base = self.automatic[category]
        return RetryDecision(True, min(base * (2 ** max(0, attempt_count - 1)), 600))
