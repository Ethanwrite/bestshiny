"""A compile that refuses must leave nothing behind.

`_write_shot_lineage` refuses a shot naming a character with no locked
identity - but it used to be the only place that did, and it runs after the
Episode, its Scenes and its Shots are committed. The refusal rolled back the
lineage rows and nothing else, leaving an orphan Episode that the project's
episode endpoint lists, Director opens, and a user can generate shots from -
on a host where generation spends real money.
"""


from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from test_creative_director import (
    openrouter_container as openrouter_container,  # re-exported so the fixture resolves here
)


def test_the_coverage_helper_names_both_ways_a_character_can_be_uncovered() -> None:
    from creative_director_core.service import _uncovered_character_keys

    anchor_ids = {"character:mira": "anchor-1", "character:jun": "anchor-2", "scene:roof": "a3"}
    identities: dict[str, Any] = {"character:mira": {"identity_version_id": "iv-1"}}

    # `jun` has a key visual but no locked identity; `ada` has neither.
    assert _uncovered_character_keys(
        ["character:mira", "character:jun", "character:ada", "scene:roof"],
        anchor_ids,
        identities,
    ) == ["character:ada", "character:jun"]

    # A scene or prop the user skipped is a recorded decision, never a breach.
    assert _uncovered_character_keys(["scene:roof", "prop:phone"], {}, {}) == []
    assert _uncovered_character_keys(["character:mira"], anchor_ids, identities) == []


@pytest.mark.asyncio
async def test_an_uncovered_character_refuses_before_an_episode_is_created(
    openrouter_container,
) -> None:
    """The orphan-Episode defect.

    `_write_shot_lineage` refuses a shot naming a character with no locked
    identity - but it used to be the *only* place that did, and it runs after
    `compiled_episode_id` is committed and after the orchestrator has created
    the Episode, its Scenes and its Shots. The refusal rolled back the lineage
    rows and nothing else, leaving an Episode that the project's episode
    endpoint lists, Director opens, and a user can generate shots from - shots
    with no lineage row, on a host where generation spends real money.

    Here the identity is removed from the locked bible's lineage before beats
    are approved, so the compile must refuse *and leave no Episode behind*.
    """

    from production_domain.models import CreativeSession, Episode, VisualBibleVersion
    from test_creative_director import (
        RICH_IDEA,
        ScriptedDirector,
        _approve_brief,
        _approve_screenplay,
        _client,
        _complete_visuals,
        _registered_pro,
        _rich_turn,
        _wire_openrouter_images,
    )

    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(
            client, container, "orphan-episode@example.com"
        )
        started = client.post(
            "/v1/creative/sessions",
            headers=headers,
            json={"project_id": project_id, "idea": RICH_IDEA},
        ).json()
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"], headers)
        _approve_screenplay(client, session_id, headers)
        await _complete_visuals(container, client, session_id, headers)
        bible = client.post(
            f"/v1/creative/sessions/{session_id}/bible/propose", headers=headers
        ).json()
        locked = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": bible["version"]},
        )
        assert locked.status_code == 200, locked.text

        # Strip every locked identity: the anchors still exist and are READY,
        # so the shots still name their characters, but nothing is identified.
        with container.database.session() as session:
            bible_row = session.scalar(
                select(VisualBibleVersion)
                .where(VisualBibleVersion.session_id == session_id)
                .order_by(VisualBibleVersion.version.desc())
            )
            lineage = dict(bible_row.lineage_json or {})
            assert lineage.get("identities"), "fixture is meaningless without locked identities"
            lineage["identities"] = {}
            bible_row.lineage_json = lineage
            session.flush()

        beats = client.post(
            f"/v1/creative/sessions/{session_id}/beats/propose", headers=headers
        )
        assert beats.status_code == 200, beats.text
        approved = client.post(
            f"/v1/creative/sessions/{session_id}/beats/approve",
            headers=headers,
            json={"plan_revision": 1},
        )

        assert approved.status_code == 409, approved.text
        assert approved.json()["detail"]["reason_code"] == "CHARACTER_IDENTITY_NOT_COVERED"

        with container.database.session() as session:
            episodes = list(
                session.scalars(select(Episode).where(Episode.project_id == project_id))
            )
            assert episodes == [], "the refusal left an orphan Episode behind"
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id)
            )
            assert row.compiled_episode_id is None
            assert row.status == "BEATS_PROPOSED"
