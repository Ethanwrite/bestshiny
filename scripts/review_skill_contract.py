"""Review a submitted Skill against the installed contract without installing it.

This is a reviewer aid for the user-authored Skill workflow described in
``docs/DEVELOPMENT_HANDOFF_2026-08-22.md``. It reads a candidate ``SKILL.md``,
reports structural and contract findings, and exits non-zero on a blocking
finding. It never writes, installs, or edits anything: the human review decision
and the Skill text itself remain the user's.

    uv run python scripts/review_skill_contract.py                       # installed prompt-compiler
    uv run python scripts/review_skill_contract.py ~/Downloads/SKILL.md  # a submitted candidate
    uv run python scripts/review_skill_contract.py skills/director/SKILL.md
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for package in ("core/skills", "packages/contracts"):
    sys.path.insert(0, str(REPOSITORY_ROOT / package))

from skill_core import SkillRegistry, SkillRegistryError  # noqa: E402

BLOCKING = "FAIL"
ADVISORY = "WARN"

# The eight fields of PromptCompilerOutput, and the input envelope of
# PromptCompilerInput. Both are defined in packages/contracts/platform_contracts/prompt.py.
PROMPT_COMPILER_OUTPUT_FIELDS = (
    "status",
    "positive_prompt",
    "negative_prompt",
    "asset_bindings",
    "continuity_assertions",
    "qc_checklist",
    "missing_fields",
    "review_reason",
)
PROMPT_COMPILER_STATUSES = ("COMPILED", "NOT_COMPILABLE")
PROMPT_COMPILER_INPUT_FIELDS = ("shot_spec", "asset_bindings", "continuity_context")
CANONICAL_SHOT_FIELDS = ("subjects", "dominant_action", "camera")
RETIRED_STATUS = "REQUIRES_DIRECTOR_REVIEW"
# The pre-envelope draft shape: a flat spec keyed by singular subject/action/shot.
FLAT_SPEC_MARKERS = (r"`subject`", r"`action`", r"`shot`")


@dataclass(frozen=True)
class Finding:
    severity: str
    check: str
    detail: str


def _output_section(body: str) -> str:
    """Return the text of the output-contract section, or the whole body."""

    headings = list(re.finditer(r"^#{1,6}\s+(.+)$", body, re.M))
    for index, heading in enumerate(headings):
        title = heading.group(1).lower()
        if any(token in title for token in ("output", "return", "输出", "返回")):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
            return body[heading.end() : end]
    return body


def _structural_findings(path: Path) -> tuple[list[Finding], object | None]:
    """Parse with the one authoritative registry so there is no second parser."""

    raw = path.read_text("utf-8")
    declared = re.search(r"^name:\s*(.+)$", raw, re.M)
    if not declared:
        return [Finding(BLOCKING, "frontmatter", "no `name:` field in the frontmatter")], None
    name = declared.group(1).strip()
    with tempfile.TemporaryDirectory() as staging:
        staged = Path(staging) / name
        staged.mkdir(parents=True)
        shutil.copyfile(path, staged / "SKILL.md")
        try:
            skill = SkillRegistry(Path(staging)).resolve(name)
        except (SkillRegistryError, LookupError) as exc:
            return [Finding(BLOCKING, "registry", str(exc))], None
    findings: list[Finding] = []
    install_path = REPOSITORY_ROOT / "skills" / name / "SKILL.md"
    if path.resolve() != install_path and path.parent.name != name:
        findings.append(
            Finding(
                ADVISORY,
                "install-path",
                f"candidate is outside its install path; it belongs at skills/{name}/SKILL.md",
            )
        )
    return findings, skill


def _prompt_compiler_findings(body: str) -> list[Finding]:
    findings: list[Finding] = []
    section = _output_section(body)

    if RETIRED_STATUS in body:
        findings.append(
            Finding(
                BLOCKING,
                "retired-status",
                f"{RETIRED_STATUS} was replaced by NOT_COMPILABLE in the final contract",
            )
        )
    missing_output = [field for field in PROMPT_COMPILER_OUTPUT_FIELDS if field not in body]
    if missing_output:
        findings.append(
            Finding(
                BLOCKING,
                "output-fields",
                "PromptCompilerOutput fields never named: " + ", ".join(missing_output),
            )
        )
    missing_status = [status for status in PROMPT_COMPILER_STATUSES if status not in body]
    if missing_status:
        findings.append(
            Finding(BLOCKING, "output-status", "status values never named: " + ", ".join(missing_status))
        )
    missing_input = [field for field in PROMPT_COMPILER_INPUT_FIELDS if field not in body]
    if missing_input:
        findings.append(
            Finding(
                BLOCKING,
                "input-envelope",
                "PromptCompilerInput envelope fields never named: " + ", ".join(missing_input),
            )
        )
    missing_shot = [field for field in CANONICAL_SHOT_FIELDS if field not in body]
    if missing_shot:
        findings.append(
            Finding(
                BLOCKING,
                "shot-spec-fields",
                "CanonicalShotSpec field names never used: " + ", ".join(missing_shot),
            )
        )
    flat = [marker.strip("`") for marker in FLAT_SPEC_MARKERS if re.search(marker, body)]
    if flat:
        findings.append(
            Finding(
                BLOCKING,
                "flat-spec",
                "uses the retired flat spec keys instead of the envelope: " + ", ".join(flat),
            )
        )
    for field in ("provider", "model"):
        if re.search(rf"`{field}`", section):
            findings.append(
                Finding(
                    ADVISORY,
                    "provider-boundary",
                    f"`{field}` appears in the output section; Model Router owns that choice and it "
                    "is not one of the eight output fields",
                )
            )
    return findings


def review(path: Path) -> list[Finding]:
    findings, skill = _structural_findings(path)
    if skill is None:
        return findings
    name = getattr(skill, "name", "")
    body = getattr(skill, "system_prompt", "")
    print(f"  name        {name}")
    print(f"  version     {getattr(skill, 'version', '')}")
    print(f"  sha256      {getattr(skill, 'content_hash', '')}")
    if name == "prompt-compiler":
        findings.extend(_prompt_compiler_findings(body))
    else:
        print(
            "  note        no output contract is defined for this Skill yet; "
            "structural review only"
        )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[REPOSITORY_ROOT / "skills" / "prompt-compiler" / "SKILL.md"],
        help="SKILL.md files to review (default: the installed prompt-compiler Skill)",
    )
    arguments = parser.parse_args()
    blocking = 0
    for path in arguments.paths:
        print(f"\n{path}")
        if not path.is_file():
            print(f"  {BLOCKING:<5} missing-file   {path} does not exist")
            blocking += 1
            continue
        findings = review(path)
        if not findings:
            print("  PASS        no findings; the review decision remains the reviewer's")
            continue
        for finding in findings:
            print(f"  {finding.severity:<5} {finding.check:<18} {finding.detail}")
        blocking += sum(1 for finding in findings if finding.severity == BLOCKING)
    print(
        "\nThis tool reports findings only. It installs nothing, edits nothing, and does not "
        "author Skill text."
    )
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
