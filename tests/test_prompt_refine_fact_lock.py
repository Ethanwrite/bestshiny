"""What `/v1/prompts/refine` is allowed to hold immutable.

A fact lock has to be satisfiable by the very rewrite it is guarding. Until
2026-08-30 this call site locked the whole corrected prompt as a single span,
so the only candidate that could pass was one that repeated the prompt word for
word — which no genuine refinement does. Every live refine came back
`IMMUTABLE_FACT_CONTENT_CHANGED:narrative_event`, the original was returned
unchanged, and the model was billed for a candidate that could never be used.

What is pinned here:

1. only spans the corrected prompt actually carries become locks;
2. a real rewording that keeps those spans is accepted;
3. a prompt carrying no verbatim constraint locks nothing textual;
4. the whole-prompt lock this replaced rejects every rewording.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
from image_prompt_core.corrector import ImagePromptCorrector
from image_prompt_core.schemas import ImagePromptCorrectRequest
from provider_sdk import FactLockSet, verifiable_spans
from video_platform_api.main import create_app

CONSTRAINED = "Mina raises the red phone, do not change the sign reading OPEN"
PLAIN = "a cinematic high quality portrait of a woman"


def _register(client: TestClient, email: str) -> tuple[dict, str]:
    registered = client.post(
        "/api/auth/register",
        json={"email": email, "password": "correct horse battery staple"},
    ).json()
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    project = client.post("/v1/projects", headers=headers, json={"title": "Lock"}).json()
    return headers, project["id"]


def _capture_locks(container, monkeypatch, prompt: str, email: str) -> FactLockSet:
    """Drive the real endpoint and return the locks it handed the refiner."""

    captured: dict[str, FactLockSet] = {}

    async def refine(*_args, **kwargs):  # type: ignore[no-untyped-def]
        captured["locks"] = kwargs["fact_locks"]
        return SimpleNamespace(
            optimized_candidate="unused", accepted=True, source="test", reason_codes=(), diff=None
        )

    monkeypatch.setattr(container.model_roles, "refine_prompt", refine)
    app = create_app(container)
    with TestClient(app, raise_server_exceptions=False) as client:
        headers, project_id = _register(client, email)
        response = client.post(
            "/v1/prompts/refine", headers=headers, json={"project_id": project_id, "prompt": prompt}
        )
        assert response.status_code == 200, response.text
    return captured["locks"]


def test_verifiable_spans_keeps_only_spans_the_source_carries():
    source = "a red car, keep the sign reading OPEN"
    assert verifiable_spans(source, ["keep the sign reading OPEN"]) == ("keep the sign reading OPEN",)
    assert verifiable_spans(source, ["a fact the prompt never stated"]) == ()
    # Normalization matches the validator's: case and run-of-whitespace folded.
    assert verifiable_spans(source, ["KEEP   the sign READING open"]) == ("KEEP   the sign READING open",)
    assert verifiable_spans(source, ["", "   "]) == ()


def test_refine_locks_verbatim_spans_not_the_whole_prompt(container, monkeypatch):
    locks = _capture_locks(container, monkeypatch, CONSTRAINED, "lock-scope@example.com")
    corrected = ImagePromptCorrector().correct(ImagePromptCorrectRequest(prompt=CONSTRAINED)).corrected_prompt

    spans = locks.locked_spans["narrative_event"]
    assert spans == ("do not change the sign reading OPEN",)
    # The regression this replaces: the prompt itself must not be its own lock.
    assert corrected not in spans

    # A genuine rewording that keeps the constraint is now accepted.
    candidate = "Mina lifts the crimson handset; do not change the sign reading OPEN; warm rim light."
    valid, reasons = locks.validate(candidate, locks.immutable_facts, source_prompt=corrected)
    assert valid, reasons

    # Dropping the constraint is still caught.
    dropped = "Mina lifts the crimson handset under warm rim light."
    valid, reasons = locks.validate(dropped, locks.immutable_facts, source_prompt=corrected)
    assert not valid
    assert "IMMUTABLE_FACT_CONTENT_CHANGED:narrative_event" in reasons


def test_prompt_without_verbatim_constraints_locks_nothing(container, monkeypatch):
    locks = _capture_locks(container, monkeypatch, PLAIN, "lock-plain@example.com")
    corrected = ImagePromptCorrector().correct(ImagePromptCorrectRequest(prompt=PLAIN)).corrected_prompt

    assert locks.immutable_facts == {}
    assert locks.locked_spans == {}

    # Nothing to preserve verbatim, so the refiner is free to rewrite.
    candidate = "A quiet three-quarter portrait of a woman, soft key light, shallow depth of field."
    valid, reasons = locks.validate(candidate, locks.immutable_facts, source_prompt=corrected)
    assert valid, reasons


def test_locking_the_whole_prompt_rejects_every_rewording():
    """The construction this call site used until 2026-08-30, kept as a guard."""

    corrected = ImagePromptCorrector().correct(ImagePromptCorrectRequest(prompt=CONSTRAINED)).corrected_prompt
    whole_prompt_lock = FactLockSet(
        {"narrative_event": corrected}, locked_spans={"narrative_event": (corrected,)}
    )

    candidate = "Mina lifts the crimson handset; do not change the sign reading OPEN; warm rim light."
    valid, reasons = whole_prompt_lock.validate(
        candidate, whole_prompt_lock.immutable_facts, source_prompt=corrected
    )
    assert not valid
    assert "IMMUTABLE_FACT_CONTENT_CHANGED:narrative_event" in reasons

    # Only an exact echo passes — which is what made the lock unsatisfiable.
    echoed, _ = whole_prompt_lock.validate(
        corrected, whole_prompt_lock.immutable_facts, source_prompt=corrected
    )
    assert echoed
