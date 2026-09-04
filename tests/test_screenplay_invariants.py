"""Invariants, product claims and required copy stop being decoration.

The screenplay carried three kinds of binding material that compilation threw
away: `invariants` (what must stay true), `product_claims` (wording that must
survive verbatim) and `required_copy` (words that must appear on screen).
Nothing read them after the screenplay was written, so a shot prompt could
contradict the invariants, paraphrase a regulated claim, and omit the copy
entirely - with no error anywhere.

Now: global invariants and must-preserve claims are narrative facts (which
makes them part of the context fence a candidate commit re-checks and
unrewritable by a later edit), scoped invariants reach only the shots they
apply to, and copy without a declared shot blocks approval rather than being
dropped into an arbitrary one.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from creative_director_core.schemas import RequiredCopy, ScreenplayInvariant
from creative_director_core.screenplay import shot_constraints, validate_screenplay
from production_domain.models import CreativeAction, NarrativeFact, Shot
from sqlalchemy import select
from test_creative_director import (
    RICH_IDEA,
    SCREENPLAY,
    ScriptedDirector,
    _approve_brief,
    _approve_screenplay,
    _client,
    _complete_visuals,
    _registered_pro,
    _rich_turn,
    _state,
    _wire_openrouter_images,
)
from test_creative_director import (
    openrouter_container as openrouter_container,  # re-exported so the fixture resolves here
)

GLOBAL_INVARIANT = "the phone is not hers"
MIRA_INVARIANT = "Mira never smiles in this piece"
CLAIM = "Aurora Serum absorbs in ten seconds"
COPY = "Only at bestshiny.com"


# ------------------------------------------------------------------ schema
def test_a_plain_string_invariant_still_parses_and_is_global():
    screenplay = validate_screenplay({**copy.deepcopy(SCREENPLAY), "invariants": [GLOBAL_INVARIANT]})
    assert screenplay.invariants == [ScreenplayInvariant(text=GLOBAL_INVARIANT)]
    assert screenplay.invariants[0].is_global
    assert screenplay.invariant_texts == [GLOBAL_INVARIANT]


def test_required_copy_records_where_the_words_appear():
    placed = validate_screenplay(
        {**copy.deepcopy(SCREENPLAY), "required_copy": [{"text": COPY, "beat": 3, "shot": 1}]}
    )
    assert placed.required_copy == [RequiredCopy(text=COPY, beat=3, shot=1)]
    assert placed.unplaced_copy == []
    assert placed.required_copy_texts == [COPY]

    unplaced = validate_screenplay({**copy.deepcopy(SCREENPLAY), "required_copy": [COPY]})
    assert unplaced.unplaced_copy == [RequiredCopy(text=COPY)]


# ------------------------------------------------------------------ scoping
def test_an_invariant_reaches_only_the_shots_it_applies_to():
    content = copy.deepcopy(SCREENPLAY)
    content["invariants"] = [
        GLOBAL_INVARIANT,
        MIRA_INVARIANT,  # names a character, so it scopes to her
        {"text": "the ledge stays wet", "scenes": ["roof"]},
        {"text": "Ren is never seen", "characters": ["Ren"]},
    ]
    screenplay = validate_screenplay(content)
    per_shot = shot_constraints(screenplay)
    assert len(per_shot) == sum(len(beat.shots) for beat in screenplay.beats)

    # Beat 1 is Mira alone on the roof.
    first = per_shot[0]
    assert GLOBAL_INVARIANT in first.invariants
    assert MIRA_INVARIANT in first.invariants
    assert "the ledge stays wet" in first.invariants
    assert "Ren is never seen" not in first.invariants

    # Beat 2 has both characters, so Ren's invariant applies there and only there.
    ren_shot = next(item for item in per_shot if item.beat_sequence == 2)
    assert "Ren is never seen" in ren_shot.invariants


def test_a_must_preserve_claim_reaches_the_shot_that_shows_the_product():
    content = copy.deepcopy(SCREENPLAY)
    content["product_claims"] = [{"claim": CLAIM, "must_preserve": True}]
    content["beats"][0]["shots"][1]["action"]["object"] = "Aurora Serum"
    per_shot = shot_constraints(validate_screenplay(content), product="Aurora Serum")
    with_product = [item for item in per_shot if item.product_claims]
    assert len(with_product) == 1
    assert with_product[0].beat_sequence == 1 and with_product[0].shot_sequence == 2
    assert with_product[0].product_claims == (CLAIM,)


def test_required_copy_lands_on_exactly_the_declared_shot():
    content = copy.deepcopy(SCREENPLAY)
    content["required_copy"] = [{"text": COPY, "beat": 3, "shot": 1}]
    per_shot = shot_constraints(validate_screenplay(content))
    carrying = [item for item in per_shot if item.required_copy]
    assert len(carrying) == 1
    assert (carrying[0].beat_sequence, carrying[0].shot_sequence) == (3, 1)
    assert carrying[0].required_copy == (COPY,)


def test_copy_is_placed_by_the_shots_own_sequence_not_its_position_in_the_list():
    """Nothing renumbers shots per beat, so the list index is not the identity."""

    content = copy.deepcopy(SCREENPLAY)
    # A screenplay that numbers shots continuously across beats - schema-valid,
    # and what a model writing "beat 3, shot 5" means.
    running = 0
    for beat in content["beats"]:
        for shot in beat["shots"]:
            running += 1
            shot["sequence"] = running
    content["required_copy"] = [{"text": COPY, "beat": 3, "shot": running}]
    per_shot = shot_constraints(validate_screenplay(content))
    carrying = [item for item in per_shot if item.required_copy]
    assert len(carrying) == 1, [item.as_json() for item in per_shot]
    assert carrying[0].shot_sequence == running
    assert carrying[0].required_copy == (COPY,)


def test_a_short_cast_name_inside_another_word_does_not_scope_an_invariant():
    """"Al" must not match inside "always" and silently narrow a global rule."""

    from creative_director_core.screenplay import global_invariants

    content = copy.deepcopy(SCREENPLAY)
    content["characters"][1]["name"] = "Al"
    for beat in content["beats"]:
        beat["characters"] = ["Al" if name == "Ren" else name for name in beat["characters"]]
        for shot in beat["shots"]:
            if shot.get("dialogue", {}).get("speaker") == "Ren":
                shot["dialogue"]["speaker"] = "Al"
        for character in content["characters"]:
            for relation in character.get("relationships", []):
                if relation["with"] == "Ren":
                    relation["with"] = "Al"
    content["invariants"] = ["the logo always appears bottom-right", "Al is never seen"]
    screenplay = validate_screenplay(content)
    assert global_invariants(screenplay) == ["the logo always appears bottom-right"]
    per_shot = shot_constraints(screenplay)
    # The global one holds everywhere; the scoped one only where Al is.
    assert all("the logo always appears bottom-right" in item.invariants for item in per_shot)
    assert any("Al is never seen" in item.invariants for item in per_shot)
    assert not all("Al is never seen" in item.invariants for item in per_shot)


# --------------------------------------------------------------- approval
def test_copy_with_nowhere_to_appear_blocks_approval(container, project):
    content = copy.deepcopy(SCREENPLAY)
    content["required_copy"] = [COPY]
    container.creative_director.model_roles = ScriptedDirector(_rich_turn, screenplay=content)
    with _client(container) as client:
        started = client.post(
            "/v1/creative/sessions", json={"project_id": project.id, "idea": RICH_IDEA}
        ).json()
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"])
        view = _state(client, session_id)
        refused = client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/approve",
            json={"revision": view["screenplay"]["revision"]},
        )
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert detail["reason_code"] == "REQUIRED_COPY_UNPLACED"
        assert detail["required_copy"] == [{"text": COPY, "beat": None, "shot": None}]

        # Nothing was derived; the user (or the director) says where it goes.
        after = _state(client, session_id)
        assert after["anchors"] == []

        placed = dict(content)
        placed["required_copy"] = [{"text": COPY, "beat": 3, "shot": 1}]
        edited = client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/edit", json={"content": placed}
        )
        assert edited.status_code == 200, edited.text
        approved = client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/approve",
            json={"revision": edited.json()["revision"]},
        )
        assert approved.status_code == 200, approved.text


# ----------------------------------------------------------- through compile
def _binding_screenplay() -> dict[str, Any]:
    content = copy.deepcopy(SCREENPLAY)
    content["invariants"] = [GLOBAL_INVARIANT, MIRA_INVARIANT]
    content["product_claims"] = [{"claim": CLAIM, "must_preserve": True}]
    content["beats"][0]["shots"][1]["action"]["object"] = "Aurora Serum"
    content["required_copy"] = [{"text": COPY, "beat": 3, "shot": 1}]
    return content


async def _compile_all(container, client, headers, project_id):  # type: ignore[no-untyped-def]
    started = client.post(
        "/v1/creative/sessions", headers=headers, json={"project_id": project_id, "idea": RICH_IDEA}
    ).json()
    session_id = started["session_id"]
    edited = client.post(
        f"/v1/creative/sessions/{session_id}/brief/edit",
        headers=headers,
        json={
            "operations": [
                {
                    "op": "SET",
                    "path": "product.name",
                    "value": "Aurora Serum",
                    "evidence": "brief editor",
                }
            ]
        },
    )
    assert edited.status_code == 200, edited.text
    _approve_brief(client, session_id, edited.json()["revision"], headers)
    _approve_screenplay(client, session_id, headers)
    await _complete_visuals(container, client, session_id, headers)
    bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose", headers=headers).json()
    locked = client.post(
        f"/v1/creative/sessions/{session_id}/bible/approve",
        headers=headers,
        json={"version": bible["version"]},
    )
    assert locked.status_code == 200, locked.text
    client.post(f"/v1/creative/sessions/{session_id}/beats/propose", headers=headers)
    compiled = client.post(
        f"/v1/creative/sessions/{session_id}/beats/approve",
        headers=headers,
        json={"plan_revision": 1},
    )
    assert compiled.status_code == 200, compiled.text
    return session_id, compiled.json()


@pytest.mark.asyncio
async def test_nothing_is_lost_from_screenplay_to_beat_to_shot_to_prompt(openrouter_container):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(
        _rich_turn, screenplay=_binding_screenplay()
    )
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "invariants@example.com")
        session_id, compiled = await _compile_all(container, client, headers, project_id)

    shot_ids = compiled["shot_ids"]
    with container.database.session() as session:
        intents = [dict(session.get(Shot, shot_id).director_intent_json) for shot_id in shot_ids]
        facts = {
            row.fact_key: row.summary
            for row in session.scalars(select(NarrativeFact).where(NarrativeFact.project_id == project_id))
        }
        fact_actions = [
            action.payload_json
            for action in session.scalars(
                select(CreativeAction).where(
                    CreativeAction.session_id == session_id,
                    CreativeAction.kind == "ESTABLISH_FACT",
                )
            )
        ]

    # 1. Global invariants and must-preserve claims are narrative facts.
    summaries = set(facts.values())
    assert GLOBAL_INVARIANT in summaries, facts
    assert CLAIM in summaries, facts
    assert {item["category"] for item in fact_actions} == {"INVARIANT", "PRODUCT_CLAIM"}
    # The character-scoped invariant is NOT a global fact; it rides its shots.
    assert MIRA_INVARIANT not in summaries

    # 2. Every shot carries the invariants that apply to it, and only those.
    assert all(GLOBAL_INVARIANT in intent["invariants"] for intent in intents)
    assert any(MIRA_INVARIANT in intent["invariants"] for intent in intents)

    # 3. The claim rides the shot that shows the product, verbatim.
    with_claim = [intent for intent in intents if intent.get("product_claims")]
    assert len(with_claim) == 1
    assert with_claim[0]["product_claims"] == [CLAIM]

    # 4. The copy rides exactly the shot it was placed in.
    with_copy = [intent for intent in intents if intent.get("required_copy")]
    assert len(with_copy) == 1
    assert with_copy[0]["required_copy"] == [COPY]
    assert (with_copy[0]["beat_sequence"], with_copy[0]["shot_sequence"]) == (3, 1)

    # 5. And all of it reaches the compiled prompt, quoted where wording matters.
    from video_adapter_core.base import canonical_lines

    claim_shot = shot_ids[intents.index(with_claim[0])]
    result = container.video_prompt_compiler.compile(claim_shot)
    assert f'product claim, verbatim and unparaphrased: "{CLAIM}"' in result.spec.constraints
    assert f"invariant that holds here: {GLOBAL_INVARIANT}" in result.spec.constraints
    assert CLAIM in "\n".join(canonical_lines(result.spec, {}))

    copy_shot = shot_ids[intents.index(with_copy[0])]
    copy_result = container.video_prompt_compiler.compile(copy_shot)
    assert (
        f'required on-screen copy, exactly these words: "{COPY}"' in copy_result.spec.constraints
    )


@pytest.mark.asyncio
async def test_a_claim_on_the_ledger_cannot_be_reworded_by_a_later_compile(openrouter_container):
    """Verbatim is enforced by the ledger, not by hoping nobody edits it."""

    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(
        _rich_turn, screenplay=_binding_screenplay()
    )
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "claim-lock@example.com")
        _session_id, _compiled = await _compile_all(container, client, headers, project_id)

    with container.database.session() as session:
        claim_fact = next(
            row
            for row in session.scalars(select(NarrativeFact).where(NarrativeFact.project_id == project_id))
            if row.summary == CLAIM
        )
        fact_key = claim_fact.fact_key
        episode = claim_fact.established_episode

    # Re-establishing the same key with reworded copy is a conflict, not an update.
    with pytest.raises(ValueError):
        container.narrative_ledger.establish_fact(
            project_id,
            fact_key=fact_key,
            summary="Aurora Serum absorbs almost instantly",
            episode=episode,
        )
    with container.database.session() as session:
        unchanged = session.scalar(
            select(NarrativeFact).where(
                NarrativeFact.project_id == project_id, NarrativeFact.fact_key == fact_key
            )
        )
    assert unchanged.summary == CLAIM


def test_a_new_invariant_fact_expires_a_candidate_generated_before_it(
    container, project, account_worker, register_bytes
):
    """Rule 5: the fence a candidate stored is re-checked when it commits.

    Because global invariants and must-preserve claims are narrative facts,
    establishing one after a candidate was generated changes the ledger slice
    the candidate was compiled against - and the commit refuses rather than
    adopting a shot that never saw the constraint.
    """

    from director_production import CandidateNotCommittable
    from narrative_ledger_core import NarrativePosition
    from production_domain.models import Episode, GenerationCandidate

    account_id, _worker = account_worker
    container.flow_affinity.bind_existing(
        local_project_id=project.id,
        provider_account_id=account_id,
        provider_project_id="flow-project-test",
    )
    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title="E1",
            episode_number=1,
            script_source="INT. ROOM - NIGHT\nMira raises the phone.\n",
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    shot_id = container.narrative.compile_episode(episode_id).shot_ids[0]
    candidate, _replayed = container.candidates.create_candidate(
        shot_id, idempotency_key="fence-invariant", enforce_entitlements=False
    )
    with container.database.session() as session:
        stored = dict(session.get(GenerationCandidate, candidate.id).metadata_json)
    assert stored["narrative_context_fence"]["fence"]

    # The screenplay's invariant lands on the ledger after the candidate was made.
    container.narrative_ledger.establish_fact(
        project.id,
        fact_key="creative:test:ep1:invariant:1",
        summary=GLOBAL_INVARIANT,
        episode=1,
    )
    with container.database.session() as session:
        current = container.narrative_ledger.context_fence_in_session(
            session, project.id, position=NarrativePosition(1, 1, 1), shot_id=shot_id
        )
    assert current != stored["narrative_context_fence"]["fence"]

    # And the commit-time check refuses on exactly that mismatch.
    with container.database.session() as session:
        row = session.get(GenerationCandidate, candidate.id)
        shot = session.get(Shot, shot_id)
        with pytest.raises(CandidateNotCommittable) as raised:
            container.candidates._assert_narrative_commitability(session, row, shot)
    assert "narrative context changed after generation" in str(raised.value)
