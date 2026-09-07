"""The 2026-09-07 production review's browser-side findings, pinned.

Twenty-two findings were checked against ``0d4e71e``; the ones that live in
the served web files are pinned here by lifting the shipped functions out of
``app.js`` / ``admin.js`` / ``background.js`` by name and running them in
Node against stubs, plus a few structural facts about the markup, the
stylesheet and the nginx configuration:

1. the Director stage shows the variant the director opened - not always
   the approved take or the first one - says which take it is, and a human
   review is refused until that variant is the one on the stage;
2. a project switch carries a sequence number: an answer for the project
   the user has since left is dropped, in ``selectProject`` and in every
   loader that writes project state;
3. a deleted creation is forgotten everywhere - the Create canvas, its poll,
   the save dialog - and cannot be remembered back;
4. the credits line knows reserved, charged, refunded and held apart, and
   Try again / Cancel follow the server's ``allowed_actions``;
5. Auto video names no role and no criticality, and the plan the page acts
   on is the open project's workspace's, not any workspace of the account;
6. media with no ``public_url`` is read through the storage key;
7. an approved shot cannot be "regenerated"; the IME's Enter is not a send;
   key visuals go through the thumbnail cache; the listing pages older
   creations; the admin console maps route names to detail kinds, opens an
   audit row from the record it has, pages its lists and scrolls;
8. the worker extension leaves ``busy`` after a failed heartbeat, and the
   web proxy forwards a WebSocket upgrade.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")
ADMIN_JS = (WEB / "admin.js").read_text(encoding="utf-8")
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")
STYLES_CSS = (WEB / "styles.css").read_text(encoding="utf-8")
ADMIN_CSS = (WEB / "admin.css").read_text(encoding="utf-8")
NGINX_CONF = (WEB / "nginx.conf").read_text(encoding="utf-8")
BACKGROUND_JS = (ROOT / "apps" / "browser-worker-extension" / "background.js").read_text(encoding="utf-8")


def _balanced(source: str, start: int, opener: str, closer: str) -> int:
    depth = 0
    for position in range(start, len(source)):
        character = source[position]
        if character == opener:
            depth += 1
        elif character == closer:
            depth -= 1
            if depth == 0:
                return position + 1
    raise AssertionError("unbalanced source")


def _function_source(name: str, source: str = APP_JS) -> str:
    """One top-level function, brace-matched, verbatim."""

    for opener in (f"\nfunction {name}(", f"\nasync function {name}("):
        start = source.find(opener)
        if start != -1:
            break
    assert start != -1, f"the module no longer defines {name}()"
    start += 1
    parameters_end = _balanced(source, source.index("(", start), "(", ")")
    return source[start : _balanced(source, source.index("{", parameters_end), "{", "}")]


def _const_source(name: str, source: str = APP_JS) -> str:
    """One top-level ``const NAME = …;`` - an object, an array or one line."""

    start = source.find(f"\nconst {name} = ")
    assert start != -1, f"the module no longer defines {name}"
    start += 1
    value_at = start + len(f"const {name} = ")
    if source[value_at] in "{[":
        closer = "}" if source[value_at] == "{" else "]"
        end = _balanced(source, value_at, source[value_at], closer)
        end = source.index(";", end) + 1
    else:
        end = source.index(";\n", value_at) + 1
    return source[start:end]


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
# 1. The stage shows the take the director opened, and says which it is.
# --------------------------------------------------------------------------
STAGE_HARNESS = """
%(functions)s
const state = { candidates: [
  { id: "a", status: "USER_REVIEW_REQUIRED", output_asset_id: "asset-a" },
  { id: "b", status: "USER_REVIEW_REQUIRED", output_asset_id: "asset-b" },
  { id: "c", status: "QUEUED", output_asset_id: null },
], previewCandidateId: null };
const first = stagedCandidate()?.id;
state.previewCandidateId = "b";
const opened = stagedCandidate()?.id;
state.candidates[0].status = "COMMITTED";
state.previewCandidateId = null;
const committedWins = stagedCandidate()?.id;
state.previewCandidateId = "b";
const openedBeatsCommitted = stagedCandidate()?.id;
state.previewCandidateId = "c";
const noOutputFallsBack = stagedCandidate()?.id;
console.log(JSON.stringify({
  first, opened, committedWins, openedBeatsCommitted, noOutputFallsBack,
  letter: variantLetter(state.candidates[1]),
}));
"""


def test_the_stage_prefers_the_opened_variant_then_the_approved_take(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _run(tmp_path, STAGE_HARNESS % {
        "functions": "\n".join(_function_source(name) for name in ("stagedCandidate", "variantLetter")),
    })
    assert result == {
        "first": "a",
        "opened": "b",
        "committedWins": "a",
        "openedBeatsCommitted": "b",
        "noOutputFallsBack": "a",
        "letter": "B",
    }


RENDER_HARNESS = """
%(functions)s
const QA_SUMMARY = {};
const ICON_FRAME = "";
const state = { candidates: [
  { id: "a", status: "USER_REVIEW_REQUIRED", output_asset_id: "asset-a", cost: 0.1 },
  { id: "b", status: "USER_REVIEW_REQUIRED", output_asset_id: "asset-b", cost: 0.1 },
  { id: "c", status: "QUEUED", output_asset_id: null, cost: 0.1 },
], previewCandidateId: "b" };
const grid = { className: "", innerHTML: "", querySelectorAll: () => [] };
const $ = () => grid;
const escapeHTML = (value = "") => String(value);
const statusTone = () => "is-neutral";
const simpleLabel = (value) => value;
const sentenceCase = (value) => value;
const humanizeCode = (value) => value;
const guard = (fn) => fn;
renderCandidates(state.candidates);
console.log(JSON.stringify({ html: grid.innerHTML }));
"""


def test_every_variant_with_output_can_be_put_on_the_stage_and_the_staged_one_is_marked(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _run(tmp_path, RENDER_HARNESS % {
        "functions": "\n".join(_function_source(name) for name in ("renderCandidates", "stagedCandidate")),
    })
    html = result["html"]
    cards = re.findall(r'<article class="variant([^"]*)" data-variant="([a-z])">(.*?)</article>', html, re.S)
    assert [card[1] for card in cards] == ["a", "b", "c"]
    by_id = {card[1]: card for card in cards}
    assert 'data-preview="a"' in by_id["a"][2] and ">View<" in by_id["a"][2]
    assert 'data-preview="b"' in by_id["b"][2] and "disabled>On stage<" in by_id["b"][2]
    assert "data-preview" not in by_id["c"][2], "a variant with no output has nothing to show"
    assert "is-staged" in by_id["b"][0] and "is-staged" not in by_id["a"][0]
    assert "It is on the stage above." in by_id["b"][2]
    assert "Put it on the stage first" in by_id["a"][2]


REVIEW_HARNESS = """
%(functions)s
const calls = [];
const toasts = [];
const state = { shot: { id: "shot-1" }, candidates: [
  { id: "a", status: "USER_REVIEW_REQUIRED", output_asset_id: "asset-a" },
  { id: "b", status: "USER_REVIEW_REQUIRED", output_asset_id: "asset-b" },
], previewCandidateId: null };
const document = {
  querySelector: (selector) => selector.includes("review-reason") ? { value: "Verified the join" }
    : selector.includes("review-confirm") ? { checked: true } : null,
  querySelectorAll: () => [],
};
function toast(message) { toasts.push(message); }
async function request(path) { calls.push(path); return {}; }
async function loadCandidates() { calls.push("loadCandidates"); }
function markStagedVariant() { calls.push("markStagedVariant"); }
async function renderShotStage() { calls.push("renderShotStage"); }
await humanReviewCandidate("b");
const first = { calls: [...calls], toasts: [...toasts], staged: state.previewCandidateId };
calls.length = 0; toasts.length = 0;
await humanReviewCandidate("b");
console.log(JSON.stringify({ first, second: { calls, toasts } }));
"""


def test_a_human_review_is_of_the_variant_on_the_stage(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _run(tmp_path, REVIEW_HARNESS % {
        "functions": "\n".join(
            _function_source(name)
            for name in ("humanReviewCandidate", "stagedCandidate", "stageCandidate", "variantLetter")
        ),
    })
    first = result["first"]
    # Variant A was on the stage (first with output); confirming B first puts
    # B on the stage and asks again - nothing is recorded yet.
    assert not any("human-review" in call for call in first["calls"])
    assert first["calls"] == ["markStagedVariant", "renderShotStage"]
    assert first["staged"] == "b"
    assert first["toasts"] == ["Variant B is on the stage now. Look at it, then confirm."]
    # With B on the stage the same click records the review.
    assert "/v1/shots/shot-1/candidates/b/human-review" in result["second"]["calls"]


def test_the_stage_caption_names_the_take_and_a_late_render_does_not_paint_over_a_newer_one() -> None:
    source = _function_source("renderShotStage")
    assert "const sequence = ++shotStageSequence;" in source
    assert "if (sequence !== shotStageSequence) {" in source
    assert "Approved take · Variant" in source
    assert "`Variant ${letter} · ${simpleLabel(shown.status)}`" in source
    assert 'id="shotStageTake"' in INDEX_HTML
    assert ".variant.is-staged" in STYLES_CSS
    # Picking a different shot forgets which variant was on the stage.
    assert "if (state.shot?.id !== id) state.previewCandidateId = null;" in _function_source("selectShot")


# --------------------------------------------------------------------------
# 2. A project switch that races with itself ends on the project picked last.
# --------------------------------------------------------------------------
SWITCH_HARNESS = """
%(functions)s
let workspaceEpoch = 0;
const staleEpoch = (epoch) => epoch !== workspaceEpoch;
let projectLoadSequence = 0;
const projectChanged = (projectId) => state.project?.id !== projectId;
const state = {
  project: null, projects: [], episode: null, creative: { session: null },
  passengerJobs: { image: null, video: null }, passengerMedia: "image", page: "create",
  confirmedAssets: new Set(), previewCandidateId: null, productionsPaging: null,
};
const nodes = {
  projectSelect: { value: "" }, passengerReference: { value: "" }, referenceFileName: { hidden: false },
};
const $ = (id) => nodes[id] || { value: "", hidden: false };
const loads = [];
const pending = new Map();
function request(path) { return new Promise((resolve) => { pending.set(path, resolve); }); }
function stopPassengerPolling() {}
function clearReferencePreview() {}
async function renderPassengerJob() {}
function announceWorkspace(id) { loads.push(`workspace:${id}`); }
function renderCreative() {}
async function loadPassengerModels() {}
async function loadImageTiers() {}
async function loadLogicalAssets() { loads.push(`assets:${state.project.id}`); }
async function loadCharacters() { loads.push(`characters:${state.project.id}`); }
async function loadEpisodeStrip() { loads.push(`episodes:${state.project.id}`); }
async function loadEpisode(id) { loads.push(`episode:${id}`); }
function resetProductionView() { loads.push(`reset:${state.project.id}`); }
async function loadCreativeSessions() {}
async function refreshProductions() {}
function syncOperationsContext() { loads.push(`sync:${state.project.id}`); }
const a = selectProject("A");
const b = selectProject("B");
await new Promise((resolve) => setTimeout(resolve, 0));
pending.get("/v1/projects/B")({ id: "B", workspace_id: "w-b", episodes: [] });
await b;
pending.get("/v1/projects/A")({ id: "A", workspace_id: "w-a", episodes: [] });
await a;
console.log(JSON.stringify({ project: state.project.id, select: nodes.projectSelect.value, loads }));
"""


def test_the_project_picked_last_wins_even_when_its_answer_arrives_first(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _run(tmp_path, SWITCH_HARNESS % {"functions": _function_source("selectProject")})
    assert result["project"] == "B"
    assert result["select"] == "B"
    assert result["loads"] == [
        "workspace:w-b", "assets:B", "characters:B", "episodes:B", "reset:B", "sync:B",
    ], "nothing of project A - answered later - may be applied"


LOADER_HARNESS = """
%(functions)s
let workspaceEpoch = 0;
const staleEpoch = (epoch) => epoch !== workspaceEpoch;
const projectChanged = (projectId) => state.project?.id !== projectId;
const state = {
  project: { id: "A" }, characters: [], selectedCharacterId: null,
  episodes: [], logicalAssets: [], styleLock: null,
};
const resolvers = [];
function request() { return new Promise((resolve) => { resolvers.push(resolve); }); }
function renderCharacters() { throw new Error("must not render another project's characters"); }
function renderEpisodeStrip() { throw new Error("must not render another project's episodes"); }
const characters = loadCharacters();
const episodes = loadEpisodeStrip();
state.project = { id: "B" };
resolvers.forEach((resolve) => resolve([{ id: "row" }]));
await characters;
await episodes;
console.log(JSON.stringify({ characters: state.characters, episodes: state.episodes }));
"""


def test_a_loader_drops_an_answer_for_a_project_no_longer_open(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _run(tmp_path, LOADER_HARNESS % {
        "functions": "\n".join(_function_source(name) for name in ("loadCharacters", "loadEpisodeStrip")),
    })
    assert result == {"characters": [], "episodes": []}


def test_every_project_scoped_loader_checks_the_project_it_started_for() -> None:
    for name in (
        "loadLogicalAssets", "loadCharacters", "loadEpisodeStrip", "loadEpisode", "selectShot",
        "loadCreativeSessions", "openCreativeSession", "refreshProductions", "loadProjectGenerations",
    ):
        assert "projectChanged(projectId)" in _function_source(name), name
    assert "state.shot?.id !== shotId" in _function_source("loadCandidates")


# --------------------------------------------------------------------------
# 3. A deleted creation is forgotten everywhere, and stays forgotten.
# --------------------------------------------------------------------------
FORGET_HARNESS = """
%(functions)s
const calls = [];
const state = {
  project: { id: "p" }, forgottenJobs: new Set(),
  jobs: new Map([["job-1", { id: "job-1", project_id: "p" }]]),
  passengerJobs: { image: { id: "job-1" }, video: null }, passengerMedia: "image", savingJobId: "job-1",
  selectedJobId: "job-1", operations: { job: { id: "job-1" } },
};
const $ = () => null;
function stopPassengerPolling() { calls.push("stopPolling"); }
async function renderPassengerJob(job) { calls.push(`canvas:${job}`); }
function renderGenerationControl(job) { calls.push(`inspector:${job}`); }
function renderProductions() { calls.push("productions"); }
forgetJob("job-1");
rememberJob({ id: "job-1", project_id: "p", status: "COMPLETED" });
rememberJob({ id: "job-2", project_id: "p", status: "COMPLETED" });
console.log(JSON.stringify({
  calls, remembered: [...state.jobs.keys()], canvasJob: state.passengerJobs.image, saving: state.savingJobId,
}));
"""


def test_a_deleted_creation_leaves_the_canvas_and_cannot_be_remembered_back(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _run(tmp_path, FORGET_HARNESS % {
        "functions": "\n".join(_function_source(name) for name in ("forgetJob", "rememberJob")),
    })
    assert result["calls"] == ["stopPolling", "canvas:null", "inspector:null", "productions"]
    assert result["remembered"] == ["job-2"], "the deleted creation is refused, a live one remembered"
    assert result["canvasJob"] is None
    assert result["saving"] is None


# --------------------------------------------------------------------------
# 4. The credits line, and the actions the server would accept.
# --------------------------------------------------------------------------
CREDIT_HARNESS = """
%(consts)s
%(functions)s
const state = { operations: {}, jobs: new Map(), project: { id: "p" } };
const nodes = {};
const $ = (id) => nodes[id]
  || (nodes[id] = { value: "", className: "", innerHTML: "", disabled: null, textContent: "" });
function setText() {}
function renderProductions() {}
function rememberJob() {}
const statusTone = () => "t";
const simpleLabel = (value) => value;
const friendlyModel = (value) => value;
const escapeHTML = (value = "") => String(value);
const sentenceCase = (value) => value;
const humanizeCode = (value = "") => String(value).replaceAll("_", " ").toLowerCase();
function jobCredits(job) { return Number(job.estimated_credits || 0); }
const EVENT_LABELS = {};
const lines = ["RESERVED", "SETTLED", "REFUNDED", "RECONCILIATION_REQUIRED", null]
  .map((credit_status) => creditLine({ estimated_credits: 10, credit_status }));
renderGenerationControl({
  id: "j", status: "FAILED", credit_status: "REFUNDED", estimated_credits: 10, safe_to_retry: true,
  submission_state: "NOT_SENT", allowed_actions: { retry: false, cancel: false, resubmit: true }, events: [],
});
const failed = {
  html: nodes.generationControlStatus.innerHTML,
  retry: nodes.retryJobBtn.disabled, cancel: nodes.cancelJobBtn.disabled,
};
renderGenerationControl({
  id: "k", status: "RETRY_WAIT", credit_status: "RESERVED", estimated_credits: 10, safe_to_retry: true,
  submission_state: "NOT_SENT", events: [],
});
const waiting = { retry: nodes.retryJobBtn.disabled, cancel: nodes.cancelJobBtn.disabled };
console.log(JSON.stringify({ lines, failed, waiting }));
"""


def test_refunded_credits_are_not_charged_and_try_again_follows_the_server(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _run(tmp_path, CREDIT_HARNESS % {
        "consts": _const_source("CREDIT_STATUS_COPY"),
        "functions": "\n".join(
            _function_source(name) for name in ("creditLine", "jobActions", "renderGenerationControl")
        ),
    })
    assert result["lines"] == [
        "10 credits reserved",
        "10 credits charged",
        "10 credits refunded",
        "10 credits held while the charge is checked",
        "10 credits quoted",
    ]
    failed = result["failed"]
    assert "10 credits refunded" in failed["html"] and "charged" not in failed["html"]
    assert failed["retry"] is True and failed["cancel"] is True, "a finished failure offers neither"
    assert "start a new one from Create" in failed["html"]
    # Without the server's verdict the local rule is the gateway's own.
    assert result["waiting"] == {"retry": False, "cancel": False}
    assert "creditLine(job)" in _function_source("openDeleteCreationDialog")
    assert "credits used" not in APP_JS


# --------------------------------------------------------------------------
# 5. The plan the page acts on is the open project's; Auto names nothing.
# --------------------------------------------------------------------------
PLAN_HARNESS = """
%(functions)s
const projectOnFreePlan = () => projectPlanTier() === "FREE";
const state = {
  authUser: { workspaces: [{ id: "w-free", plan_tier: "FREE" }, { id: "w-pro", plan_tier: "PRO" }] },
  project: { id: "p", workspace_id: "w-pro" },
};
const pro = projectOnFreePlan();
state.project.workspace_id = "w-free";
const free = projectOnFreePlan();
state.project = null;
const none = projectOnFreePlan();
console.log(JSON.stringify({ pro, free, none }));
"""


def test_a_pro_project_is_not_treated_as_free_because_another_workspace_is(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _run(tmp_path, PLAN_HARNESS % {"functions": _function_source("projectPlanTier")})
    assert result == {"pro": False, "free": True, "none": False}
    assert "isFreeWorkspace" not in APP_JS
    generate = _function_source("generatePassenger")
    gone_for_good = ("model_role", "asset_criticality", "passengerCriticality", "freeVideo")
    for gone in gone_for_good:
        assert gone not in generate, gone
    assert 'id="passengerCriticality"' not in INDEX_HTML


# --------------------------------------------------------------------------
# 6. Media with no address is read through its storage key.
# --------------------------------------------------------------------------
MEDIA_HARNESS = """
%(functions)s
const API = "/api";
const location = { href: "https://bestshiny.example/app" };
const fetched = [];
class URL extends globalThis.URL { static createObjectURL() { return "blob:take"; } }
const assets = {
  direct: { id: "direct", public_url: null, storage_key: "uploads/ab/x.png", mime_type: "image/png" },
  local: {
    id: "local", public_url: "https://bestshiny.example/api/v1/storage/out/y.mp4",
    storage_key: "out/y.mp4", mime_type: "video/mp4",
  },
  remote: {
    id: "remote", public_url: "https://cdn.example/z.png", storage_key: "z.png", mime_type: "image/png",
  },
};
async function request(path) { return assets[path.split("/").pop()]; }
async function fetch(url) { fetched.push(url); return { ok: true, blob: async () => ({}) }; }
const direct = await resolveAssetMedia("direct");
const local = await resolveAssetMedia("local");
const remote = await resolveAssetMedia("remote");
console.log(JSON.stringify({ fetched, direct, local, remote }));
"""


def test_an_asset_with_no_public_url_is_read_through_its_storage_key(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _run(tmp_path, MEDIA_HARNESS % {"functions": _function_source("resolveAssetMedia")})
    assert result["fetched"] == ["/api/v1/storage/uploads/ab/x.png", "/api/v1/storage/out/y.mp4"]
    assert result["direct"] == {"url": "blob:take", "mime": "image/png", "revocable": True}
    assert result["local"] == {"url": "blob:take", "mime": "video/mp4", "revocable": True}
    assert result["remote"] == {"url": "https://cdn.example/z.png", "mime": "image/png", "revocable": False}
    # The Create canvas goes through the same resolver rather than its own copy.
    canvas = _function_source("renderPassengerJob")
    assert "resolveAssetMedia(job.output_asset_id)" in canvas
    assert "isProtectedLocalMedia" not in canvas


# --------------------------------------------------------------------------
# 7. The smaller contracts: approved shots, the IME, thumbnails, paging.
# --------------------------------------------------------------------------
def test_an_approved_shot_offers_no_regenerate() -> None:
    select = _function_source("selectShot")
    assert '$("generateBtn").disabled = approved;' in select
    assert "Approved · in the timeline" in select
    assert "Regenerate shot" not in APP_JS
    generate = _function_source("generateShot")
    assert 'if (state.shot.status === "COMMITTED")' in generate


def test_enter_during_ime_composition_does_not_send() -> None:
    handler = APP_JS[APP_JS.index('on("creativeReplyInput", "keydown"') :][:400]
    assert "event.isComposing" in handler and "event.keyCode === 229" in handler
    assert handler.index("isComposing") < handler.index('event.key === "Enter"')


def test_the_length_that_runs_is_shown_beside_the_length_asked_for() -> None:
    canvas = _function_source("renderPassengerJob")
    assert "job.requested_duration" in canvas
    assert "(asked ${askedLength}s)" in canvas
    submit = _function_source("generatePassenger")
    assert "this model shoots fixed lengths" in submit


def test_key_visuals_reuse_the_thumbnail_cache() -> None:
    creative = _function_source("renderCreative")
    assert "cachedAssetThumbnail(anchor.media_asset_id)" in creative
    assert "resolveAssetThumbnail(anchor" not in creative


PAGING_HARNESS = """
%(functions)s
const state = { project: { id: "p" }, productionsPaging: null, jobs: new Map(), forgottenJobs: new Set() };
const requested = [];
const answers = [
  { jobs: [{ id: "j9" }, { id: "j8" }], has_more: true, next_cursor: "j8" },
  { jobs: [{ id: "j7" }], has_more: false, next_cursor: null },
  { jobs: [{ id: "j9" }, { id: "j8" }], has_more: true, next_cursor: "j8" },
];
async function request(path) { requested.push(path); return answers.shift(); }
const projectChanged = (projectId) => state.project?.id !== projectId;
await loadProjectGenerations();
const afterFirst = { ...state.productionsPaging };
await loadProjectGenerations({ older: true });
const afterOlder = { ...state.productionsPaging };
await loadProjectGenerations();
const afterRefresh = { ...state.productionsPaging };
console.log(JSON.stringify({
  requested, afterFirst, afterOlder, afterRefresh, remembered: [...state.jobs.keys()],
}));
"""


def test_the_productions_listing_pages_older_creations_by_cursor(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _run(tmp_path, PAGING_HARNESS % {
        "functions": "\n".join(
            _function_source(name) for name in ("loadProjectGenerations", "productionsPaging", "rememberJob")
        ),
    })
    assert result["requested"] == [
        "/v1/generations?project_id=p&limit=100",
        "/v1/generations?project_id=p&limit=100&before=j8",
        "/v1/generations?project_id=p&limit=100",
    ]
    assert result["afterFirst"] == {"projectId": "p", "older": False, "cursor": "j8", "hasMore": True}
    assert result["afterOlder"] == {"projectId": "p", "older": True, "cursor": None, "hasMore": False}
    # A refresh re-reads the newest page but keeps the depth already reached,
    # so an exhausted listing does not offer its second page again.
    assert result["afterRefresh"] == {"projectId": "p", "older": True, "cursor": None, "hasMore": False}
    assert result["remembered"] == ["j9", "j8", "j7"]
    assert 'id="productionsMore"' in INDEX_HTML and 'id="productionsMoreBtn"' in INDEX_HTML
    assert 'on("productionsMoreBtn", "click", guard(loadOlderProductions))' in APP_JS
    assert ".load-more" in STYLES_CSS


def test_the_director_inspector_no_longer_offers_controls_nothing_reads() -> None:
    assert 'id="cameraScale"' not in INDEX_HTML
    assert 'id="lighting"' not in INDEX_HTML
    assert 'id="cameraAngle"' in INDEX_HTML and 'id="cameraMove"' in INDEX_HTML
    camera = INDEX_HTML[INDEX_HTML.index("Camera, for the continuity check") :][:1800]
    assert "Used only by Check continuity" in camera


# --------------------------------------------------------------------------
# 8. The admin console: route kinds, audit rows, pages, scrolling.
# --------------------------------------------------------------------------
ADMIN_HARNESS = """
%(functions)s
const calls = [];
const auditRecords = new Map([["log-1", { id: "log-1", action: "CREDITS_ADJUSTED" }]]);
const activePath = "/admin/audit";
const location = { pathname: "/admin/audit" };
const history = { replaceState() {} };
function markCurrentRow() {}
function showDrawer(title, data, kind) {
  calls.push({ drawer: title, id: data?.id ?? data?.error, kind: kind || "" });
}
async function request(path) {
  calls.push({ request: path });
  return { items: [{ id: "log-2", action: "USER_SUSPENDED" }], account: { email: "a@b" }, id: "u-1" };
}
await openDetail("audit", "log-1");
await openDetail("audit", "log-2");
await openDetail("user", "u-1");
const pages = [
  pager({ total: 120, limit: 50, offset: 50 }), pager({ total: 10, limit: 50, offset: 0 }), pager(undefined),
];
console.log(JSON.stringify({ calls, pages }));
"""


def test_the_admin_console_opens_the_audit_record_it_has_and_maps_route_names(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _run(tmp_path, ADMIN_HARNESS % {
        "functions": "const fmt = (value) => String(value);\n" + "\n".join(
            _function_source(name, ADMIN_JS) for name in ("openDetail", "pager")
        ),
    })
    assert result["calls"] == [
        {"drawer": "CREDITS_ADJUSTED", "id": "log-1", "kind": ""},
        {"request": "/admin/audit?id=log-2"},
        {"drawer": "USER_SUSPENDED", "id": "log-2", "kind": ""},
        {"request": "/admin/users/u-1"},
        {"drawer": "a@b", "id": "u-1", "kind": "user"},
    ]
    first, second, third = result["pages"]
    assert "51–100 of 120" in first and 'data-page-step="-1"' in first and 'data-page-step="1"' in first
    assert "disabled" not in first
    assert second == "" and third == ""
    assert "entity_id=" not in _function_source("openDetail", ADMIN_JS)
    load = _function_source("load", ADMIN_JS)
    assert "openDetail(DETAIL_KIND[activePage] || activePage, parsed.id)" in load
    kinds = _const_source("DETAIL_KIND", ADMIN_JS)
    assert 'users: "user"' in kinds and 'models: "model"' in kinds
    assert 'params.set("offset", String(activeOffset))' in _function_source("queryFor", ADMIN_JS)
    assert ".admin-pager" in ADMIN_CSS


def test_the_admin_console_can_scroll_a_long_list() -> None:
    rule = STYLES_CSS[STYLES_CSS.index("body.admin-route {") :][:200]
    assert "overflow: visible" in rule and "height: auto" in rule


# --------------------------------------------------------------------------
# 9. The worker extension, and the proxy's WebSocket upgrade.
# --------------------------------------------------------------------------
WORKER_HARNESS = """
%(functions)s
let busy = false;
let beats = 0;
async function heartbeat() { beats += 1; if (beats === 1) throw new Error("network"); }
const responses = [];
async function respond(commandId, response, error) { responses.push({ commandId, response, error }); }
async function providerRequest() { return { status: 200, data: {} }; }
async function mediaUrl() { return { url: "x" }; }
await processCommand({ id: "c1", type: "provider.request", payload: {} });
console.log(JSON.stringify({ busy, beats, responses }));
"""


def test_a_failed_busy_heartbeat_does_not_strand_the_worker(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _run(tmp_path, WORKER_HARNESS % {"functions": _function_source("processCommand", BACKGROUND_JS)})
    assert result["busy"] is False
    assert result["beats"] == 2, "the BUSY heartbeat failed, the READY one was still sent"
    assert result["responses"] == [
        {"commandId": "c1", "response": {"status": 200, "data": {}}, "error": None},
    ]


def test_the_web_proxy_forwards_a_websocket_upgrade() -> None:
    assert "map $http_upgrade $connection_upgrade {" in NGINX_CONF
    api = NGINX_CONF[NGINX_CONF.index("location /api/ {") :]
    api = api[: api.index("}")]
    assert "proxy_set_header Upgrade $http_upgrade;" in api
    assert "proxy_set_header Connection $connection_upgrade;" in api
    assert "proxy_http_version 1.1;" in api
