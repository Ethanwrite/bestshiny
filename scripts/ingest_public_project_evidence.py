"""Materialise a bounded public production-project trace.

The output is an ignored working artefact under ``data/production-evidence``.
It contains a raw typed snapshot plus two projections of the same facts:
Router reporting (never posterior-eligible) and Production Evidence lineage.

Examples::

    .venv/bin/python scripts/ingest_public_project_evidence.py
    .venv/bin/python scripts/ingest_public_project_evidence.py \
      --folder 'SCENE 2 - LIVINGROOM/shot 1' --max-items-per-folder 20
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core" / "router-evidence"))

from router_evidence_core import (  # noqa: E402
    HiggsfieldPublicProjectClient,
    PublicProjectSourceStore,
)


def _write_json_atomic(
    destination: Path,
    payload: dict[str, Any],
    *,
    overwrite: bool,
) -> None:
    """Publish one complete snapshot without silently replacing an older one."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump(payload, output, indent=2, ensure_ascii=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if overwrite:
            os.replace(temporary, destination)
        else:
            # A same-directory hard link is an atomic no-clobber publish: it
            # fails with FileExistsError if another run already owns the name.
            os.link(temporary, destination)
            temporary.unlink()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _display_path(destination: Path) -> str:
    try:
        return str(destination.relative_to(ROOT))
    except ValueError:
        return str(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="higgsfield-oneiric-2026-08")
    parser.add_argument(
        "--folder",
        action="append",
        help="exact registered project folder path; repeat for multiple folders",
    )
    parser.add_argument("--max-items-per-folder", type=int, default=100)
    parser.add_argument(
        "--without-prompts",
        action="store_true",
        help="retain prompt hashes but omit prompt text from the materialised snapshot",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--force",
        action="store_true",
        help="atomically replace an existing output; the default refuses to overwrite evidence",
    )
    arguments = parser.parse_args()

    if arguments.max_items_per_folder < 1:
        parser.error("--max-items-per-folder must be at least 1")

    source = PublicProjectSourceStore().source(arguments.source)
    destination = (
        (
            arguments.output
            or (ROOT / "data" / "production-evidence" / "public-projects" / f"{source.source_id}.json")
        )
        .expanduser()
        .resolve()
    )
    if destination.exists() and not arguments.force:
        parser.error(f"output already exists: {destination}; pass --force to replace it atomically")

    with HiggsfieldPublicProjectClient(source) as client:
        snapshot = client.snapshot(
            folder_paths=arguments.folder,
            max_items_per_folder=arguments.max_items_per_folder,
            include_prompts=not arguments.without_prompts,
        )

    router_view = snapshot.router_evidence_view(source)
    production_view = snapshot.production_evidence_view(source)
    payload = {
        "schema_version": "public-project-evidence-v1",
        "source_registry_version": PublicProjectSourceStore().registry().registry_version,
        "snapshot": snapshot.model_dump(mode="json"),
        "router_evidence": router_view,
        "production_evidence": production_view,
    }
    try:
        _write_json_atomic(destination, payload, overwrite=arguments.force)
    except FileExistsError:
        parser.error(f"output already exists: {destination}; pass --force to replace it atomically")

    router = router_view["sample"]
    print(f"source      {source.source_id}")
    print(f"output      {_display_path(destination)}")
    print(f"generations {router['generation_jobs']}")
    print(f"prompts     {router['prompts_present']}")
    print(f"outputs     {router['outputs_public']}")
    print(f"truncated   {router['truncated']}")
    print("posterior   ineligible (external vendor project; no exact revision or final-selection edge)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
