"""Print the Skill integration matrix: what is registered, bound, injected, validated, tracked.

    uv run python scripts/skill_runtime_matrix.py            # markdown table
    uv run python scripts/skill_runtime_matrix.py --json     # the rows as JSON

A Skill counts as integrated only when Runtime Bound and Body Injected are
both yes. The matrix is computed from the installed registry's metadata and
the runtime's call-site table; the test suite (tests/test_skill_runtime.py)
is what proves each bound Skill's body actually reaches a model.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for package in ("core/skills", "packages/contracts"):
    sys.path.insert(0, str(REPOSITORY_ROOT / package))

from skill_core import SkillRegistry, SkillRuntime  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--json", action="store_true", help="print the rows as JSON")
    parser.add_argument("--skills", type=Path, default=REPOSITORY_ROOT / "skills")
    arguments = parser.parse_args()
    runtime = SkillRuntime(SkillRegistry(arguments.skills))
    problems = runtime.validate()
    rows = runtime.integration_matrix()
    if arguments.json:
        print(json.dumps({"problems": problems, "matrix": rows}, indent=2, ensure_ascii=False))
        return 1 if problems else 0
    yes_no = lambda value: "yes" if value else "no"  # noqa: E731
    print(
        "| Skill | Registered | Runtime Bound | Body Injected | Structured Validated | "
        "Fallback Tracked | Integrated |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for row in rows:
        validated = ", ".join(row["structured_validated"]) if row["structured_validated"] else "no"
        note = f" (folded into {row['bound_to']})" if row["bound_to"] else ""
        print(
            f"| {row['skill']}{note} | {yes_no(row['registered'])} | {yes_no(row['runtime_bound'])} | "
            f"{yes_no(row['body_injected'])} | {validated} | {yes_no(row['fallback_tracked'])} | "
            f"{yes_no(row['integrated'])} |"
        )
    if problems:
        print("\nBinding problems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
