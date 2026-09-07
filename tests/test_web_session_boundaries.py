"""What signing out must do, what it must not claim, and the folded inspector.

The 2026-09-06 audit found three defects in the shipped web app that no
server test can see:

1. ``logout()`` swallowed every failure of ``POST /api/auth/logout`` and hid
   the page anyway, so a sign-out the server never saw - the session still
   live, the HttpOnly cookie still set - looked like a sign-out, and the next
   refresh walked back into the account;
2. ``clearWorkspaceState()`` left the director sessions, the session rail,
   the episode strip, a continuation draft, the prompt boxes and their DOM
   behind, and a new account with no projects never overwrote them, so a
   shared browser showed the previous account's work;
3. under 1280px the whole Inspector was ``display:none`` behind a class
   nothing in the app ever set, so the model, ratio, resolution, duration,
   character and recovery controls were simply gone.

The shipped functions are lifted out of ``app.js`` by name and executed in
Node against stubs, so what is asserted is the source that is served.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "apps" / "web"
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")
STYLES_CSS = (WEB / "styles.css").read_text(encoding="utf-8")


def _function_source(name: str, source: str = APP_JS) -> str:
    """One top-level function, brace-matched, verbatim."""

    for opener in (f"\nfunction {name}(", f"\nasync function {name}("):
        start = source.find(opener)
        if start != -1:
            break
    assert start != -1, f"the module no longer defines {name}()"
    start += 1
    depth = 0
    index = source.index("{", start)
    for position in range(index, len(source)):
        character = source[position]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[start : position + 1]
    raise AssertionError(f"{name}() is not brace-balanced")


def _run(tmp_path: Path, script: str) -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - developer machines all have it.
        pytest.skip("node is required to execute the shipped front-end functions")
    path = tmp_path / "harness.mjs"
    path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        [node, str(path)], capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------
# 1. Sign-out is only a sign-out once the server has confirmed it.
# --------------------------------------------------------------------------
LOGOUT_HARNESS = """
%(logout)s

const nodes = {
  logoutError: { hidden: true, textContent: "" },
  userMenu: { hidden: true },
  userMenuBtn: { attrs: {}, setAttribute(name, value) { this.attrs[name] = value; } },
};
const $ = (id) => nodes[id] || null;
const calls = [];
const toasts = [];
function toast(message) { toasts.push(message); }
function lockAuth() { calls.push("lockAuth"); }
function navigate(route) { calls.push(`navigate:${route}`); }
const outcome = %(outcome)s;
async function request(path, options) {
  calls.push(`${options.method} ${path}`);
  if (outcome === "ok") return null;
  const error = new Error(outcome === "401" ? "Unauthorized" : "Failed to fetch");
  if (outcome === "401") error.status = 401;
  if (outcome === "500") { error.status = 500; error.message = "Internal Server Error"; }
  throw error;
}

const returned = await logout();
console.log(JSON.stringify({
  returned, calls, toasts, note: nodes.logoutError, menuOpen: !nodes.userMenu.hidden,
}));
"""


def _logout(tmp_path: Path, outcome: str) -> dict:
    return _run(
        tmp_path, LOGOUT_HARNESS % {"logout": _function_source("logout"), "outcome": json.dumps(outcome)}
    )


def test_a_confirmed_sign_out_locks_the_page(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _logout(tmp_path, "ok")
    assert result["returned"] is True
    assert result["calls"] == ["POST /api/auth/logout", "lockAuth", "navigate:/"]
    assert result["note"]["hidden"] is True


def test_a_session_the_server_no_longer_holds_is_signed_out_locally(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """401 means there is no server session for this cookie: locking the page is exact."""

    result = _logout(tmp_path, "401")
    assert result["returned"] is True
    assert "lockAuth" in result["calls"]


@pytest.mark.parametrize("outcome", ["network", "500"])
def test_an_unconfirmed_sign_out_keeps_the_session_and_says_so(tmp_path, outcome) -> None:  # type: ignore[no-untyped-def]
    result = _logout(tmp_path, outcome)
    assert result["returned"] is False
    assert "lockAuth" not in result["calls"], (
        "the server may still hold the session; hiding the page is not a sign-out"
    )
    assert not any(call.startswith("navigate") for call in result["calls"])
    assert result["note"]["hidden"] is False
    assert "still signed in" in result["note"]["textContent"]
    assert "try again" in result["note"]["textContent"]
    assert result["menuOpen"] is True, "the Sign out button stays in reach for the retry"
    assert result["toasts"] and "still signed in" in result["toasts"][0]


def test_the_sign_out_failure_has_a_place_on_the_page() -> None:
    assert 'id="logoutError"' in INDEX_HTML
    menu = INDEX_HTML[INDEX_HTML.index('id="userMenu"') :]
    # It sits inside the user menu, right after the Sign out button that retries.
    assert 0 < menu.index('id="logoutError"') - menu.index('id="logoutBtn"') < 400
    at = menu.index('id="logoutError"')
    assert 'role="alert"' in menu[at - 40 : at + 120]
    assert ".user-menu-error" in STYLES_CSS


# --------------------------------------------------------------------------
# 2. Nothing of the previous account survives clearWorkspaceState().
# --------------------------------------------------------------------------
CLEAR_HARNESS = """
%(functions)s

let workspaceEpoch = 0;
const SUBMISSION_STORAGE_KEY = "submissions";
const CREATIVE_TURN_STORAGE_KEY = "turns";
const CHARACTERS_EMPTY = "<empty/>";
const removed = [];
const sessionStorage = { removeItem: (key) => removed.push(key) };
const URL = { revokeObjectURL() {} };
const rendered = [];
const nodes = new Proxy({}, {
  get(target, id) {
    if (!(id in target)) {
      target[id] = {
        value: `old:${id}`, innerHTML: `<b>old ${id}</b>`, textContent: `old ${id}`,
        hidden: false, disabled: false, className: "", querySelector() { return null; },
      };
    }
    return target[id];
  },
});
const $ = (id) => nodes[id];
const setText = (id, value) => { nodes[id].textContent = value; };
function stopPassengerPolling() { rendered.push("stopPassengerPolling"); }
function stopCreativePolling() { rendered.push("stopCreativePolling"); }
function bindCharactersEmptyCta() {}
function clearReferencePreview() {}
function renderAssetRail() { rendered.push("renderAssetRail"); }
function renderProductions() { rendered.push("renderProductions"); }
function resetProductionView() { rendered.push("resetProductionView"); }
function renderPassengerJob() { rendered.push("renderPassengerJob"); }
function renderEpisodeStrip() { rendered.push("renderEpisodeStrip"); }
function renderCreative() { rendered.push("renderCreative"); }
function renderGenerationControl(job) { rendered.push(`renderGenerationControl:${job}`); }
function announceWorkspace(workspaceId) {
  state.workspaceId = workspaceId;
  rendered.push(`announce:${workspaceId}`);
}

const state = {
  projects: [{ id: "p1" }], project: { id: "p1", workspace_id: "w1" }, workspaceId: "w1",
  episode: { id: "e1" }, shot: { id: "s1" }, candidates: [{ id: "c1" }], characters: [{ id: "ch1" }],
  selectedCharacterId: "ch1",
  episodes: [{ id: "e1", title: "Alice's pilot" }],
  creative: {
    sessions: [{ id: "cs1", title: "Alice's secret film" }],
    session: { session: { id: "cs1" }, turns: [{ content: "private words" }] },
    beatEdits: { 1: {} }, editingBrief: true, editingScreenplay: true, revealedTurn: 4,
    revealedScreenplay: "sp1", thinking: { userText: "hmm" }, drafting: "writing",
  },
  continuation: { mode: "TIME_JUMP", view: { id: "cont1", brief: { premise: "Alice's next episode" } } },
  operations: { providers: [{ name: "x" }], skills: [{ id: "k" }], job: { id: "job1" } },
  passengerOriginal: { prompt: "alice" }, passengerJobs: { image: { id: "j" }, video: null },
  passengerPrompts: { image: "alice prompt", video: "" }, passengerReferenceUpload: { id: "u" },
  submissions: { passenger: "k1", shot: "k2" }, jobs: new Map([["job1", {}]]), selectedJobId: "job1",
  jobFilter: "running", credits: 120, imageTiers: [{}], savingJobId: "job1",
  assetMediaIds: new Map([["a", "m"]]), thumbCache: new Map([["m", null]]),
  confirmedAssets: new Set(["a"]), logicalAssets: [{ id: "a" }], styleLock: { id: "lock" },
};

clearWorkspaceState();
console.log(JSON.stringify({
  epoch: workspaceEpoch,
  state: {
    projects: state.projects, project: state.project, workspaceId: state.workspaceId,
    episodes: state.episodes, creative: state.creative, continuation: state.continuation,
    operations: state.operations, selectedCharacterId: state.selectedCharacterId,
    passengerOriginal: state.passengerOriginal, jobFilter: state.jobFilter, credits: state.credits,
    jobs: [...state.jobs.keys()], characters: state.characters, candidates: state.candidates,
  },
  inputs: Object.fromEntries([
    "creativeIdeaInput", "creativeReplyInput", "creativeScreenplayJson", "continuationTimeGap",
    "continuationLocation", "continuationGuidance", "operationsJobId", "passengerPrompt", "scriptInput",
    "rawPrompt", "compiledPrompt",
  ].map((id) => [id, nodes[id].value])),
  html: Object.fromEntries([
    "creativeTurns", "creativeBriefFields", "creativeBriefEditor", "creativeQuestions", "creativeAssumptions",
    "creativeScreenplayBody", "creativeScreenplayEditor", "creativeAnchorGrid", "continuationPreview",
    "creativeSessionList",
  ].map((id) => [id, nodes[id].innerHTML])),
  sessionCount: nodes.creativeSessionCount.textContent,
  credits: nodes.creditsAmount.textContent,
  removed,
  rendered,
}));
"""


@pytest.fixture(scope="module")
def cleared(tmp_path_factory) -> dict:  # type: ignore[no-untyped-def]
    functions = "\n".join(
        _function_source(name) for name in ("clearWorkspaceState", "renderCreativeSessionsEmpty")
    )
    return _run(tmp_path_factory.mktemp("clear"), CLEAR_HARNESS % {"functions": functions})


def test_sign_out_forgets_the_director_sessions_episodes_and_drafts(cleared: dict) -> None:
    state = cleared["state"]
    assert state["creative"]["sessions"] == [] and state["creative"]["session"] is None
    assert state["creative"]["beatEdits"] == {} and state["creative"]["thinking"] is None
    assert state["creative"]["drafting"] is None and state["creative"]["revealedTurn"] is None
    assert state["episodes"] == []
    assert state["continuation"] == {"mode": "CONTINUOUS", "view": None}
    assert state["operations"] == {"providers": [], "skills": [], "job": None}
    assert state["selectedCharacterId"] is None and state["characters"] == []
    assert state["passengerOriginal"] is None and state["jobFilter"] == "all"
    assert state["projects"] == [] and state["project"] is None and state["workspaceId"] is None
    assert state["jobs"] == [] and state["credits"] is None and state["candidates"] == []


def test_sign_out_empties_every_prompt_box_and_the_conversation_dom(cleared: dict) -> None:
    assert all(value == "" for value in cleared["inputs"].values()), cleared["inputs"]
    emptied = {key: value for key, value in cleared["html"].items() if key != "creativeSessionList"}
    assert all(value == "" for value in emptied.values()), emptied
    assert "No director sessions yet" in cleared["html"]["creativeSessionList"]
    assert "Alice" not in json.dumps(cleared["html"])
    assert cleared["sessionCount"] == "0"
    assert cleared["credits"] == "—"


def test_sign_out_stops_the_polls_repaints_the_rails_and_drops_the_stores(cleared: dict) -> None:
    for name in ("stopCreativePolling", "stopPassengerPolling", "renderEpisodeStrip", "renderCreative",
                 "renderGenerationControl:null", "renderProductions", "resetProductionView", "announce:null"):
        assert name in cleared["rendered"], name
    assert set(cleared["removed"]) == {"submissions", "turns"}
    assert cleared["epoch"] == 1, "every reset starts a new epoch, so in-flight answers are dropped"


EPOCH_HARNESS = """
%(functions)s

let workspaceEpoch = 0;
const staleEpoch = (epoch) => epoch !== workspaceEpoch;
const state = {
  project: { id: "p1" }, creative: { sessions: [{ id: "keep" }] }, episodes: [{ id: "keep" }], projects: [],
};
const painted = [];
const nodes = {
  projectSelect: { innerHTML: "" },
  creativeSessionCount: { textContent: "" },
  creativeSessionList: { className: "", innerHTML: "", querySelector() { return null; } },
};
const $ = (id) => nodes[id];
function renderEpisodeStrip() { painted.push("episodes"); }
function renderCreativeSessionsEmpty() { painted.push("sessions-empty"); }
function selectProject() { painted.push("selectProject"); }
function clearWorkspaceState() { painted.push("clear"); }
function escapeHTML(value) { return String(value); }
function creativeSessionId() { return null; }
const CREATIVE_STAGE_LABEL = {};
// Every request resolves only after the sign-out has happened.
async function request(path) {
  workspaceEpoch += 1;
  if (path.endsWith("/episodes")) return [{ id: "alice-episode" }];
  if (path.startsWith("/v1/creative/sessions")) return [{ id: "alice-session", title: "secret" }];
  return [{ id: "alice-project", name: "Alice" }];
}
await loadEpisodeStrip();
await loadCreativeSessions();
await loadProjects();
console.log(JSON.stringify({
  episodes: state.episodes, sessions: state.creative.sessions, projects: state.projects,
  painted, select: nodes.projectSelect.innerHTML,
}));
"""


def test_an_answer_that_arrives_after_sign_out_is_dropped(tmp_path) -> None:  # type: ignore[no-untyped-def]
    functions = "\n".join(
        _function_source(name) for name in ("loadEpisodeStrip", "loadCreativeSessions", "loadProjects")
    )
    result = _run(tmp_path, EPOCH_HARNESS % {"functions": functions})
    assert result["episodes"] == [{"id": "keep"}]
    assert result["sessions"] == [{"id": "keep"}]
    assert result["projects"] == []
    assert result["painted"] == [], "nothing is painted from a stale answer"
    assert result["select"] == ""


def test_the_loaders_that_write_private_state_all_carry_the_fence() -> None:
    for name in ("loadProjects", "selectProject", "loadLogicalAssets", "loadEpisode", "loadCandidates",
                 "loadCharacters", "loadEpisodeStrip", "loadCreativeSessions", "openCreativeSession",
                 "refreshProductions", "loadCredits", "loadOperations"):
        source = _function_source(name)
        assert "const epoch = workspaceEpoch;" in source, name
        assert "staleEpoch(epoch)" in source, name


def test_the_identity_strip_is_cleared_with_the_workspace() -> None:
    source = _function_source("lockAuth")
    for id_ in ("accountName", "userMenuEmail", "userAvatar"):
        assert f'$("{id_}")' in source, id_


# --------------------------------------------------------------------------
# 3. The folded inspector can be opened.
# --------------------------------------------------------------------------
def test_the_top_bar_carries_the_inspector_toggle() -> None:
    assert 'id="inspectorToggleBtn"' in INDEX_HTML
    button = INDEX_HTML[INDEX_HTML.index('id="inspectorToggleBtn"') - 200 :][:600]
    assert 'aria-controls="appInspector"' in button
    assert 'aria-expanded="false"' in button
    assert 'id="appInspector"' in INDEX_HTML


def test_the_toggle_sets_the_class_the_fold_listens_for() -> None:
    source = _function_source("setInspectorVisible")
    assert 'classList.toggle("show-inspector", open)' in source
    assert 'setAttribute("aria-expanded", String(open))' in source
    assert 'on("inspectorToggleBtn", "click"' in APP_JS
    # The fold itself is unchanged: hidden by default under 1280px, shown by the class.
    fold = re.search(r"@media \(max-width: 1280px\) \{(.*?)\n\}", STYLES_CSS, re.S)
    assert fold, "the narrow-desktop fold is still declared"
    assert ".app-body.show-inspector .app-inspector { display: flex; }" in fold.group(1)
    assert ".inspector-toggle { display: inline-flex" in fold.group(1), (
        "the toggle exists only where the fold applies"
    )
    assert re.search(r"^\.inspector-toggle \{ display: none;", STYLES_CSS, re.M), "and nowhere else"


def test_the_toggle_is_hidden_where_there_is_no_inspector() -> None:
    source = _function_source("switchPage")
    assert '$("inspectorToggleBtn").hidden = page === "admin"' in source


TOGGLE_HARNESS = """
%(functions)s

const classes = new Set();
const nodes = {
  appBody: {
    classList: {
      toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
      contains: (name) => classes.has(name),
    },
  },
  inspectorToggleBtn: {
    attrs: {}, textContent: "Settings", setAttribute(name, value) { this.attrs[name] = value; },
  },
};
const $ = (id) => nodes[id];
const steps = [];
for (const open of [true, false, true]) {
  setInspectorVisible(open);
  steps.push({
    shown: classes.has("show-inspector"),
    expanded: nodes.inspectorToggleBtn.attrs["aria-expanded"],
    label: nodes.inspectorToggleBtn.textContent,
  });
}
console.log(JSON.stringify(steps));
"""


def test_the_toggle_opens_and_closes_the_inspector(tmp_path) -> None:  # type: ignore[no-untyped-def]
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required")
    path = tmp_path / "toggle.mjs"
    path.write_text(TOGGLE_HARNESS % {"functions": _function_source("setInspectorVisible")}, encoding="utf-8")
    completed = subprocess.run(
        [node, str(path)], capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr
    steps = json.loads(completed.stdout.strip().splitlines()[-1])
    assert steps == [
        {"shown": True, "expanded": "true", "label": "Hide settings"},
        {"shown": False, "expanded": "false", "label": "Settings"},
        {"shown": True, "expanded": "true", "label": "Hide settings"},
    ]
