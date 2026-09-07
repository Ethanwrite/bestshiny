"""The per-shot spending cap is a limit, not a note.

The Direct inspector's "Most I'll spend on this shot (USD)" travelled to the
server as ``estimated_cost`` and was dropped: the pipeline passed a hard-coded
zero into the runtime, priced the request from the catalogue and reserved
credits for that quote without comparing the two, so a cap below the quote
prevented nothing (2026-09-06 audit). Pinned here: the cap is compared with
the server's own quote before any credit is reserved, a refusal charges
nothing and names both numbers, a cap the quote fits under changes nothing,
no cap means no cap, and an automatic retry onto a dearer alternative is held
to the same ceiling instead of quietly spending past it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from entitlement_core import ShotSpendCapExceeded, enforce_shot_spend_cap
from evaluation_core import EvaluationDecision, RetryPlan
from fastapi.testclient import TestClient
from production_domain.models import GenerationCandidate, GenerationJob
from sqlalchemy import select
from video_platform_api.main import create_app

QUOTE_USD = 0.50


def _quote(**_kwargs):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        provider_cost_usd=0.40,
        resolution_multiplier=1.0,
        reference_multiplier=1.0,
        service_multiplier=1.25,
        estimated_total_usd=QUOTE_USD,
        credits=50,
        usd_per_credit=0.01,
        image_count=1,
        pricing_status="VERIFIED",
        pricing_source_url="https://example.test/pricing",
        pricing_checked_at="2026-09-06",
    )


def _compiled_shot(client: TestClient) -> str:
    project = client.post("/v1/projects", json={"title": "Capped", "default_provider": "google_flow"})
    assert project.status_code == 200, project.text
    episode = client.post(
        f"/v1/projects/{project.json()['id']}/episodes",
        json={
            "project_id": project.json()["id"],
            "title": "Pilot",
            "episode_number": 1,
            "script_source": "EXT. HOTEL - NIGHT\nLinJin turns toward the door.",
        },
    )
    assert episode.status_code == 200, episode.text
    compiled = client.post(f"/v1/episodes/{episode.json()['id']}/compile")
    assert compiled.status_code == 200, compiled.text
    return compiled.json()["shot_ids"][0]


def _generate(client: TestClient, shot_id: str, key: str, **body):  # type: ignore[no-untyped-def]
    return client.post(f"/v1/shots/{shot_id}/generate", json={"idempotency_key": key, **body})


def _jobs_and_candidates(container) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        jobs = len(list(session.scalars(select(GenerationJob))))
        candidates = len(list(session.scalars(select(GenerationCandidate))))
    return jobs, candidates


# --------------------------------------------------------------------------
# The route: the cap is enforced before anything is reserved.
# --------------------------------------------------------------------------
def test_a_quote_above_the_cap_is_refused_before_anything_is_reserved(container, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(container.credit_pricing, "estimate", _quote)
    submissions: list[object] = []
    original_submit = container.visual_runtime.submit_autopilot

    def counting_submit(prepared, **kwargs):  # type: ignore[no-untyped-def]
        submissions.append(prepared)
        return original_submit(prepared, **kwargs)

    monkeypatch.setattr(container.visual_runtime, "submit_autopilot", counting_submit)
    with TestClient(create_app(container)) as client:
        shot_id = _compiled_shot(client)
        refused = _generate(client, shot_id, "capped-1", spend_cap_usd=0.25)
        assert refused.status_code == 422, refused.text
        detail = refused.json()["detail"]
        assert "0.5000" in detail and "$0.25" in detail, detail
        assert "nothing was charged" in detail
        assert submissions == [], "the runtime was never asked to submit, so nothing was reserved"
        assert _jobs_and_candidates(container) == (0, 0)
        assert client.get(f"/v1/shots/{shot_id}/candidates").json() == []


def test_a_cap_the_quote_fits_under_changes_nothing(container, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(container.credit_pricing, "estimate", _quote)
    with TestClient(create_app(container)) as client:
        shot_id = _compiled_shot(client)
        accepted = _generate(client, shot_id, "capped-2", spend_cap_usd=0.80)
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["replayed"] is False
        candidates = client.get(f"/v1/shots/{shot_id}/candidates").json()
        assert len(candidates) == 1
        assert candidates[0]["cost"] == pytest.approx(QUOTE_USD)
    with container.database.session() as session:
        job = session.scalar(select(GenerationJob))
        assert job is not None
        # The cap rides on the job so a retry can be held to it.
        assert job.request_json["metadata"]["spend_cap_usd"] == 0.80


@pytest.mark.parametrize("body", [{}, {"spend_cap_usd": 0}, {"spend_cap_usd": None}])
def test_no_cap_means_no_cap(container, monkeypatch, body):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(container.credit_pricing, "estimate", _quote)
    with TestClient(create_app(container)) as client:
        shot_id = _compiled_shot(client)
        accepted = _generate(client, shot_id, "uncapped", **body)
        assert accepted.status_code == 202, accepted.text
    with container.database.session() as session:
        job = session.scalar(select(GenerationJob))
        assert job is not None
        assert "spend_cap_usd" not in job.request_json["metadata"]


def test_the_old_estimate_field_is_not_a_cap(container, monkeypatch):  # type: ignore[no-untyped-def]
    """``estimated_cost`` is the caller's guess and was never a limit; a client
    still sending only that field must not start being refused."""

    monkeypatch.setattr(container.credit_pricing, "estimate", _quote)
    with TestClient(create_app(container)) as client:
        shot_id = _compiled_shot(client)
        accepted = _generate(client, shot_id, "estimate-only", estimated_cost=0.01)
        assert accepted.status_code == 202, accepted.text


def test_a_negative_cap_is_rejected_at_the_door(container):  # type: ignore[no-untyped-def]
    with TestClient(create_app(container)) as client:
        shot_id = _compiled_shot(client)
        assert _generate(client, shot_id, "negative", spend_cap_usd=-1).status_code == 422
        assert _jobs_and_candidates(container) == (0, 0)


# --------------------------------------------------------------------------
# The rule itself, and the retry path that reuses it.
# --------------------------------------------------------------------------
def _admitted(quoted: float, provider: str = "seedance", model: str = "seedance-2.5"):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        request=SimpleNamespace(provider=provider, model=model),
        estimate=SimpleNamespace(estimated_total_usd=quoted),
    )


def test_the_rule_compares_the_quote_with_the_cap() -> None:
    enforce_shot_spend_cap(_admitted(0.50), None)
    enforce_shot_spend_cap(_admitted(0.50), 0)
    enforce_shot_spend_cap(_admitted(0.50), 0.50)
    with pytest.raises(ShotSpendCapExceeded) as refused:
        enforce_shot_spend_cap(_admitted(0.5001, "kling", "kling-3"), 0.50)
    assert refused.value.quoted_usd == pytest.approx(0.5001)
    assert refused.value.cap_usd == 0.50
    assert "kling/kling-3" in str(refused.value)


def test_a_retry_refused_by_the_cap_ends_the_retries_without_failing_the_evaluation(
    container, monkeypatch
):  # type: ignore[no-untyped-def]
    """The evaluation's retry plan names an alternative whose quote is above
    the cap the shot was generated under: no retry job is created, the plan
    comes back terminal with the reason on it, and nothing is raised - so the
    evaluation completes instead of hard-failing the candidate."""

    runtime = container.visual_runtime
    plan = RetryPlan(
        action=EvaluationDecision.SWITCH_MODEL,
        attempt_number=1,
        terminal=False,
        next_provider="kling",
        next_model="kling-3",
        reasons=["identity_failure"],
    )

    def refusing_retry(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise ShotSpendCapExceeded(quoted_usd=0.75, cap_usd=0.40, provider="kling", model="kling-3")

    monkeypatch.setattr(runtime, "_execute_retry", refusing_retry)
    decided, retry_job = runtime._retry_under_cap("job", {}, {"spend_cap_usd": 0.40}, {}, plan)
    assert retry_job is None
    assert decided.terminal is True
    assert decided.reasons == ["identity_failure", "SPEND_CAP_EXCEEDED:0.7500>0.40"]
    assert decided.next_provider == "kling", "what was refused stays on record"

    from production_engine.runtime import _spend_cap

    assert _spend_cap({"spend_cap_usd": 0.40}) == 0.40
    assert _spend_cap({}) is None
    assert _spend_cap({"spend_cap_usd": "0"}) is None
    assert _spend_cap({"spend_cap_usd": "not a number"}) is None
    assert _spend_cap({"spend_cap_usd": True}) is None


def test_the_retry_admission_is_held_to_the_cap(container, monkeypatch):  # type: ignore[no-untyped-def]
    """``_execute_retry`` re-admits the alternative and must refuse it above
    the cap before it reaches the gateway - so the gateway is never called and
    the retry's provisional candidate is cleaned up."""

    monkeypatch.setattr(container.credit_pricing, "estimate", _quote)  # every quote is 0.50
    with TestClient(create_app(container)) as client:
        shot_id = _compiled_shot(client)
        assert _generate(client, shot_id, "first", spend_cap_usd=0.80).status_code == 202
    with container.database.session() as session:
        job = session.scalar(select(GenerationJob))
        assert job is not None
        job_id = job.id
        request = dict(job.request_json)
    metadata = {**dict(request.get("metadata") or {}), "spend_cap_usd": 0.25}
    request["metadata"] = metadata

    runtime = container.visual_runtime
    submitted: list[object] = []
    monkeypatch.setattr(
        runtime, "submit", lambda *args, **kwargs: submitted.append(args) or (None, False)
    )
    plan = RetryPlan(
        action=EvaluationDecision.RETRY_REWRITE_PROMPT,
        attempt_number=1,
        terminal=False,
        prompt_patch="hold the framing",
    )
    with pytest.raises(ShotSpendCapExceeded) as refused:
        runtime._execute_retry(job_id, request, metadata, {}, plan)
    assert refused.value.cap_usd == 0.25 and refused.value.quoted_usd == pytest.approx(QUOTE_USD)
    assert submitted == [], "the gateway was never asked, so nothing was reserved"
    assert _jobs_and_candidates(container) == (1, 1), "the retry's provisional candidate is gone"
