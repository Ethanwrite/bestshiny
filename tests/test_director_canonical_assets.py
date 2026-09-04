"""Scene, product and prop key visuals are Canon, not decoration.

The visual bible lock promoted only the character anchors (through
CharacterIdentityService) and the style plate (through ProjectStyleService).
The scene, product and prop images the user paid for and approved never became
`Asset` rows, so:

* `FrameAnchorPlanner._scene_asset_id` found no canonical SCENE and every
  RECONSTRUCT_FIRST_FRAME plan downgraded with NO_CANONICAL_SCENE_REFERENCE;
* the product a commerce film is about never reached a shot's reference set or
  its CanonicalShotSpec.

These tests follow a scene plate and a product plate from the anchor grid into
the Asset registry, into the frame-anchor plan, and into the generation request.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from production_domain.models import Asset, AssetVersion, Location, Scene, Shot, VisualBibleVersion
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

PRODUCT = "Aurora Serum"


def _product_screenplay() -> dict[str, Any]:
    content = copy.deepcopy(SCREENPLAY)
    content["product_claims"] = [{"claim": f"{PRODUCT} absorbs in ten seconds", "must_preserve": True}]
    content["treatment"]["premise"] = (
        f"Mira finds a stranger's phone on a rooftop at night, beside a bottle of {PRODUCT}."
    )
    content["beats"][0]["shots"][1]["action"]["object"] = PRODUCT
    return content


async def _locked_session(container, client, headers, project_id, *, product: bool = False):  # type: ignore[no-untyped-def]
    started = client.post(
        "/v1/creative/sessions", headers=headers, json={"project_id": project_id, "idea": RICH_IDEA}
    ).json()
    session_id = started["session_id"]
    revision = started["brief_revision"]
    if product:
        edited = client.post(
            f"/v1/creative/sessions/{session_id}/brief/edit",
            headers=headers,
            json={
                "operations": [
                    {"op": "SET", "path": "product.name", "value": PRODUCT, "evidence": "brief editor"}
                ]
            },
        )
        assert edited.status_code == 200, edited.text
        revision = edited.json()["revision"]
    _approve_brief(client, session_id, revision, headers)
    _approve_screenplay(client, session_id, headers)
    await _complete_visuals(container, client, session_id, headers)
    bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose", headers=headers).json()
    locked = client.post(
        f"/v1/creative/sessions/{session_id}/bible/approve",
        headers=headers,
        json={"version": bible["version"]},
    )
    assert locked.status_code == 200, locked.text
    return session_id, locked.json()


@pytest.mark.asyncio
async def test_locking_the_bible_promotes_scene_product_and_prop_key_visuals(openrouter_container):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(
        _rich_turn, screenplay=_product_screenplay()
    )
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "canon-assets@example.com")
        session_id, locked = await _locked_session(
            container, client, headers, project_id, product=True
        )
        view = _state(client, session_id, headers)

    anchors = {anchor["anchor_key"]: anchor for anchor in view["anchors"]}
    assets = locked["lineage"]["assets"]
    assert "scene:rooftop" in assets, sorted(assets)
    assert "product:aurora serum" in assets, sorted(assets)

    with container.database.session() as session:
        rows = {
            row.asset_type: row
            for row in session.scalars(select(Asset).where(Asset.project_id == project_id))
        }
        assert {"SCENE", "PRODUCT"} <= set(rows), sorted(rows)
        for kind, anchor_key in (("SCENE", "scene:rooftop"), ("PRODUCT", "product:aurora serum")):
            asset = rows[kind]
            assert asset.canonical_metadata["creative_anchor_key"] == anchor_key
            assert asset.canonical_version_id, kind
            version = session.get(AssetVersion, asset.canonical_version_id)
            metadata = version.metadata_json
            # Full lineage on the version: anchor, brief, screenplay, bible, media.
            assert metadata["anchor_id"] == anchors[anchor_key]["id"]
            assert metadata["anchor_version"] == anchors[anchor_key]["version"]
            assert metadata["media_asset_id"] == anchors[anchor_key]["media_asset_id"]
            assert metadata["brief_id"] and metadata["screenplay_id"] and metadata["bible_id"]
            assert version.primary_media_asset_id == anchors[anchor_key]["media_asset_id"]


@pytest.mark.asyncio
async def test_a_skipped_optional_anchor_never_becomes_canon(openrouter_container):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "skip-canon@example.com")
        started = client.post(
            "/v1/creative/sessions", headers=headers, json={"project_id": project_id, "idea": RICH_IDEA}
        ).json()
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"], headers)
        _approve_screenplay(client, session_id, headers)
        view = _state(client, session_id, headers)
        scene_anchor = next(a for a in view["anchors"] if a["anchor_key"] == "scene:rooftop")
        # Its generation failed; the user chooses to go without the plate.
        with container.database.session() as session:
            from production_domain.models import CreativeVisualAnchor

            row = session.get(CreativeVisualAnchor, scene_anchor["id"])
            row.status = "FAILED"
            row.failure_code = "PROVIDER_REFUSED"
        skipped = client.post(
            f"/v1/creative/sessions/{session_id}/visuals/anchors/{scene_anchor['id']}/skip",
            headers=headers,
            json={"reason": "we will shoot the plate ourselves"},
        )
        assert skipped.status_code == 200, skipped.text
        await _complete_visuals(container, client, session_id, headers)
        bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose", headers=headers).json()
        locked = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": bible["version"]},
        )
        assert locked.status_code == 200, locked.text

    assert "scene:rooftop" not in locked.json()["lineage"].get("assets", {})
    with container.database.session() as session:
        scene_assets = list(
            session.scalars(
                select(Asset).where(Asset.project_id == project_id, Asset.asset_type == "SCENE")
            )
        )
    assert scene_assets == []


@pytest.mark.asyncio
async def test_a_changed_scene_plate_appends_a_version_and_never_overwrites(openrouter_container):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "scene-version@example.com")
        session_id, locked = await _locked_session(container, client, headers, project_id)
        first = locked["lineage"]["assets"]["scene:rooftop"]

    # The same anchor, a new image: a second version, promoted, first kept.
    with container.database.session() as session:
        bible = session.scalar(
            select(VisualBibleVersion).where(VisualBibleVersion.session_id == session_id)
        )
        lineage = dict(bible.lineage_json)
        lineage["assets"]["scene:rooftop"]["media_asset_id"] = "changed"
        bible.lineage_json = lineage
    replacement = container.media.register(
        project_id, "REFERENCE", __import__("io").BytesIO(b"second-plate"), filename="p.png",
        mime_type="image/png",
    )[0]
    container.creative_director._lock_supporting_assets(
        session_id,
        project_id,
        first["bible_id"],
        2,
        first["brief_id"],
        first["screenplay_id"],
        [
            {
                "id": first["anchor_id"],
                "anchor_key": "scene:rooftop",
                "version": 2,
                "kind": "SCENE",
                "title": "rooftop",
                "media_asset_id": replacement.id,
                "subject": "rooftop",
            }
        ],
        {"assets": {}},
        None,
    )
    with container.database.session() as session:
        asset = session.get(Asset, first["asset_id"])
        versions = list(
            session.scalars(
                select(AssetVersion)
                .where(AssetVersion.asset_id == asset.id)
                .order_by(AssetVersion.version)
            )
        )
    assert [version.version for version in versions] == [1, 2]
    assert versions[0].primary_media_asset_id == first["media_asset_id"]
    assert versions[1].primary_media_asset_id == replacement.id
    assert asset.canonical_version_id == versions[1].id


@pytest.mark.asyncio
async def test_the_canonical_scene_binds_to_its_location_and_reaches_the_frame_anchor_plan(
    openrouter_container,
):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(
        _rich_turn, screenplay=_product_screenplay()
    )
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "scene-plan@example.com")
        session_id, _locked = await _locked_session(
            container, client, headers, project_id, product=True
        )
        client.post(f"/v1/creative/sessions/{session_id}/beats/propose", headers=headers)
        compiled = client.post(
            f"/v1/creative/sessions/{session_id}/beats/approve",
            headers=headers,
            json={"plan_revision": 1},
        )
        assert compiled.status_code == 200, compiled.text
        shot_ids = compiled.json()["shot_ids"]

    with container.database.session() as session:
        scene_asset = session.scalar(
            select(Asset).where(Asset.project_id == project_id, Asset.asset_type == "SCENE")
        )
        location = session.scalar(select(Location).where(Location.project_id == project_id))
        shot = session.get(Shot, shot_ids[0])
        scene = session.get(Scene, shot.scene_id)
    # The plate is bound to the very Location the compiler minted.
    assert scene_asset.canonical_metadata["location_id"] == location.id
    assert scene.location_id == location.id

    # Which is exactly the lookup the frame-anchor planner performs.
    planner = container.orchestrator.frame_anchors
    with container.database.session() as session:
        resolved = planner._scene_asset_id(session, project_id, scene.id)
    assert resolved == scene_asset.id


@pytest.mark.asyncio
async def test_the_product_plate_reaches_the_shot_spec_and_its_reference_set(openrouter_container):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(
        _rich_turn, screenplay=_product_screenplay()
    )
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "product-spec@example.com")
        session_id, locked = await _locked_session(
            container, client, headers, project_id, product=True
        )
        client.post(f"/v1/creative/sessions/{session_id}/beats/propose", headers=headers)
        compiled = client.post(
            f"/v1/creative/sessions/{session_id}/beats/approve",
            headers=headers,
            json={"plan_revision": 1},
        )
        assert compiled.status_code == 200, compiled.text
        shot_ids = compiled.json()["shot_ids"]

    product = locked["lineage"]["assets"]["product:aurora serum"]
    canonical_assets, canonical_media = container.visual_runtime._canonical_assets(project_id)
    kinds = {asset["type"] for asset in canonical_assets}
    assert {"SCENE", "PRODUCT"} <= kinds, kinds
    # The product's key visual is in the project's canonical reference media.
    assert product["media_asset_id"] in canonical_media

    result = container.video_prompt_compiler.compile(shot_ids[0], canonical_assets=canonical_assets)
    product_props = [prop for prop in result.spec.props if prop.get("kind") == "PRODUCT"]
    assert product_props, result.spec.props
    assert product_props[0]["asset_id"] == product["asset_id"]
    assert product_props[0]["asset_version_id"] == product["asset_version_id"]

    # And the shot's own director intent bound it as a reference too.
    with container.database.session() as session:
        intent = dict(session.get(Shot, shot_ids[1]).director_intent_json)
    assert "product:aurora serum" in intent["anchors"], intent
    assert product["media_asset_id"] in intent["reference_asset_ids"]
