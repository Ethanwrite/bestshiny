"""Ask Grok to search the public record; keep every answer as raw evidence.

The division of labour this script exists to enforce:

* **Grok searches.** It has web access and this process does not. It returns
  candidate records with URLs and quotes.
* **This script transports.** It builds the prompt, constrains the answer to a
  JSON Schema generated from the very Pydantic models the ingest will validate
  against, runs the CLI, and writes the raw response to disk untouched.
* **Nothing here judges.** No score is adjusted, no blank is filled, no version
  is resolved. That happens in ``ingest_router_evidence.py``, which refuses far
  more than it accepts, and the refusals are part of the output.

Raw responses are kept verbatim under ``data/router-evidence/raw/`` even when
they are later rejected wholesale. A rejected record is the evidence that the
research pass was checked; deleting it leaves only the survivors, which looks
identical to a pass that found nothing wrong.

Usage::

    .venv/bin/python scripts/research_router_evidence.py --layer benchmark
    .venv/bin/python scripts/research_router_evidence.py --layer community --model kling-3-pro-openrouter
    .venv/bin/python scripts/research_router_evidence.py --list

Each invocation spends Grok credits. ``--list`` and ``--dry-run`` spend none.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core" / "router-evidence"))

from router_evidence_core.layers import LAYER_SOURCE_TYPES, EvidenceLayer  # noqa: E402
from router_evidence_core.priors import DESCRIPTIVE_SCALES, SCORING_SCALES  # noqa: E402
from router_evidence_core.records import (  # noqa: E402
    BenchmarkRecord,
    CommunityRecord,
    OfficialRecord,
)

GROK = Path.home() / ".grok" / "bin" / "grok"
RAW_ROOT = ROOT / "data" / "router-evidence" / "raw"

_RECORD_MODELS = {
    EvidenceLayer.OFFICIAL: OfficialRecord,
    EvidenceLayer.BENCHMARK: BenchmarkRecord,
    EvidenceLayer.COMMUNITY: CommunityRecord,
}


@dataclass(frozen=True)
class ResearchTarget:
    """One model, named the three ways the search has to be able to spell it.

    ``exact_version`` is what this platform runs and what a record must bind
    to. ``search_names`` are the names the public record uses, which are
    frequently neither the logical name nor the API id.
    """

    logical_name: str
    provider: str
    model_id: str
    exact_version: str
    modality: str
    search_names: tuple[str, ...]


TARGETS: tuple[ResearchTarget, ...] = (
    ResearchTarget(
        "seedance-2.5-official", "seedance", "doubao-seedance-2-5-260628", "seedance-2.5",
        "video", ("Seedance 2.5", "Doubao Seedance", "豆包 Seedance"),
    ),
    ResearchTarget(
        "kling-3-pro-openrouter", "openrouter", "kwaivgi/kling-v3.0-pro", "kling-v3.0-pro",
        "video", ("Kling 3.0 Pro", "Kling v3 Pro", "可灵 3.0"),
    ),
    ResearchTarget(
        "kling-3-standard-openrouter", "openrouter", "kwaivgi/kling-v3.0-std", "kling-v3.0-std",
        "video", ("Kling 3.0 Standard", "Kling v3 Std"),
    ),
    ResearchTarget(
        "veo-3.1-quality-official", "veo_official", "veo-3.1-quality", "veo-3.1-quality",
        "video", ("Veo 3.1", "Veo 3.1 Quality"),
    ),
    ResearchTarget(
        "veo-3.1-fast-openrouter", "openrouter", "google/veo-3.1-fast", "veo-3.1-fast",
        "video", ("Veo 3.1 Fast",),
    ),
    ResearchTarget(
        "veo-3.1-openrouter", "openrouter", "google/veo-3.1", "veo-3.1",
        "video", ("Veo 3.1",),
    ),
    ResearchTarget(
        "wan-2.7-official", "wan", "wan-2.7", "wan-2.7",
        "video", ("Wan 2.7", "Wan2.7", "通义万相 2.7"),
    ),
    ResearchTarget(
        "grok-video-official", "grok", "grok-video", "grok-video",
        "video", ("Grok Imagine video", "Grok Imagine", "xAI Grok video"),
    ),
    ResearchTarget(
        "grok-imagine-video-openrouter", "openrouter", "x-ai/grok-imagine-video", "grok-imagine-video",
        "video", ("Grok Imagine video",),
    ),
    ResearchTarget(
        "gpt-image-2-openrouter", "openrouter", "openai/gpt-image-2", "gpt-image-2",
        "image", ("GPT Image 2", "gpt-image-2"),
    ),
    ResearchTarget(
        "seedream-5.0-ark", "seedance", "doubao-seedream-5-0-260128", "seedream-5.0",
        "image", ("Seedream 5.0", "Doubao Seedream 5"),
    ),
)

SCENARIO_BRIEF = (
    "motion, physics, human consistency, identity, camera motion, prompt adherence, "
    "text rendering, Chinese text, dialogue/lip-sync, reference adherence, first/last frame, "
    "cinematic quality, commercial/product, portrait"
)

_COMMON_RULES = """
HARD RULES. Breaking any of these makes the whole answer worthless, and the
receiving pipeline will reject records that break them.

1. Never invent a number. If a page states a result in words and publishes no
   figure, set `value` to null. A plausible number is indistinguishable from a
   real one at the point of use, and that is the failure this pipeline exists
   to prevent.
2. `provenance.verbatim_quote` must be text copied from the source, containing
   the number you are reporting. Records whose quote does not contain the
   number are rejected automatically.
3. Never infer a sample size, a confidence interval or a generation count. Each
   has a `*_stated_by_source` flag; set the value only when the source states
   it, and set the flag true only then.
4. Never guess which API alias corresponds to which model snapshot. If the
   source does not name a version, use version_match
   "EXACT_VERSION_UNSPECIFIED_REVISION" and mapping_confidence "MEDIUM" at
   most. If it names a different version from the one we run, say
   "VERSION_MISMATCH" — such records are kept deliberately, so report them
   rather than dropping them.
5. `metric_scale_id` must be one of these, exactly as spelled.
   Score scales (something the model can be better or worse at):
   {scoring}
   Descriptive scales (a documented fact, not a score — duration ceilings,
   limits, prices, API enums, and `unscored` for a claim made in words with no
   number at all):
   {descriptive}
   If a source's scale is not in either list, omit the record and say so in
   your closing note. Do not invent a scale id.
6. Keep the source's own scale. Do not convert a percentage to a ratio, a
   Likert to a percentage, or an Elo to anything.
7. Do not mix modalities or operations. A text-to-video result and an
   image-to-video result are different records with different `task_type`.
8. Write your findings as prose, one section per source. Every section must
   give: the URL, the publisher, the publication or reading date, the exact
   model name the source used, a verbatim quote containing any number you
   report, the scale that number is on, and which of our scenes it speaks to.
   A second pass will turn your prose into records; it cannot reach the web,
   so anything you leave out is lost.
"""


def _rules() -> str:
    return _COMMON_RULES.format(
        scoring=", ".join(sorted(SCORING_SCALES)),
        descriptive=", ".join(sorted(DESCRIPTIVE_SCALES)),
    )


def build_prompt(layer: EvidenceLayer, target: ResearchTarget) -> str:
    names = ", ".join(f'"{name}"' for name in target.search_names)
    allowed = ", ".join(sorted(LAYER_SOURCE_TYPES[layer]))
    header = (
        f"Search the public record for evidence about the {target.modality} model we run as "
        f"`{target.logical_name}` (provider `{target.provider}`, API id `{target.model_id}`, "
        f"exact version `{target.exact_version}`). Public names to search for: {names}.\n\n"
        f"You are gathering ONLY {layer.value} evidence. Allowed source types for this pass: "
        f"{allowed}. Evidence of any other class must not be returned in this pass — a Reddit "
        f"thread is not a benchmark and a vendor blog is not independent.\n\n"
        f"Scenes we care about: {SCENARIO_BRIEF}.\n"
    )
    if layer is EvidenceLayer.OFFICIAL:
        body = (
            "Find the vendor's own technical report, model card, API documentation, pricing page "
            "and release notes. Report: stated capabilities and limitations, documented maximum "
            "and minimum durations, supported resolutions and aspect ratios, reference-image "
            "limits, audio/dialogue support, published pricing, and any benchmark numbers the "
            "vendor publishes about itself. Set `claim_kind` honestly — a documented duration "
            "limit is a `capability_limit` and is worth more than a marketing adjective, which "
            "is a `qualitative_claim` with a null value.\n"
        )
    elif layer is EvidenceLayer.BENCHMARK:
        body = (
            "Find academic papers, independent benchmark suites, arena leaderboards and "
            "third-party evaluations with a stated protocol. For each: the benchmark name and "
            "version, who evaluated, whether scoring was human or automatic, the sample size if "
            "and only if it is stated, any confidence interval if and only if it is stated, and "
            "the models it was compared against. A leaderboard reading is dynamic — set "
            "`provenance.dynamic` true and give the date you read it.\n"
        )
    else:
        body = (
            "Find first-hand practitioner reports: Reddit, X, GitHub issues and discussions, "
            "Hugging Face discussions, public Discord and forum posts, and creator comparisons. "
            "For each post record the author handle, the venue, whether the author ran the model "
            "themselves (`firsthand`) or is relaying someone else's result (`paraphrased` / "
            "`secondhand`), their stance, and the specific failure modes named. Set "
            "`is_marketing` true for promotional posts and `is_bot_suspected` true for automated "
            "ones — they are recorded and then excluded, and mislabelling them as genuine reports "
            "inflates the sample. Most community posts carry no number: that is expected, set "
            "`value` to null and let the stance carry the information. Prefer many distinct "
            "authors over many posts by the same author; repeated posts from one author are "
            "discounted downstream.\n"
        )
    search_first = (
        "BEFORE ANSWERING YOU MUST USE YOUR WEB SEARCH AND WEB FETCH TOOLS. An answer produced "
        "without searching is a failed run, whatever it contains: your own recollection of a "
        "model's scores is exactly the fabricated-evidence problem this pipeline is built to "
        "catch. Search several times, with several phrasings, and open the pages you cite so "
        "that the quote you return is text you actually read.\n"
    )
    return header + "\n" + search_first + "\n" + body + "\n" + _rules() + (
        "\nReport between 0 and 12 sources. Zero is a valid and useful answer; a padded list is "
        "not. For each, state explicitly what the source named the model and how confident you "
        f"are that it is our `{target.exact_version}` — and say so plainly when it is a different "
        "version, because near-miss evidence is kept on purpose rather than discarded.\n"
        "Finish with a short paragraph on what you searched for and found nothing on."
    )


def build_schema(layer: EvidenceLayer) -> dict[str, object]:
    """The answer schema, generated from the models the ingest validates with.

    Generating it rather than writing it by hand is what keeps the research
    contract and the validation contract from drifting apart — a field added to
    the record type appears in the next prompt automatically.
    """

    record_schema = _RECORD_MODELS[layer].model_json_schema()
    defs = record_schema.pop("$defs", {})
    # Pin `source_type` to this layer's own set. Prose in the prompt was not
    # enough: a whole community pass came back with the *layer* name in that
    # field, which is a labelling slip rather than bad evidence but invalidates
    # every record it touches. An enum makes it unrepresentable.
    provenance = defs.get("Provenance")
    if isinstance(provenance, dict):
        properties = provenance.get("properties", {})
        if "source_type" in properties:
            properties["source_type"] = {
                "type": "string",
                "enum": sorted(LAYER_SOURCE_TYPES[layer]),
                "description": f"Which kind of {layer.value} source this is.",
            }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["records", "note"],
        "properties": {
            "records": {"type": "array", "items": record_schema, "maxItems": 12},
            "note": {
                "type": "string",
                "description": (
                    "What you searched, what you deliberately excluded and why, and anything "
                    "you found that did not fit the schema."
                ),
            },
        },
        "$defs": defs,
    }


def _invoke(command: list[str], *, timeout: int) -> tuple[bool, str]:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed executable, no shell
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return False, f"grok timed out after {timeout}s"
    if completed.returncode != 0:
        return False, (completed.stderr or completed.stdout)[-2000:]
    return True, completed.stdout


def research(prompt: str, *, timeout: int, max_turns: int) -> tuple[bool, str]:
    """Stage one: search the web and write findings in prose.

    Unconstrained on purpose. Structured-output mode makes the model emit the
    whole JSON object on every assistant turn, which both wastes the budget and
    pushes it towards answering before it has searched — the first probe of
    this script came back with an empty list, one turn and complete confidence.
    Prose first, structure second.
    """

    if not GROK.exists():  # pragma: no cover - operator environment
        return False, f"grok CLI not found at {GROK}"
    return _invoke(
        [
            str(GROK),
            "-p",
            prompt,
            # `--permission-mode dontAsk` cancels the run the first time a tool
            # wants approval, and the transcript reads as a model that simply
            # stopped searching: three narration lines, stopReason "cancelled",
            # and an empty result that looks like "nothing found". This flag is
            # the one that actually lets the search finish.
            "--always-approve",
            "--max-turns",
            str(max_turns),
            "--output-format",
            "json",
        ],
        timeout=timeout,
    )


def structure(
    findings: str, schema: dict[str, object], layer: EvidenceLayer, *, timeout: int
) -> tuple[bool, str]:
    """Stage two: turn the findings into records, with the web switched off.

    ``--disable-web-search`` is the point of the split. This call physically
    cannot reach a source, so it cannot introduce a fact that stage one did not
    find; its only job is to reshape. Anything it invents anyway has no quote
    behind it and is rejected at ingest.
    """

    if not GROK.exists():  # pragma: no cover - operator environment
        return False, f"grok CLI not found at {GROK}"
    prompt = (
        "Convert the research notes below into records matching the schema. Rules:\n"
        "- Use ONLY what the notes contain. Add nothing. If the notes do not give a number, "
        "set `value` to null.\n"
        "- `provenance.verbatim_quote` must be text the notes quote from the source and must "
        "contain the number you report. If the notes have no quote for a claim, drop the claim.\n"
        "- Set every `*_stated_by_source` flag false unless the notes say the source stated it.\n"
        "- Do not resolve a version the notes left ambiguous; use version_match "
        "EXACT_VERSION_UNSPECIFIED_REVISION or VERSION_MISMATCH and say why.\n"
        f"- Every record is {layer.value} evidence; its source_type must match that layer.\n"
        "- Return an empty list rather than a padded one.\n\n"
        "=== RESEARCH NOTES ===\n" + findings
    )
    return _invoke(
        [
            str(GROK),
            "-p",
            prompt,
            "--json-schema",
            json.dumps(schema),
            "--always-approve",
            "--disable-web-search",
            "--max-turns",
            "3",
        ],
        timeout=timeout,
    )


def _extract_text(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(payload, dict):
        return str(payload.get("text") or raw)
    return raw


def raw_path(layer: EvidenceLayer, target: ResearchTarget) -> Path:
    return RAW_ROOT / layer.value / f"{target.logical_name}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layer", choices=[layer.value for layer in _RECORD_MODELS], action="append")
    parser.add_argument("--model", action="append", help="logical name; repeatable")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--dry-run", action="store_true", help="write the prompts, call nothing")
    parser.add_argument("--list", action="store_true", help="print targets and exit")
    parser.add_argument("--overwrite", action="store_true", help="re-research targets already on disk")
    arguments = parser.parse_args()

    if arguments.list:
        for target in TARGETS:
            print(f"{target.logical_name:34s} {target.provider:12s} {target.exact_version}")
        return 0

    if shutil.which("grok") is None and not GROK.exists() and not arguments.dry_run:
        print(f"grok CLI not found at {GROK}", file=sys.stderr)
        return 2

    requested = arguments.layer or [layer.value for layer in _RECORD_MODELS]
    layers = [EvidenceLayer(value) for value in requested]
    targets = [
        target
        for target in TARGETS
        if not arguments.model or target.logical_name in set(arguments.model)
    ]
    if not targets:
        print("no targets matched", file=sys.stderr)
        return 2

    failures = 0
    for layer in layers:
        schema = build_schema(layer)
        for target in targets:
            destination = raw_path(layer, target)
            if destination.exists() and not arguments.overwrite:
                print(f"skip   {layer.value:18s} {target.logical_name} (already on disk)")
                continue
            prompt = build_prompt(layer, target)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if arguments.dry_run:
                (destination.with_suffix(".prompt.txt")).write_text(prompt, "utf-8")
                print(f"prompt {layer.value:18s} {target.logical_name}")
                continue
            print(f"search {layer.value:18s} {target.logical_name} ...", flush=True)
            found, findings_raw = research(
                prompt, timeout=arguments.timeout, max_turns=arguments.max_turns
            )
            findings = _extract_text(findings_raw) if found else ""
            ok, output = (False, findings_raw)
            if found and findings.strip():
                ok, output = structure(findings, schema, layer, timeout=arguments.timeout)
            envelope = {
                "layer": layer.value,
                "logical_name": target.logical_name,
                "provider": target.provider,
                "model_id": target.model_id,
                "exact_version": target.exact_version,
                "requested_at": datetime.now(UTC).isoformat(),
                "ok": ok,
                "findings": findings,
                "findings_raw": findings_raw if not found else "",
                "raw": output,
            }
            destination.write_text(json.dumps(envelope, indent=2, ensure_ascii=False) + "\n", "utf-8")
            if not ok:
                failures += 1
                print(f"  FAILED: {output[:300]}", file=sys.stderr)
            else:
                print(f"  wrote {destination.relative_to(ROOT)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
