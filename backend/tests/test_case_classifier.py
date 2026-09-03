from __future__ import annotations

from app.rag.case_classifier import (
    classify_page,
    extract_set_areas,
    extract_set_facts,
)


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


def test_extract_set_areas_keeps_area_type_value_unit_and_evidence() -> None:
    areas = extract_set_areas(
        file_name="現況資料.pdf",
        page_text="専有面積 80.4㎡、延床面積 112.2㎡",
        page_phase="EXISTING",
    )

    by_type = {item.area_type: item for item in areas}
    assert set(by_type) == {"専有面積", "延床面積"}
    assert by_type["専有面積"].value == 80.4
    assert by_type["専有面積"].unit == "㎡"
    assert by_type["専有面積"].phase == "EXISTING"
    assert by_type["専有面積"].source == "RULE"
    assert by_type["専有面積"].confirmed is True
    assert by_type["専有面積"].evidence["matched_text"] == "専有面積 80.4㎡"
    assert "専有面積 80.4㎡" in by_type["専有面積"].evidence["context"]


def test_extract_set_areas_keeps_vlm_only_values_unconfirmed() -> None:
    areas = extract_set_areas(
        file_name="scan.pdf",
        vlm_text="施工対象面積 45㎡",
        page_phase="PROPOSED",
    )

    assert len(areas) == 1
    assert areas[0].area_type == "施工対象面積"
    assert areas[0].value == 45.0
    assert areas[0].source == "VLM"
    assert areas[0].confirmed is False


def test_extract_set_areas_keeps_unlabelled_value_as_unknown_type() -> None:
    areas = extract_set_areas(
        file_name="マンション80m2.pdf",
        page_phase="EXISTING",
    )

    assert len(areas) == 1
    assert areas[0].area_type == "不明"
    assert areas[0].value == 80.0
    assert areas[0].unit == "㎡"
    assert areas[0].source == "RULE"
    assert areas[0].confirmed is True
    assert areas[0].evidence["matched_text"] == "80㎡"
