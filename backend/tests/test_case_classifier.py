from __future__ import annotations

from app.rag.case_classifier import classify_page, extract_set_facts


def test_classify_page_uses_explicit_filename_and_tags() -> None:
    page = classify_page(
        file_name="03.PLAN-A-1F平面図.pdf",
        page_text="1階 平面図",
        tag_codes={"floor_plan", "plan", "floor_1f"},
    )
    assert page.content_kind == "FLOOR_PLAN"
    assert page.phase == "PROPOSED"
    assert page.floor_code == "1F"
    assert page.plan_variant == "A"
    assert page.confirmed is True
    assert page.source == "RULE"


def test_classify_page_keeps_vlm_only_role_unconfirmed() -> None:
    page = classify_page(
        file_name="scan.pdf",
        vlm_text="これは現況の1階平面図です",
    )
    assert page.content_kind == "FLOOR_PLAN"
    assert page.phase == "EXISTING"
    assert page.floor_code == "1F"
    assert page.confirmed is False
    assert page.source == "VLM"


def test_extract_set_facts_keeps_phase_and_explicit_values() -> None:
    facts = extract_set_facts(
        file_name="提案図.pdf",
        page_text="RC造マンション 専有面積 80.5㎡ 2LDK",
        page_phase="PROPOSED",
    )
    by_key = {(item.fact_code, item.phase): item for item in facts}
    assert by_key[("BUILDING_TYPE", "COMMON")].value_text == "マンション"
    assert by_key[("STRUCTURE", "COMMON")].value_text == "RC造"
    assert by_key[("LAYOUT", "PROPOSED")].value_text == "2LDK"
    assert by_key[("AREA_TYPE", "COMMON")].value_text == "専有面積"
    assert by_key[("AREA_VALUE", "COMMON")].value_number == 80.5
    assert all(item.confirmed for item in facts)


def test_explicit_page_type_overrides_broad_document_tag() -> None:
    page = classify_page(
        file_name="住宅提案資料.pdf",
        page_text="内観パース LDKイメージ",
        tag_codes={"floor_plan", "plan"},
    )
    assert page.content_kind == "PERSPECTIVE"
    assert page.phase == "PROPOSED"
    assert page.confirmed is True
    assert page.source == "RULE"
