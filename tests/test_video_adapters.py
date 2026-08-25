from video_adapter_core import AdapterInput, VideoAdapterRegistry

SHOT = {
    "intent": "A tense profile ending",
    "subjects": [
        {
            "name": "Lin Jin",
            "asset_version_id": "lin:v1",
            "screen_position": "left",
            "body_orientation": "profile-right",
            "eyeline_target": "Zhao Kai",
        }
    ],
    "start_state": {"position": "door"},
    "action": "Lin Jin turns once toward Zhao Kai",
    "blocking": {"path": "one step screen-right"},
    "camera": {"movement": "slow push", "path": "straight", "speed": "slow"},
    "lighting": {"key": "camera-left"},
    "end_state": {"orientation": "profile-right"},
    "duration": 8,
    "resolution": "1080p",
    "constraints": ["no camera gaze"],
}
CONTEXT = {
    "canonical_asset_ids": ["lin:v1"],
    "reference_images": ["asset-front", "asset-profile"],
    "reference_videos": ["movement-reference"],
    "previous_final_frame_asset_id": "frame-01",
    "end_frame": "frame-02",
    "assembled_text": "CURRENT_TEMPORAL_STATE: wardrobe remains blue",
}


def test_adapters_generate_distinct_provider_payloads():
    registry = VideoAdapterRegistry()
    value = AdapterInput(shot=SHOT, context=CONTEXT)
    kling = registry.get("kling").compile("kling-3.0", value)
    veo = registry.get("veo").compile("veo-3.1-quality", value)
    seedance = registry.get("seedance").compile("doubao-seedance-2-5-260628", value)
    assert kling.payload["tail_image_url"] == "frame-02"
    assert veo.payload["last_frame"] == "frame-02"
    assert seedance.payload["first_frame_image"] == "frame-01"
    assert seedance.payload["reference_video"] == "movement-reference"
    assert "wardrobe remains blue" in seedance.prompt
    assert len({kling.prompt, veo.prompt, seedance.prompt}) == 3


def test_grok_adapter_injects_end_gaze_repair_language():
    result = (
        VideoAdapterRegistry().get("grok").compile("grok-video", AdapterInput(shot=SHOT, context=CONTEXT))
    )
    assert "Never acknowledge or look into the camera" in result.prompt
    assert "preserve the approved body orientation" in result.prompt


def test_grok_adapter_preserves_explicitly_approved_camera_gaze():
    shot = {**SHOT, "allow_camera_gaze": True}
    result = (
        VideoAdapterRegistry().get("grok").compile("grok-video", AdapterInput(shot=shot, context=CONTEXT))
    )
    assert "explicitly approved camera eyeline" in result.prompt
    assert "Never acknowledge or look into the camera" not in result.prompt


def test_adapter_keeps_assets_out_of_opaque_prompt_only_contract():
    result = VideoAdapterRegistry().get("wan").compile("wan-2.7", AdapterInput(shot=SHOT, context=CONTEXT))
    assert result.asset_bindings == ["lin:v1", "asset-front", "asset-profile"]
    assert result.payload["first_frame"] == "frame-01"
    assert result.payload["last_frame"] == "frame-02"


def test_the_wan_compiler_never_sends_the_shots_audio_design_as_a_parameter():
    """Wan 2.7 has no generate-audio switch, and no `parameters.audio` at all.

    This compiler passed `shot.audio` — a dict describing the audio *design* —
    straight into the payload, and the provider posted it as `parameters.audio`,
    so every Wan request this platform built carried `"audio": {}`. Wan's audio
    inputs are three different URLs on three different modes, and the compiler
    now passes those through instead when the context resolves them.
    """

    shot = {**SHOT, "audio": {"dialogue": "none", "ambience": "room tone"}}
    result = VideoAdapterRegistry().get("wan").compile(
        "wan-2.7", AdapterInput(shot=shot, context=CONTEXT)
    )
    assert "audio" not in result.payload
    assert result.payload["audio_url"] is None
    assert result.payload["driving_audio"] is None
    assert result.payload["reference_voice"] is None
    # The design itself is not lost; it is in the prompt, where it belongs.
    assert "room tone" in result.prompt

    carried = VideoAdapterRegistry().get("wan").compile(
        "wan-2.7",
        AdapterInput(
            shot=shot,
            context={**CONTEXT, "driving_audio": "https://media.invalid/take.mp3"},
        ),
    )
    assert carried.payload["driving_audio"] == "https://media.invalid/take.mp3"
