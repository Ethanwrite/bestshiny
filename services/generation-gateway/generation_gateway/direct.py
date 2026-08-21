from __future__ import annotations

import uuid

from platform_database import Database
from production_domain.models import (
    AccountStatus,
    BrowserWorker,
    ProviderAccount,
    WorkerStatus,
)
from sqlalchemy import select

_DIRECT_ACCOUNT_NAMESPACE = uuid.UUID("db3f4c72-6b57-57a4-a955-1884e90d0bf7")


class DirectAPIResourceRegistry:
    """Register scheduler capacity for server-side HTTP adapters.

    The existing scheduler owns concurrency and release accounting for every
    generation job. A direct adapter therefore receives a synthetic resource
    worker, but no credential, API key, session, or token is persisted.
    """

    def __init__(self, database: Database):
        self.database = database

    def ensure_provider(
        self,
        provider: str,
        *,
        supported_models: set[str],
        capabilities: set[str],
        max_jobs: int = 2,
    ) -> tuple[str, str]:
        provider = provider.strip()
        models = sorted(model.strip() for model in supported_models if model.strip())
        media_capabilities = sorted(capabilities.intersection({"image", "video"}))
        if not provider or not models or not media_capabilities:
            raise ValueError("provider, supported models, and media capabilities are required")
        if max_jobs < 1:
            raise ValueError("max_jobs must be positive")
        account_id = str(uuid.uuid5(_DIRECT_ACCOUNT_NAMESPACE, f"account:{provider}"))
        worker_id = f"direct-api:{provider}"
        marker = {"resource_kind": "DIRECT_API", "stores_provider_secret": False}
        with self.database.session() as session:
            account = session.get(ProviderAccount, account_id)
            if account is None:
                conflicting = session.scalar(
                    select(ProviderAccount).where(
                        ProviderAccount.provider == provider,
                        ProviderAccount.account_identifier == f"direct-api://{provider}",
                    )
                )
                account = conflicting or ProviderAccount(
                    id=account_id,
                    provider=provider,
                    account_identifier=f"direct-api://{provider}",
                )
                if conflicting is None:
                    session.add(account)
            elif account.metadata_json.get("resource_kind") != "DIRECT_API":
                raise RuntimeError("direct API account ID collides with a non-system account")
            account.status = AccountStatus.READY.value
            # Scheduler eligibility sentinel, not the remote provider balance.
            # Actual spend is recorded by the cost and provider-budget layers.
            account.credits = 1
            account.image_capacity = max_jobs if "image" in media_capabilities else 0
            account.video_capacity = max_jobs if "video" in media_capabilities else 0
            account.supported_models = models
            account.metadata_json = marker
            session.flush()

            worker = session.get(BrowserWorker, worker_id)
            if worker is None:
                worker = BrowserWorker(
                    id=worker_id,
                    provider=provider,
                    account_id=account.id,
                    connection_id=worker_id,
                )
                session.add(worker)
            elif worker.metadata_json.get("resource_kind") != "DIRECT_API":
                raise RuntimeError("direct API worker ID collides with a non-system worker")
            worker.provider = provider
            worker.account_id = account.id
            worker.connection_id = worker_id
            worker.status = WorkerStatus.READY.value
            worker.capabilities = media_capabilities
            worker.max_jobs = max_jobs
            worker.metadata_json = marker
            account.worker_id = worker.id
            session.flush()
            return account.id, worker.id


__all__ = ["DirectAPIResourceRegistry"]
