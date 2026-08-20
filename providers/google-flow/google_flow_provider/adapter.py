from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from browser_runtime import BrowserCommandTimeout, BrowserRuntime, WorkerDisconnected
from platform_database import Database
from platform_shared import Settings
from production_domain.models import ProviderAccount, RetryCategory
from provider_sdk import GenerationProvider, ProviderError, ProviderHealth, ProviderJob, ProviderSubmission

from .mapper import image_payload, video_payload


class GoogleFlowProvider(GenerationProvider):
    name = "google_flow"

    def __init__(self, runtime: BrowserRuntime, settings: Settings, database: Database | None = None):
        self.runtime = runtime
        self.settings = settings
        self.database = database

    def _project_id(self, account_id: str) -> str:
        if self.database:
            with self.database.session() as session:
                account = session.get(ProviderAccount, account_id)
                if account and account.metadata_json.get("project_id"):
                    return str(account.metadata_json["project_id"])
        return self.settings.flow_project_id

    def _url(self, endpoint: str) -> str:
        base = self.settings.flow_api_base.rstrip("/")
        suffix = f"?key={self.settings.flow_api_key}" if self.settings.flow_api_key else ""
        return f"{base}{endpoint}{suffix}"

    async def _request(
        self,
        worker_id: str,
        endpoint: str,
        body: dict[str, Any] | None = None,
        *,
        method: str = "POST",
        captcha_action: str = "",
        generation_job_id: str | None = None,
        submitted: bool = False,
    ) -> dict[str, Any]:
        try:
            result = await self.runtime.dispatch(
                worker_id,
                "provider.request",
                {
                    "provider": self.name,
                    "url": self._url(endpoint),
                    "method": method,
                    "headers": {"Content-Type": "application/json"},
                    "body": body or {},
                    "captcha_action": captcha_action,
                },
                generation_job_id=generation_job_id,
                timeout_seconds=self.settings.browser_command_timeout_seconds,
            )
        except (BrowserCommandTimeout, WorkerDisconnected) as exc:
            raise ProviderError(
                str(exc), RetryCategory.WORKER_DISCONNECT, code="WORKER_DISCONNECTED", submitted=submitted
            ) from exc
        except RuntimeError as exc:
            message = str(exc)
            if "WORKER_NEEDS_USER_ACTION" in message or "NO_FLOW_KEY" in message:
                raise ProviderError(
                    message,
                    RetryCategory.CREDENTIAL_EXPIRED,
                    code="WORKER_NEEDS_USER_ACTION",
                    submitted=False,
                ) from exc
            raise ProviderError(
                message, RetryCategory.TRANSIENT_NETWORK, code="BROWSER_REQUEST_FAILED", submitted=submitted
            ) from exc
        status = int(result.get("status") or 0)
        if 200 <= status < 300:
            data = result.get("data")
            return data if isinstance(data, dict) else result
        data = result.get("data") or result.get("error") or {}
        message = str(data)
        if status == 401:
            category, code = RetryCategory.CREDENTIAL_EXPIRED, "CREDENTIAL_EXPIRED"
        elif status == 429:
            category, code = RetryCategory.RATE_LIMIT, "RATE_LIMIT"
        elif status in {502, 503, 504}:
            category, code = RetryCategory.PROVIDER_BUSY, "PROVIDER_BUSY"
        elif status in {400, 422}:
            category, code = RetryCategory.INVALID_REQUEST, "INVALID_REQUEST"
        elif status in {403, 451}:
            category, code = RetryCategory.CONTENT_REJECTED, "CONTENT_REJECTED"
        elif status == 404:
            category, code = RetryCategory.INVALID_REQUEST, "MEDIA_NOT_FOUND"
        else:
            category, code = RetryCategory.PERMANENT_ERROR, "PROVIDER_ERROR"
        submission_is_uncertain = submitted and status in {502, 503, 504}
        raise ProviderError(message, category, code=code, submitted=submission_is_uncertain)

    @staticmethod
    def _media_ids(data: dict[str, Any]) -> list[str]:
        media = data.get("media") or []
        return [
            str(item.get("name") or item.get("mediaId"))
            for item in media
            if item.get("name") or item.get("mediaId")
        ]

    async def generate_video(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        project_id = str(request.get("_provider_project_id") or self._project_id(account_id))
        if not project_id:
            raise ProviderError(
                "Google Flow project ID is not configured",
                RetryCategory.INVALID_REQUEST,
                code="FLOW_PROJECT_MISSING",
            )
        endpoint, body = video_payload(request, project_id)
        data = await self._request(
            worker_id,
            endpoint,
            body,
            captcha_action="VIDEO_GENERATION",
            generation_job_id=request.get("_generation_job_id"),
            submitted=True,
        )
        media_ids = self._media_ids(data)
        if not media_ids:
            raise ProviderError(
                "Google Flow returned no media ID",
                RetryCategory.PERMANENT_ERROR,
                code="MISSING_PROVIDER_JOB",
                submitted=True,
            )
        return ProviderSubmission(media_ids[0], data)

    async def generate_image(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        project_id = str(request.get("_provider_project_id") or self._project_id(account_id))
        if not project_id:
            raise ProviderError(
                "Google Flow project ID is not configured",
                RetryCategory.INVALID_REQUEST,
                code="FLOW_PROJECT_MISSING",
            )
        endpoint, body = image_payload(request, project_id)
        data = await self._request(
            worker_id,
            endpoint,
            body,
            captcha_action="IMAGE_GENERATION",
            generation_job_id=request.get("_generation_job_id"),
            submitted=True,
        )
        media_ids = self._media_ids(data)
        if not media_ids:
            raise ProviderError(
                "Google Flow returned no image media ID",
                RetryCategory.PERMANENT_ERROR,
                code="MISSING_PROVIDER_JOB",
                submitted=True,
            )
        return ProviderSubmission(media_ids[0], data)

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str:
        project_id = str(asset.get("_provider_project_id") or self._project_id(account_id))
        if not project_id:
            raise ProviderError(
                "Google Flow project ID is not configured",
                RetryCategory.INVALID_REQUEST,
                code="FLOW_PROJECT_MISSING",
            )
        path = Path(asset["local_path"])
        image_bytes = base64.b64encode(path.read_bytes()).decode("ascii")
        data = await self._request(
            worker_id,
            "/v1/flow/uploadImage",
            {
                "clientContext": {"tool": "PINHOLE", "projectId": project_id},
                "imageBytes": image_bytes,
            },
            generation_job_id=asset.get("generation_job_id"),
        )
        media_id = data.get("mediaId") or data.get("name")
        if not media_id and isinstance(data.get("media"), dict):
            media_id = data["media"].get("name") or data["media"].get("mediaId")
        if not media_id:
            raise ProviderError(
                "asset upload returned no media ID",
                RetryCategory.PERMANENT_ERROR,
                code="UPLOAD_MEDIA_ID_MISSING",
            )
        return str(media_id)

    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str) -> bool:
        del account_id
        try:
            await self._request(worker_id, f"/v1/media/{provider_media_id}", method="GET")
            return True
        except ProviderError as exc:
            if exc.code in {"INVALID_REQUEST", "MEDIA_NOT_FOUND"} or "404" in str(exc):
                return False
            raise

    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
    ) -> ProviderJob:
        if generation_type == "image":
            if not await self.validate_asset(provider_job_id, account_id=account_id, worker_id=worker_id):
                return ProviderJob(provider_job_id, "RUNNING")
            response = await self.runtime.dispatch(
                worker_id,
                "provider.media_url",
                {"provider": self.name, "media_id": provider_job_id},
                timeout_seconds=self.settings.browser_command_timeout_seconds,
            )
            return ProviderJob(
                provider_job_id,
                "COMPLETED",
                progress=1,
                output_url=response.get("url"),
                output_mime_type="image/png",
            )
        project_id = self._project_id(account_id)
        data = await self._request(
            worker_id,
            "/v1/video:batchCheckAsyncVideoGenerationStatus",
            {
                "media": [{"name": provider_job_id, "projectId": project_id}],
            },
        )
        media = data.get("media") or []
        if not media:
            return ProviderJob(provider_job_id, "RUNNING", raw=data)
        state = ((media[0].get("mediaMetadata") or {}).get("mediaStatus") or {}).get(
            "mediaGenerationStatus", ""
        )
        if "SUCCESSFUL" in state:
            response = await self.runtime.dispatch(
                worker_id,
                "provider.media_url",
                {
                    "provider": self.name,
                    "media_id": provider_job_id,
                },
                timeout_seconds=self.settings.browser_command_timeout_seconds,
            )
            return ProviderJob(
                provider_job_id,
                "COMPLETED",
                progress=1,
                output_url=response.get("url"),
                output_mime_type="video/mp4",
                raw=data,
            )
        if "FAILED" in state or "BLOCKED" in state:
            return ProviderJob(provider_job_id, "FAILED", error=state, raw=data)
        return ProviderJob(provider_job_id, "RUNNING", progress=0.5, raw=data)

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool:
        del provider_job_id, account_id, worker_id
        return False

    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None:
        del account_id
        data = await self._request(worker_id, "/v1/credits", method="GET")
        value = data.get("credits") or data.get("remainingCredits")
        return int(value) if value is not None else None

    async def health(self) -> ProviderHealth:
        workers = self.runtime.available_workers(self.name)
        if not workers:
            return ProviderHealth(False, "No connected Google Flow browser worker")
        return ProviderHealth(
            True, f"{len(workers)} browser worker(s) ready", {"workers": [worker.id for worker in workers]}
        )
