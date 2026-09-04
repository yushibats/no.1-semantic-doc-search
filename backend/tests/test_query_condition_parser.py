from __future__ import annotations

from app.rag.query_condition_parser import parse_query_conditions


def test_parses_building_structure_layout_and_approximate_area() -> None:
    result = parse_query_conditions("RC造マンション80㎡ 3LDK")
    building = result["metadata_filters"]["building"]

    assert building["building_types"] == ["マンション"]
    assert building["structures"] == ["RC造"]
    assert building["layouts"] == ["3LDK"]
    assert building["area_min"] == 72.0
    assert building["area_max"] == 88.0
    assert building["area_unit"] == "㎡"
    assert any("約80㎡" in chip["label"] for chip in result["chips"])


def test_parses_typed_area_range_and_before_after_layouts() -> None:
    result = parse_query_conditions(
        "現況4DKから提案3LDK、専有面積70〜90㎡のマンション"
    )
    building = result["metadata_filters"]["building"]

    assert building["area_types"] == ["専有面積"]
    assert building["area_min"] == 70.0
    assert building["area_max"] == 90.0
    assert building["existing_layouts"] == ["4DK"]
    assert building["proposed_layouts"] == ["3LDK"]


def test_accepts_common_square_metre_notation_and_area_alias() -> None:
    result = parse_query_conditions("木造戸建て 延べ床84.2 m2")
    building = result["metadata_filters"]["building"]

    assert building["building_types"] == ["戸建て"]
    assert building["structures"] == ["木造"]
    assert building["area_types"] == ["延床面積"]
    assert building["area_min"] == 75.78
    assert building["area_max"] == 92.62
    assert building["area_unit"] == "㎡"


def test_cached_parse_results_are_returned_as_independent_values() -> None:
    first = parse_query_conditions("マンション80㎡")
    first["metadata_filters"]["building"]["building_types"].clear()

    second = parse_query_conditions("マンション80㎡")
    assert second["metadata_filters"]["building"]["building_types"] == [
        "マンション"
    ]


def test_parses_room_tatami_with_ten_percent_tolerance() -> None:
    result = parse_query_conditions("LDK 16帖")
    room = result["metadata_filters"]["room"]
    building = result["metadata_filters"]["building"]

    assert room["room_names"] == ["LDK"]
    assert room["tatami_min"] == 14.4
    assert room["tatami_max"] == 17.6
    assert building["area_min"] is None
    assert building["area_max"] is None


def test_parses_japanese_room_tatami_order_with_ten_percent_tolerance() -> None:
    result = parse_query_conditions("80帖のLDK")
    room = result["metadata_filters"]["room"]

    assert room["room_names"] == ["LDK"]
    assert room["tatami_min"] == 72.0
    assert room["tatami_max"] == 88.0


def test_parses_room_area_with_ten_percent_tolerance() -> None:
    result = parse_query_conditions("LDK 25.92㎡")
    room = result["metadata_filters"]["room"]

    assert room["room_names"] == ["LDK"]
    assert room["area_min"] == 23.33
    assert room["area_max"] == 28.51


def test_parses_building_age_and_floor_count() -> None:
    result = parse_query_conditions("築30年の2階建て")
    building = result["metadata_filters"]["building"]

    assert building["building_age_min"] == 27
    assert building["building_age_max"] == 33
    assert building["floor_count_min"] == 2
    assert building["floor_count_max"] == 2
