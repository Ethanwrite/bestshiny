from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ProviderReferenceMode(StrEnum):
    """How a provider expects local media to be handed to it.

    ``PROVIDER_MEDIA_ID`` providers ingest bytes through ``upload_asset`` and
    return a durable remote identifier. ``FETCHABLE_URL`` providers never accept
    an upload; they fetch the bytes themselves and therefore require a real
    URL. Handing such a provider a local asset ID or a provider media ID would
    submit an unresolvable reference, so the two modes must never be mixed.
    """

    PROVIDER_MEDIA_ID = "PROVIDER_MEDIA_ID"
    FETCHABLE_URL = "FETCHABLE_URL"


@dataclass(frozen=True)
class ProviderReferenceConstraints:
    """What a provider will actually accept as a reference image.

    These are transport facts, not creative ones, and they are the reason
    derived renditions exist. A provider that caps a reference at 8 MB is not a
    reason to store the user's 38 MB original at 8 MB — it is a reason to hand
    that one provider a smaller copy.

    ``max_pixels`` bounds width x height; ``max_bytes`` bounds the encoded file.
    ``accepted_mime_types`` is the set the provider documents; an original
    outside it is re-encoded into ``preferred_mime_type`` rather than rejected.
    ``None`` on a bound means the provider declares no limit, which is not the
    same as an unlimited provider — it means we have not established one, so no
    derived copy is made and the original is sent as-is.
    """

    max_pixels: int | None = None
    max_bytes: int | None = None
    accepted_mime_types: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/webp"})
    preferred_mime_type: str = "image/jpeg"

    @property
    def bounded(self) -> bool:
        return self.max_pixels is not None or self.max_bytes is not None

    def accepts(self, *, mime_type: str, pixels: int | None, size_bytes: int) -> bool:
        if mime_type.lower() not in self.accepted_mime_types:
            return False
        if self.max_pixels is not None and pixels is not None and pixels > self.max_pixels:
            return False
        return not (self.max_bytes is not None and size_bytes > self.max_bytes)

    def key(self) -> str:
        """Stable identity of these bounds, so changed limits do not reuse a copy."""

        formats = "+".join(sorted(self.accepted_mime_types))
        return (
            f"px={self.max_pixels or 0};bytes={self.max_bytes or 0};"
            f"fmt={formats};pref={self.preferred_mime_type}"
        )


@dataclass
class ProviderInlineOutput:
    """One generated artefact the provider returned in the response body itself.

    Synchronous image APIs answer with bytes rather than a fetchable URL, so
    there is nothing for the media registry to download. The bytes travel with
    the result instead, and the registry validates and stores them through the
    same path a downloaded artefact takes.
    """

    content: bytes
    mime_type: str = "image/png"

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("inline provider output cannot be empty")


@dataclass
class ProviderSubmission:
    provider_job_id: str
    raw: dict[str, Any] = field(default_factory=dict)
    # A provider whose generation call is synchronous already holds the terminal
    # result when it returns. Carrying it here lets the Gateway finish through
    # its ordinary completion path instead of polling a job that never existed.
    result: ProviderJob | None = None


@dataclass
class ProviderJob:
    provider_job_id: str
    status: str
    progress: float = 0
    output_url: str | None = None
    output_mime_type: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    # Set instead of ``output_url`` when the provider returned the bytes inline.
    # ``outputs[0]`` is the job's output asset; any further entries are extra
    # images from a batch request and are registered as siblings, never
    # discarded, because the workspace already paid for them.
    outputs: list[ProviderInlineOutput] = field(default_factory=list)

    @property
    def has_output(self) -> bool:
        return bool(self.output_url) or bool(self.outputs)


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
    reference_mode: ProviderReferenceMode = ProviderReferenceMode.PROVIDER_MEDIA_ID
    # Declared per provider. The default declares no bounds, which means the
    # original is sent unchanged — the honest reading of "we have not
    # established this provider's limits", not a claim that it has none.
    reference_constraints: ProviderReferenceConstraints = ProviderReferenceConstraints()

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
