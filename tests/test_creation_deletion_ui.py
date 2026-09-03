"""The Productions delete affordances, as a contract the browser must keep.

This repository ships no browser test harness, so the front end is pinned two
ways here, and both read the files that are actually served:

1. *Structure* — the markup and wiring that make the feature reachable: a
   danger action in the Creation detail Inspector, a per-row menu carrying
   Delete, and a confirmation nothing can bypass;
2. *Behaviour* — the delete-flow functions are lifted out of ``app.js`` by name
   and executed in Node against stubs, so what is asserted is the shipped
   source running, not a copy of it. That is what makes "the numbers fall
   immediately" and "the request is project-scoped" testable at all.

The third thing pinned is vocabulary: this surface talks to creators, so the
words the implementation is built from — soft delete, deleted_at, provider job,
orphan cleanup — must never reach it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "apps" / "web"
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")
STYLES_CSS = (WEB / "styles.css").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    """One top-level function from app.js, brace-matched, verbatim."""

    for opener in (f"\nfunction {name}(", f"\nasync function {name}("):
        start = APP_JS.find(opener)
        if start != -1:
            break
    assert start != -1, f"app.js no longer defines {name}()"
    start += 1
    depth = 0
    index = APP_JS.index("{", start)
    for position in range(index, len(APP_JS)):
        character = APP_JS[position]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return APP_JS[start : position + 1]
    raise AssertionError(f"{name}() is not brace-balanced")


# --------------------------------------------------------------------------
# 1. The two doors, and the confirmation in front of both.
# --------------------------------------------------------------------------
def test_the_inspector_offers_a_delete_action() -> None:
    assert 'id="deleteJobBtn"' in INDEX_HTML
    button = INDEX_HTML[INDEX_HTML.index('id="deleteJobBtn"') :][:400]
    assert "btn-danger" in button, "deleting is a danger action, styled as one"
    assert "Delete creation" in button
    # It lives in the Productions inspector, not somewhere a stray click finds.
    inspector = INDEX_HTML[INDEX_HTML.index("<h2>Creation detail</h2>") :]
    assert 'id="deleteJobBtn"' in inspector[: inspector.index("</aside>")]


def test_every_production_row_carries_its_own_menu() -> None:
    render = _function_source("renderProductions")
    assert "data-job-menu=" in render, "each row needs its own actions menu"
    assert 'class="job-row"' in render, "the menu cannot be nested inside the row button"
    assert 'aria-haspopup="menu"' in render and 'aria-expanded="false"' in render
    menu = _function_source("toggleJobMenu")
    assert "data-job-delete=" in menu and ">Delete<" in menu
    assert "openDeleteCreationDialog" in menu, "the menu must go through the confirmation"
    assert ".job-row" in STYLES_CSS and ".job-menu-panel" in STYLES_CSS


def test_nothing_deletes_without_a_confirmation() -> None:
    assert 'id="deleteCreationDialog"' in INDEX_HTML
    opener = _function_source("openDeleteCreationDialog")
    assert "showModal()" in opener
    # The request is only ever reached from the dialog's own confirm handler.
    callers = [
        name
        for name in ("confirmDeleteCreation", "toggleJobMenu", "renderProductions")
        if "deleteCreation(" in _function_source(name)
    ]
    assert callers == ["confirmDeleteCreation"], (
        "deleteCreation() must be reachable only through the confirmation dialog"
    )
    assert 'on("confirmDeleteCreationBtn", "click"' in APP_JS
    assert 'on("cancelDeleteCreationBtn", "click"' in APP_JS


def test_a_creation_still_being_made_says_it_will_be_stopped_first() -> None:
    assert 'id="deleteCreationRunningNote"' in INDEX_HTML
    note = INDEX_HTML[INDEX_HTML.index('id="deleteCreationRunningNote"') :][:300]
    assert "stop it first" in note
    opener = _function_source("openDeleteCreationDialog")
    assert "DIRECTLY_DELETABLE_JOB_STATES" in opener, "the note is state-driven, not always on"


# --------------------------------------------------------------------------
# 2. The shipped functions, actually run.
# --------------------------------------------------------------------------
HARNESS = """
%(functions)s

const calls = [];
const state = {
  project: { id: "project-1" },
  selectedJobId: "job-1",
  jobs: new Map([
    ["job-1", { id: "job-1", project_id: "project-1", status: "COMPLETED", model: "m" }],
    ["job-2", { id: "job-2", project_id: "project-1", status: "COMPLETED", model: "m" }],
  ]),
  operations: { job: { id: "job-1" } },
};
const inputs = { operationsJobId: { value: "job-1" } };
const $ = (id) => inputs[id] || null;
async function request(path, options) { calls.push({ path, method: options.method }); return null; }
function renderProductions() { calls.push({ rendered: true }); }
function renderGenerationControl(job) { calls.push({ inspectorCleared: job === null }); }

await deleteCreation("job-1");
console.log(JSON.stringify({
  calls,
  remaining: [...state.jobs.keys()],
  selected: state.selectedJobId,
  jobIdField: inputs.operationsJobId.value,
}));
"""


def _run_delete_flow(tmp_path: Path) -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - developer machines all have it.
        pytest.skip("node is required to execute the shipped front-end functions")
    script = tmp_path / "delete-flow.mjs"
    script.write_text(
        HARNESS
        % {
            "functions": "\n".join(
                _function_source(name) for name in ("deleteCreation", "forgetJob")
            )
        },
        encoding="utf-8",
    )
    completed = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_the_delete_request_names_the_project_it_belongs_to(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Cross-project deletion is fenced server-side; the client never asks for it."""

    result = _run_delete_flow(tmp_path)
    requests = [call for call in result["calls"] if "path" in call]
    assert len(requests) == 1
    assert requests[0]["method"] == "DELETE"
    assert requests[0]["path"] == "/v1/generations/job-1?project_id=project-1"


def test_the_counts_fall_immediately_without_waiting_for_the_server(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Every number on the page is derived from state.jobs, so it must shrink.

    The four state counts, the session panel and the project total all read the
    same map. Dropping the creation from it and re-rendering is what makes them
    all correct in the same frame.
    """

    result = _run_delete_flow(tmp_path)
    assert result["remaining"] == ["job-2"], "the deleted creation leaves the session's memory"
    assert result["selected"] is None, "a deleted creation cannot stay selected"
    assert result["jobIdField"] == "", "the lookup field cannot keep pointing at it"
    assert any(call.get("rendered") for call in result["calls"]), "the page redraws at once"
    assert any(call.get("inspectorCleared") for call in result["calls"])


# --------------------------------------------------------------------------
# 3. Vocabulary.
# --------------------------------------------------------------------------
#: Words that describe how deletion is built, not what it does for a creator.
FORBIDDEN_IN_UI_TEXT = (
    "soft delete",
    "soft-delete",
    "deleted_at",
    "deleted_by",
    "provider job",
    "orphan",
    "orphan cleanup",
    "media asset",
    "tombstone",
    "generation job",
    "sweep",
)


def _visible_text(html: str) -> str:
    """Everything a creator can read: element text plus the visible attributes."""

    import re

    without_tags = re.sub(r"<[^>]+>", " ", html)
    labels = " ".join(
        re.findall(r'(?:title|aria-label|placeholder)="([^"]*)"', html)
    )
    return f"{without_tags} {labels}".lower()


def test_the_delete_surface_speaks_the_users_language() -> None:
    dialog = INDEX_HTML[
        INDEX_HTML.index('id="deleteCreationDialog"') : INDEX_HTML.index(
            'id="passwordResetDialog"'
        )
    ]
    inspector_group = INDEX_HTML[INDEX_HTML.index('id="deleteJobBtn"') - 400 :][:1200]
    for phrase in FORBIDDEN_IN_UI_TEXT:
        assert phrase not in _visible_text(dialog), f"{phrase!r} is jargon in the confirmation"
        assert phrase not in _visible_text(inspector_group), f"{phrase!r} is jargon in the inspector"
    # And it says the one thing a creator most needs to know.
    assert "not returned" in _visible_text(dialog) or "not returned" in _visible_text(
        inspector_group
    ), "the credit consequence must be stated in plain words"


def test_the_toast_and_menu_labels_stay_plain() -> None:
    for name in ("confirmDeleteCreation", "toggleJobMenu", "openDeleteCreationDialog"):
        source = _function_source(name).lower()
        for phrase in FORBIDDEN_IN_UI_TEXT:
            # Only the quoted strings a user can see, not the code around them.
            import re

            for literal in re.findall(r'"([^"]*)"|`([^`]*)`', source):
                text = (literal[0] or literal[1]).lower()
                if text.startswith("/") or "$" in text or "<" in text:
                    continue
                assert phrase not in text, f"{phrase!r} reaches the user from {name}()"
