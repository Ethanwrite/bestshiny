from fastapi.testclient import TestClient
from image_prompt_core import ImagePromptCorrector, ImagePromptCorrectRequest
from production_domain.models import PromptRevision
from video_platform_api.main import create_app


def test_corrector_turns_vague_commercial_language_into_visual_controls():
    result = ImagePromptCorrector().correct(ImagePromptCorrectRequest(prompt="一个女生拿着香水，高级一点"))
    assert result.detected_type == "product"
    assert result.corrected_prompt.startswith("画面主体：一个女生拿着香水")
    assert "采用以产品为重点的构图" in result.corrected_prompt
    assert "高级" not in result.corrected_prompt
    assert "8K" not in result.corrected_prompt
    assert "masterpiece" not in result.corrected_prompt


def test_reference_regeneration_locks_every_unrequested_variable():
    result = ImagePromptCorrector().correct(
        ImagePromptCorrectRequest(
            prompt="修改发型为短发",
            reference_assets=["character-v1-front"],
        )
    )
    assert result.identity_preservation_mode is True
    assert result.detected_type == "reference_character_regeneration"
    assert result.editable_variables == ["hairstyle"]
    assert "画面主体：修改发型为短发" in result.corrected_prompt
    assert "只执行原文明确要求的修改" in result.corrected_prompt
    assert "未要求修改 wardrobe，因此保持不变" in result.preserved_constraints
    assert "未要求修改 lighting，因此保持不变" in result.preserved_constraints


def test_product_prompt_preserves_product_invariants():
    result = ImagePromptCorrector().correct(
        ImagePromptCorrectRequest(
            prompt="A frosted glass perfume bottle on a black plinth", task_type="product"
        )
    )
    assert any("product geometry" in constraint for constraint in result.preserved_constraints)
    assert "accurate material response" in result.corrected_prompt


def test_english_prompt_normalizes_prose_but_preserves_exact_quoted_text():
    result = ImagePromptCorrector().correct(
        ImagePromptCorrectRequest(
            prompt='LinJin   raises the phone marked "LUMEN  08"!!',
            task_type="commercial",
        )
    )

    assert result.corrected_prompt.startswith('LinJin raises the phone marked "LUMEN  08";')
    assert '"LUMEN  08"' in result.corrected_prompt


def test_chinese_invariants_remain_chinese_and_verbatim():
    result = ImagePromptCorrector().correct(
        ImagePromptCorrectRequest(prompt="一个女生拿着香水，高级一点，保持瓶身标签和人物发型不变")
    )
    assert "一个女生拿着香水，保持瓶身标签和人物发型不变" in result.corrected_prompt
    assert "preserve this untranslated" not in result.corrected_prompt
    assert "a woman" not in result.corrected_prompt
    assert "逐字保留：保持瓶身标签和人物发型不变" in result.preserved_constraints


def test_chinese_prompt_keeps_text_handedness_gaze_and_negative_constraint_verbatim():
    prompt = (
        "一位黑色短发女生右手拿着红色香水瓶，瓶身文字必须是‘LUMEN 08’，雨夜酒店门口，"
        "人物看向左侧窗户，不要看镜头。"
    )
    result = ImagePromptCorrector().correct(ImagePromptCorrectRequest(prompt=prompt))

    assert result.corrected_prompt.startswith(f"画面主体：{prompt}")
    assert "右手拿着红色香水瓶" in result.corrected_prompt
    assert "瓶身文字必须是‘LUMEN 08’" in result.corrected_prompt
    assert "雨夜酒店门口" in result.corrected_prompt
    assert "人物看向左侧窗户" in result.corrected_prompt
    assert "不要看镜头" in result.corrected_prompt
    assert "preserve this untranslated" not in result.corrected_prompt
    assert "short hairwoman" not in result.corrected_prompt
    assert "逐字保留：一位黑色短发女生右手拿着红色香水瓶" in result.preserved_constraints
    assert "逐字保留：瓶身文字必须是‘LUMEN 08’" in result.preserved_constraints
    assert "逐字保留：人物看向左侧窗户" in result.preserved_constraints
    assert "逐字保留：不要看镜头" in result.preserved_constraints


def test_prompt_correct_api_persists_original_for_undo(container, project):
    with TestClient(create_app(container)) as client:
        response = client.post(
            f"/api/prompt/correct?project_id={project.id}",
            json={"prompt": "一个女生拿着香水，高级一点", "task_type": "auto"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["original_prompt"] == "一个女生拿着香水，高级一点"
        assert body["corrected_prompt"] != body["original_prompt"]
        with container.database.session() as session:
            revision = session.get(PromptRevision, body["revision_id"])
            assert revision.original_prompt == body["original_prompt"]
            assert revision.corrected_prompt == body["corrected_prompt"]
