"""Public production-project traces, kept outside the production posterior.

Some vendors publish the project behind a finished film: generation jobs,
prompts, named reference assets, outputs and folders.  That is unusually rich
evidence for understanding a production workflow, but it is still *external*
evidence.  It must not be inserted into ``router_observations`` as if the
attempts happened on this platform.

This module gives Router Evidence and Production Evidence one shared, typed
source without weakening that boundary.  The registry is a small committed
manifest; the client materialises a bounded live snapshot under ``data/`` when
an operator explicitly runs the companion script.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict, deque
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

_PACKAGED_PUBLIC_PROJECT_SOURCES_PATH = Path(__file__).with_name("data") / "sources-v1.json"
_CHECKOUT_PUBLIC_PROJECT_SOURCES_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "production-evidence" / "sources-v1.json"
)
DEFAULT_PUBLIC_PROJECT_SOURCES_PATH = (
    _PACKAGED_PUBLIC_PROJECT_SOURCES_PATH
    if _PACKAGED_PUBLIC_PROJECT_SOURCES_PATH.is_file()
    else _CHECKOUT_PUBLIC_PROJECT_SOURCES_PATH
)

type InferredTaskType = Literal["T2V", "I2V", "R2V", "V2V", "T2I", "I2I", "R2I"]
type PublicOutcomeClass = Literal[
    "PROVIDER_FAILURE",
    "CREATIVE_REWORK_CANDIDATE",
    "COMPLETED_CANDIDATE",
    "UNKNOWN",
]


class PublicProjectObservedStats(BaseModel):
    """Numbers displayed by the source, never recomputed into a success rate."""

    model_config = ConfigDict(frozen=True)

    observed_at: str = Field(min_length=1, max_length=40)
    generations_count: int = Field(ge=0)
    root_item_count: int = Field(ge=0)
    top_level_folder_count: int = Field(ge=0)
    top_level_folder_item_sum: int = Field(ge=0)
    assets_bucket_count: int = Field(ge=0)
    regeneration_bucket_count: int = Field(ge=0)
    final_duration_seconds: float = Field(gt=0)
    final_video_asset_id: str = Field(min_length=1, max_length=120)
    scene_folder_counts: dict[str, int] = Field(default_factory=dict)


class PublicProjectSource(BaseModel):
    """A deliberately registered public-project source.

    ``posterior_eligible`` is a literal false rather than a configurable flag.
    Making it true would require a different type and a reviewed calibration
    path, not a manifest edit.
    """

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9]+(?:[-.][a-z0-9]+)*$")
    platform: Literal["higgsfield_public_project"]
    source_class: Literal["vendor_public_project_trace"] = "vendor_public_project_trace"
    page_url: str = Field(min_length=1, max_length=2048)
    api_base_url: str = Field(min_length=1, max_length=2048)
    series_id: str = Field(min_length=1, max_length=120)
    snapshot_folder_id: str = Field(min_length=1, max_length=120)
    publication_id: str = Field(min_length=1, max_length=120)
    prompts_publicly_exposed: bool
    posterior_eligible: Literal[False] = False
    intended_uses: list[Literal["router_reporting", "production_evidence"]] = Field(min_length=1)
    default_folder_paths: list[str] = Field(default_factory=list)
    observed_stats: PublicProjectObservedStats
    router_exclusion_reasons: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _https_and_distinct_uses(self) -> PublicProjectSource:
        for name, value in (("page_url", self.page_url), ("api_base_url", self.api_base_url)):
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError(f"{name} must be an absolute HTTPS URL")
        if len(self.intended_uses) != len(set(self.intended_uses)):
            raise ValueError("intended_uses contains duplicates")
        return self

    def router_view(self) -> dict[str, Any]:
        """Small report shape safe to place beside Router Evidence coverage."""

        return {
            "source_id": self.source_id,
            "source_class": self.source_class,
            "page_url": self.page_url,
            "posterior_eligible": False,
            "exclusion_reasons": list(self.router_exclusion_reasons),
            "observed_stats": self.observed_stats.model_dump(mode="json"),
        }

    def production_view(self) -> dict[str, Any]:
        """Source catalogue shape for the Production Evidence surface."""

        return {
            "source_id": self.source_id,
            "platform": self.platform,
            "source_class": self.source_class,
            "page_url": self.page_url,
            "intended_uses": list(self.intended_uses),
            "posterior_eligible": False,
            "lineage_support": {
                "prompt_to_generation": "EXACT_WHEN_PRESENT",
                "generation_to_output_asset": "EXACT_WHEN_OUTPUT_IS_PUBLIC",
                "reference_asset_to_generation": "EXACT_WHEN_PRESENT",
                "shot_folder_to_generation": "EXACT_FOLDER_MEMBERSHIP",
                "output_asset_to_final_edit": "UNOBSERVED",
                "previous_provider_failures": "ONLY_EXPLICIT_FAILED_STATUSES",
                "creative_rework": "REGENERATION_FOLDER_MEMBERSHIP_ONLY",
            },
            "limitations": list(self.limitations),
        }


class PublicProjectSourceRegistry(BaseModel):
    model_config = ConfigDict(frozen=True)

    registry_version: str = Field(min_length=1, max_length=80)
    frozen_at: str = Field(min_length=1, max_length=40)
    sources: list[PublicProjectSource] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_sources(self) -> PublicProjectSourceRegistry:
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("duplicate public-project source_id")
        return self


class PublicProjectSourceStore:
    """Read-only access to the committed public-project source registry."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else DEFAULT_PUBLIC_PROJECT_SOURCES_PATH
        self._registry: PublicProjectSourceRegistry | None = None

    def registry(self) -> PublicProjectSourceRegistry:
        if self._registry is None:
            self._registry = PublicProjectSourceRegistry.model_validate(
                json.loads(self.path.read_text("utf-8"))
            )
        return self._registry

    def sources(self) -> tuple[PublicProjectSource, ...]:
        return tuple(self.registry().sources)

    def source(self, source_id: str) -> PublicProjectSource:
        for source in self.sources():
            if source.source_id == source_id:
                return source
        raise KeyError(f"unknown public-project source {source_id!r}")


class PublicFolder(BaseModel):
    model_config = ConfigDict(frozen=True)

    folder_id: str
    name: str
    path: str
    parent_id: str | None = None
    item_count: int = 0
    subfolders_count: int = 0


class ReferenceMedia(BaseModel):
    model_config = ConfigDict(frozen=True)

    media_id: str
    url: str
    media_type: str
    width: int | None = None
    height: int | None = None


class ReferenceAsset(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset_id: str
    name: str
    category: str | None = None
    description: str | None = None
    media: list[ReferenceMedia] = Field(default_factory=list)


class OutputAsset(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset_id: str
    asset_id_source: Literal["SOURCE_FIELD", "URL_BASENAME"]
    url: str
    asset_type: str
    variant: str


class GenerationTrace(BaseModel):
    """One public generation job with only observed facts plus labelled inference."""

    model_config = ConfigDict(frozen=True)

    generation_id: str
    job_set_id: str | None = None
    job_set_parent_id: str | None = None
    source_model_id: str
    source_model_revision: None = None
    exact_model_revision_observed: Literal[False] = False
    status: str
    created_at: datetime
    folder_id: str
    folder_path: str
    prompt: str | None = None
    prompt_sha256: str | None = None
    task_type: InferredTaskType
    task_type_inferred: Literal[True] = True
    duration_seconds: float | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    generate_audio: bool | None = None
    reference_assets: list[ReferenceAsset] = Field(default_factory=list)
    output_assets: list[OutputAsset] = Field(default_factory=list)
    outcome_class: PublicOutcomeClass
    final_selection: Literal["UNOBSERVED"] = "UNOBSERVED"


class LineageCandidate(BaseModel):
    """The requested chain, with absence represented explicitly."""

    model_config = ConfigDict(frozen=True)

    folder_path: str
    final_shot_link: Literal["UNOBSERVED"] = "UNOBSERVED"
    output_asset_ids: list[str]
    generation_id: str
    prompt_sha256: str | None
    reference_asset_ids: list[str]
    previous_candidate_ids: list[str]
    previous_explicit_failure_ids: list[str]
    previous_creative_rework_ids: list[str]


class PublicProjectSnapshot(BaseModel):
    """A bounded materialisation of a public project."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    fetched_at: datetime
    project: dict[str, Any]
    root_folder: dict[str, Any]
    folders: list[PublicFolder]
    final_film_assets: list[OutputAsset]
    generations: list[GenerationTrace]
    skipped_non_job_items: int = 0
    truncated: bool = False

    def lineages(self) -> list[LineageCandidate]:
        by_folder: dict[str, list[GenerationTrace]] = defaultdict(list)
        for generation in self.generations:
            by_folder[generation.folder_path].append(generation)

        lineages: list[LineageCandidate] = []
        for folder_path, candidates in sorted(by_folder.items()):
            candidates.sort(key=lambda item: (item.created_at, item.generation_id))
            for index, generation in enumerate(candidates):
                previous = candidates[:index]
                lineages.append(
                    LineageCandidate(
                        folder_path=folder_path,
                        output_asset_ids=[item.asset_id for item in generation.output_assets],
                        generation_id=generation.generation_id,
                        prompt_sha256=generation.prompt_sha256,
                        reference_asset_ids=[item.asset_id for item in generation.reference_assets],
                        previous_candidate_ids=[item.generation_id for item in previous],
                        previous_explicit_failure_ids=[
                            item.generation_id
                            for item in previous
                            if item.outcome_class == "PROVIDER_FAILURE"
                        ],
                        previous_creative_rework_ids=[
                            item.generation_id
                            for item in previous
                            if item.outcome_class == "CREATIVE_REWORK_CANDIDATE"
                        ],
                    )
                )
        return lineages

    def router_evidence_view(self, source: PublicProjectSource) -> dict[str, Any]:
        """Reporting-only aggregate; intentionally has no posterior conversion."""

        return {
            **source.router_view(),
            "sample": {
                "generation_jobs": len(self.generations),
                "model_family_counts": dict(
                    sorted(Counter(item.source_model_id for item in self.generations).items())
                ),
                "task_type_counts": dict(
                    sorted(Counter(item.task_type for item in self.generations).items())
                ),
                "outcome_class_counts": dict(
                    sorted(Counter(item.outcome_class for item in self.generations).items())
                ),
                "prompts_present": sum(item.prompt_sha256 is not None for item in self.generations),
                "prompt_texts_materialised": sum(item.prompt is not None for item in self.generations),
                "outputs_public": sum(bool(item.output_assets) for item in self.generations),
                "truncated": self.truncated,
            },
        }

    def production_evidence_view(self, source: PublicProjectSource) -> dict[str, Any]:
        return {
            **source.production_view(),
            "fetched_at": self.fetched_at.isoformat(),
            "final_film_asset_ids": [asset.asset_id for asset in self.final_film_assets],
            "lineage_candidates": [item.model_dump(mode="json") for item in self.lineages()],
            "warning": (
                "Every final_shot_link is UNOBSERVED: the public project exposes the final film "
                "and generation folders, but not the edit decision joining them."
            ),
        }


class HiggsfieldPublicProjectClient:
    """Bounded reader for a deliberately registered Higgsfield project."""

    def __init__(
        self,
        source: PublicProjectSource,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
    ):
        self.source = source
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        self._api_origin = urlparse(source.api_base_url)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> HiggsfieldPublicProjectClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _get(self, path: str, *, params: Mapping[str, str | int | float | bool] | None = None) -> Any:
        url = urljoin(self.source.api_base_url.rstrip("/") + "/", path.lstrip("/"))
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != self._api_origin.netloc:
            raise ValueError("public-project request escaped the registered API origin")
        response = self.client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def project_metadata(self) -> dict[str, Any]:
        payload = self._get(f"fnf-series/series/{self.source.series_id}")
        if not isinstance(payload, dict):
            raise ValueError("series response is not an object")
        return payload

    def root_folder(self) -> dict[str, Any]:
        payload = self._get(f"fnf/folders/{self.source.snapshot_folder_id}")
        if not isinstance(payload, dict):
            raise ValueError("root-folder response is not an object")
        return payload

    def folder_catalog(self) -> list[PublicFolder]:
        folders: list[PublicFolder] = []
        queue: deque[tuple[str, str]] = deque([(self.source.snapshot_folder_id, "")])
        seen = {self.source.snapshot_folder_id}
        while queue:
            parent_id, parent_path = queue.popleft()
            payload = self._get(
                f"fnf/folders/{parent_id}/children",
                params={"size": 100, "sort_by": "name"},
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise ValueError("folder-children response has no item list")
            for raw in payload["items"]:
                if not isinstance(raw, dict):
                    continue
                folder_id = str(raw.get("id", ""))
                name = str(raw.get("name", ""))
                if not folder_id or not name:
                    continue
                path = f"{parent_path}/{name}".strip("/")
                folder = PublicFolder(
                    folder_id=folder_id,
                    name=name,
                    path=path,
                    parent_id=str(raw.get("parent_id")) if raw.get("parent_id") else parent_id,
                    item_count=int(raw.get("count") or 0),
                    subfolders_count=int(raw.get("subfolders_count") or 0),
                )
                folders.append(folder)
                if folder_id not in seen and folder.subfolders_count:
                    seen.add(folder_id)
                    queue.append((folder_id, path))
        return folders

    def iter_folder_items(
        self,
        folder_id: str,
        *,
        include_subfolders: bool = False,
        max_items: int = 100,
        page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        if max_items < 1:
            return
        cursor: str | float | int | None = None
        seen_cursors: set[str] = set()
        seen_items: set[str] = set()
        yielded = 0
        while yielded < max_items:
            params: dict[str, str | int | float | bool] = {
                "include_subfolders": str(include_subfolders).lower(),
                "size": min(page_size, max_items - yielded),
            }
            if cursor is not None:
                params["cursor"] = cursor
            payload = self._get(f"fnf/folders/{folder_id}/items/v2", params=params)
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise ValueError("folder-items response has no item list")
            page_new = 0
            for raw in payload["items"]:
                if not isinstance(raw, dict):
                    continue
                identity = _item_identity(raw)
                if identity in seen_items:
                    continue
                seen_items.add(identity)
                yielded += 1
                page_new += 1
                yield raw
                if yielded >= max_items:
                    return
            next_cursor = payload.get("cursor")
            if next_cursor is None or not payload["items"]:
                return
            cursor_token = str(next_cursor)
            if cursor_token in seen_cursors or page_new == 0:
                raise ValueError("folder-items pagination repeated without progress")
            seen_cursors.add(cursor_token)
            cursor = next_cursor

    def snapshot(
        self,
        *,
        folder_paths: list[str] | None = None,
        max_items_per_folder: int = 100,
        include_prompts: bool = True,
    ) -> PublicProjectSnapshot:
        project = self.project_metadata()
        root = self.root_folder()
        publication = _project_publication(project)
        if include_prompts and (
            not self.source.prompts_publicly_exposed or not publication.get("show_prompts", False)
        ):
            raise ValueError("source publication does not declare prompts public")

        catalog = self.folder_catalog()
        selected_paths = folder_paths or self.source.default_folder_paths
        selected = _select_folders(catalog, selected_paths)
        generations: list[GenerationTrace] = []
        skipped = 0
        for folder in selected:
            for raw in self.iter_folder_items(
                folder.folder_id,
                # The source contract calls folder membership exact. Asking
                # the API for descendants and then labelling every returned
                # job with this selected folder would merge separate shots
                # into one false lineage.
                include_subfolders=False,
                max_items=max_items_per_folder,
            ):
                if raw.get("type") != "job" or not isinstance(raw.get("job"), dict):
                    skipped += 1
                    continue
                generations.append(
                    _normalise_job(
                        raw["job"],
                        folder=folder,
                        include_prompt=include_prompts,
                    )
                )

        expected = sum(min(folder.item_count, max_items_per_folder) for folder in selected)
        return PublicProjectSnapshot(
            source_id=self.source.source_id,
            fetched_at=datetime.now(UTC),
            project=_project_summary(project),
            root_folder=_root_summary(root),
            folders=catalog,
            final_film_assets=_final_film_assets(publication),
            generations=sorted(generations, key=lambda item: (item.created_at, item.generation_id)),
            skipped_non_job_items=skipped,
            truncated=len(generations) + skipped < expected
            or any(folder.item_count > max_items_per_folder for folder in selected),
        )


def _item_identity(raw: dict[str, Any]) -> str:
    if isinstance(raw.get("job"), dict) and raw["job"].get("id"):
        return f"job:{raw['job']['id']}"
    if raw.get("id"):
        return f"{raw.get('type', 'item')}:{raw['id']}"
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
    return f"anonymous:{digest}"


def _select_folders(catalog: list[PublicFolder], requested: list[str]) -> list[PublicFolder]:
    if not requested:
        raise ValueError("select at least one folder path; whole-project crawling is not implicit")
    by_path = {folder.path.casefold(): folder for folder in catalog}
    selected: list[PublicFolder] = []
    missing: list[str] = []
    for path in requested:
        folder = by_path.get(path.strip("/").casefold())
        if folder is None:
            missing.append(path)
        elif folder not in selected:
            selected.append(folder)
    if missing:
        raise KeyError(f"public-project folder path(s) not found: {', '.join(missing)}")
    return selected


def _project_publication(project: dict[str, Any]) -> dict[str, Any]:
    episodes = project.get("episodes")
    if not isinstance(episodes, list) or not episodes or not isinstance(episodes[0], dict):
        return {}
    publication = episodes[0].get("project_publication")
    return publication if isinstance(publication, dict) else {}


def _project_summary(project: dict[str, Any]) -> dict[str, Any]:
    episodes = project.get("episodes") if isinstance(project.get("episodes"), list) else []
    first_episode = episodes[0] if episodes and isinstance(episodes[0], dict) else {}
    publication = _project_publication(project)
    return {
        "id": project.get("id"),
        "slug": project.get("slug"),
        "name": project.get("name"),
        "episode_id": first_episode.get("id"),
        "duration_seconds": first_episode.get("duration_seconds"),
        "publication_id": publication.get("publication_id"),
        "generations_count": (publication.get("stats") or {}).get("generations_count")
        if isinstance(publication.get("stats"), dict)
        else None,
        "show_prompts": publication.get("show_prompts"),
    }


def _root_summary(root: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": root.get("id"),
        "name": root.get("name"),
        "count": root.get("count"),
        "subfolders_count": root.get("subfolders_count"),
        "is_snapshot": root.get("is_snapshot"),
        "publication_state": (root.get("publication") or {}).get("state")
        if isinstance(root.get("publication"), dict)
        else None,
    }


def _final_film_assets(publication: dict[str, Any]) -> list[OutputAsset]:
    assets: list[OutputAsset] = []
    gallery = publication.get("gallery_media")
    if not isinstance(gallery, list):
        return assets
    for raw in gallery:
        if not isinstance(raw, dict) or not raw.get("url"):
            continue
        assets.append(
            OutputAsset(
                asset_id=str(raw.get("id") or _asset_id_from_url(str(raw["url"]))),
                asset_id_source="SOURCE_FIELD" if raw.get("id") else "URL_BASENAME",
                url=str(raw["url"]),
                asset_type=str(raw.get("type") or "unknown"),
                variant="final_film",
            )
        )
    return assets


def _normalise_job(
    job: dict[str, Any],
    *,
    folder: PublicFolder,
    include_prompt: bool,
) -> GenerationTrace:
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    prompt_value = params.get("prompt")
    prompt = str(prompt_value) if prompt_value not in (None, "") else None
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest() if prompt is not None else None
    status = str(job.get("status") or "unknown")
    outcome = _outcome_class(status, folder.path)
    created = job.get("created_at")
    try:
        created_at = datetime.fromtimestamp(float(created), UTC)
    except (TypeError, ValueError, OSError):
        created_at = datetime.fromtimestamp(0, UTC)
    return GenerationTrace(
        generation_id=str(job.get("id") or ""),
        job_set_id=str(job["job_set_id"]) if job.get("job_set_id") else None,
        job_set_parent_id=str(job["job_set_parent_id"]) if job.get("job_set_parent_id") else None,
        source_model_id=str(job.get("job_set_type") or params.get("model") or "unknown"),
        status=status,
        created_at=created_at,
        folder_id=folder.folder_id,
        folder_path=folder.path,
        prompt=prompt if include_prompt else None,
        prompt_sha256=prompt_hash,
        task_type=_infer_task_type(job, params),
        duration_seconds=float(params["duration"]) if params.get("duration") is not None else None,
        resolution=str(params["resolution"]) if params.get("resolution") else None,
        aspect_ratio=str(params["aspect_ratio"]) if params.get("aspect_ratio") else None,
        generate_audio=bool(params["generate_audio"]) if params.get("generate_audio") is not None else None,
        reference_assets=_reference_assets(params),
        output_assets=_output_assets(job),
        outcome_class=outcome,
        final_selection="UNOBSERVED",
    )


def _infer_task_type(job: dict[str, Any], params: dict[str, Any]) -> InferredTaskType:
    model = str(job.get("job_set_type") or params.get("model") or "").lower()
    is_video = params.get("duration") is not None or any(
        token in model for token in ("video", "seedance", "veo", "kling", "wan")
    )
    references = params.get("reference_elements")
    medias = params.get("medias")
    has_references = isinstance(references, list) and bool(references)
    has_medias = isinstance(medias, list) and bool(medias)
    if is_video:
        if has_references:
            return "R2V"
        if has_medias:
            media_types = {str(item.get("type", "")) for item in medias if isinstance(item, dict)}
            return "V2V" if any("video" in item for item in media_types) else "I2V"
        return "T2V"
    if has_references:
        return "R2I"
    if has_medias:
        return "I2I"
    return "T2I"


def _outcome_class(status: str, folder_path: str) -> PublicOutcomeClass:
    if status.lower() in {"failed", "error", "cancelled", "canceled", "rejected"}:
        return "PROVIDER_FAILURE"
    if "regeneration" in folder_path.casefold():
        return "CREATIVE_REWORK_CANDIDATE"
    if status.lower() == "completed":
        return "COMPLETED_CANDIDATE"
    return "UNKNOWN"


def _reference_assets(params: dict[str, Any]) -> list[ReferenceAsset]:
    raw_elements = params.get("reference_elements")
    if not isinstance(raw_elements, list):
        return []
    assets: list[ReferenceAsset] = []
    for raw in raw_elements:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        media: list[ReferenceMedia] = []
        raw_media = raw.get("medias")
        if isinstance(raw_media, list):
            for item in raw_media:
                if not isinstance(item, dict) or not item.get("url"):
                    continue
                media.append(
                    ReferenceMedia(
                        media_id=str(item.get("id") or _asset_id_from_url(str(item["url"]))),
                        url=str(item["url"]),
                        media_type=str(item.get("type") or "unknown"),
                        width=int(item["width"]) if item.get("width") is not None else None,
                        height=int(item["height"]) if item.get("height") is not None else None,
                    )
                )
        assets.append(
            ReferenceAsset(
                asset_id=str(raw["id"]),
                name=str(raw.get("name") or raw["id"]),
                category=str(raw["category"]) if raw.get("category") is not None else None,
                description=str(raw["description"]) if raw.get("description") is not None else None,
                media=media,
            )
        )
    return assets


def _output_assets(job: dict[str, Any]) -> list[OutputAsset]:
    candidates: list[tuple[str, dict[str, Any]]] = []
    result = job.get("result")
    if isinstance(result, dict):
        candidates.append(("result", result))
    results = job.get("results")
    if isinstance(results, dict):
        for variant, raw in results.items():
            if isinstance(raw, dict):
                candidates.append((str(variant), raw))
    assets: list[OutputAsset] = []
    seen_urls: set[str] = set()
    for variant, raw in candidates:
        url = raw.get("url")
        if not url or str(url) in seen_urls:
            continue
        seen_urls.add(str(url))
        assets.append(
            OutputAsset(
                asset_id=str(raw.get("id") or _asset_id_from_url(str(url))),
                asset_id_source="SOURCE_FIELD" if raw.get("id") else "URL_BASENAME",
                url=str(url),
                asset_type=str(raw.get("type") or "unknown"),
                variant=variant,
            )
        )
    return assets


def _asset_id_from_url(url: str) -> str:
    filename = urlparse(url).path.rsplit("/", 1)[-1]
    stem = filename.split(".", 1)[0]
    return stem or hashlib.sha256(url.encode()).hexdigest()[:32]


__all__ = [
    "DEFAULT_PUBLIC_PROJECT_SOURCES_PATH",
    "GenerationTrace",
    "HiggsfieldPublicProjectClient",
    "LineageCandidate",
    "OutputAsset",
    "PublicFolder",
    "PublicProjectObservedStats",
    "PublicProjectSnapshot",
    "PublicProjectSource",
    "PublicProjectSourceRegistry",
    "PublicProjectSourceStore",
    "ReferenceAsset",
    "ReferenceMedia",
]
