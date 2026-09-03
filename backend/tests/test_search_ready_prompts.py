from app.rag.profile_prompt_migration import SEARCH_READY_PROFILE_UPDATES
from app.rag.profile_prompts import (
    CONSTRUCTION_PHOTO_PROFILE_PROMPT,
    PRESENTATION_DESIGN_PROFILE_PROMPT,
    PROPOSAL_PLAN_PROFILE_PROMPT,
    SEARCH_CONCEPT_EXTRACTION_PROMPT,
)
from app.rag.vlm_prompting import INDEX_OUTPUT_CONTRACT


def test_all_three_profiles_are_in_search_ready_update_set() -> None:
    assert set(SEARCH_READY_PROFILE_UPDATES) == {1, 2, 3}
    assert SEARCH_READY_PROFILE_UPDATES[1][1] == CONSTRUCTION_PHOTO_PROFILE_PROMPT
    assert SEARCH_READY_PROFILE_UPDATES[2][1] == PROPOSAL_PLAN_PROFILE_PROMPT
    assert SEARCH_READY_PROFILE_UPDATES[3][1] == PRESENTATION_DESIGN_PROFILE_PROMPT


def test_search_concept_prompt_covers_requested_search_vocabulary() -> None:
    for phrase in (
        "室内窓",
        "間接照明",
        "クラシック",
        "古民家風",
        "防音室",
        "車椅子対応",
        "二世帯住宅",
        "ペット対応",
        "水回り収納増設",
        "間仕切りで可変的な空間",
        "開放感あふれる住まい",
        "安心安全な暮らし",
    ):
        assert phrase in SEARCH_CONCEPT_EXTRACTION_PROMPT


def test_profiles_preserve_searchable_typed_building_evidence() -> None:
    for prompt in (
        CONSTRUCTION_PHOTO_PROFILE_PROMPT,
        PROPOSAL_PLAN_PROFILE_PROMPT,
        PRESENTATION_DESIGN_PROFILE_PROMPT,
    ):
        assert "建物種別: マンション" in prompt
        assert "構造: RC造" in prompt
        assert "用途: 住宅" in prompt
        assert "専有面積: 80.4㎡" in prompt or "専有面積: 80.42㎡" in prompt

    for phrase in (
        "専有面積",
        "延床面積",
        "建築面積",
        "敷地面積",
        "施工対象面積",
        "部屋面積",
        "面積（種別不明）",
        "現況間取り",
        "提案間取り",
    ):
        assert phrase in PROPOSAL_PLAN_PROFILE_PROMPT


def test_presentation_prompt_pairs_labels_with_values_and_ignores_repeat_headers() -> None:
    assert "ページ上部の反復ヘッダーと本文中のラベルを区別" in PRESENTATION_DESIGN_PROFILE_PROMPT
    assert "提案コンセプト: 二人で安心して暮らせる住まい" in PRESENTATION_DESIGN_PROFILE_PROMPT
    assert "現況課題: 浴室が狭い" in PRESENTATION_DESIGN_PROFILE_PROMPT
    assert "提案内容: 洗面室を広げる" in PRESENTATION_DESIGN_PROFILE_PROMPT


def test_global_contract_requires_area_type_and_page_evidence() -> None:
    assert "面積（種別不明）" in INDEX_OUTPUT_CONTRACT
    assert "見出し・表・注記と照合" in INDEX_OUTPUT_CONTRACT
    assert "ページ外の情報を補わない" in INDEX_OUTPUT_CONTRACT
