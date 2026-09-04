from __future__ import annotations

import copy
import re
import unicodedata
from functools import lru_cache
import math
from typing import Any


AREA_TOLERANCE_PERCENT = 10.0

_BUILDING_TYPES: tuple[tuple[str, str], ...] = (
    ("マンション", r"(?:マンション|集合住宅|共同住宅)"),
    ("戸建て", r"(?:一戸建て|戸建て|戸建)"),
)
_STRUCTURES: tuple[tuple[str, str], ...] = (
    ("SRC造", r"(?:SRC造?|鉄骨鉄筋コンクリート造?)"),
    ("RC造", r"(?<!S)(?:RC造?|鉄筋コンクリート造?)"),
    ("S造", r"(?:S造|鉄骨造)"),
    ("木造", r"木造"),
)
_USES: tuple[tuple[str, str], ...] = (
    ("店舗併用住宅", r"店舗併用(?:住宅)?"),
    ("賃貸住宅", r"賃貸住宅"),
    ("事務所", r"(?:事務所|オフィス)"),
    ("店舗", r"店舗"),
    ("住宅", r"(?:専用住宅|住宅)"),
)
_AREA_TYPES: tuple[tuple[str, str], ...] = (
    ("専有面積", r"専有面積"),
    ("延床面積", r"(?:延床面積|延べ床面積|延べ面積|延床|延べ床)"),
    ("建築面積", r"建築面積"),
    ("敷地面積", r"(?:敷地面積|土地面積)"),
    ("施工対象面積", r"(?:施工対象面積|施工面積|改修面積|リフォーム面積|工事対象面積)"),
    ("部屋面積", r"(?:部屋面積|室面積|居室面積)"),
)
_EXISTING_MARKERS = re.compile(r"(?:現況|既存|改修前|リフォーム前|before)", re.IGNORECASE)
_PROPOSED_MARKERS = re.compile(r"(?:提案|計画|改修後|リフォーム後|after)", re.IGNORECASE)
_LAYOUT_PATTERN = re.compile(r"(?<![A-Z0-9])([1-9])\s*(LDK|DK|K)(?![A-Z])", re.IGNORECASE)
_RANGE_AREA_PATTERN = re.compile(
    r"(?P<minimum>\d{1,5}(?:\.\d+)?)\s*"
    r"(?P<unit1>㎡|坪)?\s*(?:〜|～|~|－|-|から)\s*"
    r"(?P<maximum>\d{1,5}(?:\.\d+)?)\s*(?P<unit2>㎡|坪)",
)
_BOUND_AREA_PATTERN = re.compile(
    r"(?P<value>\d{1,5}(?:\.\d+)?)\s*(?P<unit>㎡|坪)\s*"
    r"(?P<bound>以上|超|以下|未満)",
)
_BARE_AREA_PATTERN = re.compile(
    r"(?P<value>\d{1,5}(?:\.\d+)?)\s*(?P<unit>㎡|坪)"
)
_ROOM_NAMES = (
    "ファミリークローク", "ランドリールーム", "ウォークインクローゼット",
    "子供部屋", "主寝室", "リビング", "キッチン", "ダイニング",
    "パントリー", "洗面脱衣室", "脱衣室", "洗面室", "畳コーナー",
    "洋室", "和室", "寝室", "書斎", "納戸", "浴室", "トイレ",
    "WIC", "LDK", "DK", "居間", "ホール",
)
_ROOM_NAME_PATTERN = "|".join(re.escape(value) for value in _ROOM_NAMES)
_ROOM_TATAMI_PATTERNS = (
    re.compile(
        rf"(?P<room>{_ROOM_NAME_PATTERN})\s*(?P<value>\d{{1,3}}(?:\.\d+)?)\s*(?:帖|畳)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<value>\d{{1,3}}(?:\.\d+)?)\s*(?:帖|畳)(?:の)?\s*(?P<room>{_ROOM_NAME_PATTERN})",
        re.IGNORECASE,
    ),
)
_ROOM_AREA_PATTERNS = (
    re.compile(
        rf"(?P<room>{_ROOM_NAME_PATTERN})\s*(?P<value>\d{{1,4}}(?:\.\d+)?)\s*(?P<unit>㎡|坪)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<value>\d{{1,4}}(?:\.\d+)?)\s*(?P<unit>㎡|坪)(?:の)?\s*(?P<room>{_ROOM_NAME_PATTERN})",
        re.IGNORECASE,
    ),
)
_FLOOR_COUNT_PATTERN = re.compile(
    r"(?:地上\s*(?P<ground>[1-9]\d?)\s*階|"
    r"(?P<built>[1-9]\d?)\s*階(?:建て|建))",
    re.IGNORECASE,
)
_BUILDING_AGE_PATTERN = re.compile(r"築\s*(?P<years>\d{1,3})\s*年", re.IGNORECASE)


def _normalize(query: str) -> str:
    value = unicodedata.normalize("NFKC", query or "")
    value = re.sub(r"(?i)m(?:\^?2|²)", "㎡", value)
    value = value.replace("平米", "㎡")
    return re.sub(r"\s+", " ", value).strip()


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _chip(
    chips: list[dict[str, Any]],
    *,
    field: str,
    label: str,
    effect: dict[str, Any],
) -> None:
    key = f"{field}:{label}"
    if any(item["id"] == key for item in chips):
        return
    chips.append({"id": key, "field": field, "label": label, "effect": effect})


def _extract_named_values(
    query: str,
    definitions: tuple[tuple[str, str], ...],
    target: list[str],
    chips: list[dict[str, Any]],
    field: str,
    label_prefix: str,
) -> None:
    for canonical, pattern in definitions:
        if re.search(pattern, query, re.IGNORECASE):
            _append_unique(target, canonical)
            _chip(
                chips,
                field=field,
                label=f"{label_prefix}: {canonical}",
                effect={field: [canonical]},
            )


def _layout_phase(query: str, start: int) -> str:
    prefix = query[max(0, start - 18):start]
    existing = list(_EXISTING_MARKERS.finditer(prefix))
    proposed = list(_PROPOSED_MARKERS.finditer(prefix))
    if existing or proposed:
        existing_pos = existing[-1].start() if existing else -1
        proposed_pos = proposed[-1].start() if proposed else -1
        return "EXISTING" if existing_pos > proposed_pos else "PROPOSED"
    has_existing = bool(_EXISTING_MARKERS.search(query))
    has_proposed = bool(_PROPOSED_MARKERS.search(query))
    if has_existing != has_proposed:
        return "EXISTING" if has_existing else "PROPOSED"
    return "COMMON"


def _span_overlaps(span: tuple[int, int], used: list[tuple[int, int]]) -> bool:
    return any(span[0] < other[1] and other[0] < span[1] for other in used)


def _area_effect(
    query: str,
    chips: list[dict[str, Any]],
    excluded_spans: list[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "area_types": [],
        "area_min": None,
        "area_max": None,
        "area_unit": "㎡",
    }
    for canonical, pattern in _AREA_TYPES:
        if re.search(pattern, query, re.IGNORECASE):
            _append_unique(result["area_types"], canonical)
            _chip(
                chips,
                field="area_types",
                label=f"面積種別: {canonical}",
                effect={"area_types": [canonical]},
            )

    used: list[tuple[int, int]] = list(excluded_spans or [])
    range_match = next(
        (match for match in _RANGE_AREA_PATTERN.finditer(query)
         if not _span_overlaps(match.span(), used)),
        None,
    )
    if range_match:
        minimum = float(range_match.group("minimum"))
        maximum = float(range_match.group("maximum"))
        unit = range_match.group("unit2") or range_match.group("unit1") or "㎡"
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        result.update(area_min=minimum, area_max=maximum, area_unit=unit)
        used.append(range_match.span())
        _chip(
            chips,
            field="area_range",
            label=f"面積: {minimum:g}〜{maximum:g}{unit}",
            effect={"area_min": minimum, "area_max": maximum, "area_unit": unit},
        )
        return result

    bound_match = next(
        (match for match in _BOUND_AREA_PATTERN.finditer(query)
         if not _span_overlaps(match.span(), used)),
        None,
    )
    if bound_match:
        value = float(bound_match.group("value"))
        unit = bound_match.group("unit")
        bound = bound_match.group("bound")
        if bound in {"以上", "超"}:
            result["area_min"] = value
        else:
            result["area_max"] = value
        result["area_unit"] = unit
        used.append(bound_match.span())
        _chip(
            chips,
            field="area_range",
            label=f"面積: {value:g}{unit}{bound}",
            effect={
                "area_min": result["area_min"],
                "area_max": result["area_max"],
                "area_unit": unit,
            },
        )
        return result

    for match in _BARE_AREA_PATTERN.finditer(query):
        if _span_overlaps(match.span(), used):
            continue
        value = float(match.group("value"))
        unit = match.group("unit")
        tolerance = value * AREA_TOLERANCE_PERCENT / 100
        minimum = round(max(0, value - tolerance), 2)
        maximum = round(value + tolerance, 2)
        result.update(area_min=minimum, area_max=maximum, area_unit=unit)
        _chip(
            chips,
            field="area_range",
            label=(
                f"面積: 約{value:g}{unit}"
                f"（{minimum:g}〜{maximum:g}{unit}）"
            ),
            effect={"area_min": minimum, "area_max": maximum, "area_unit": unit},
        )
        break
    return result


def _canonical_room_name(value: str) -> str:
    normalized = value.upper() if value.upper() in {"LDK", "DK", "WIC"} else value
    if normalized == "ウォークインクローゼット":
        return "WIC"
    return normalized


def _numeric_tolerance(value: float) -> tuple[float, float]:
    tolerance = value * AREA_TOLERANCE_PERCENT / 100
    return round(max(0, value - tolerance), 2), round(value + tolerance, 2)


def _extract_room_conditions(
    query: str, chips: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[tuple[int, int]]]:
    room: dict[str, Any] = {
        "room_names": [],
        "phases": [],
        "tatami_min": None,
        "tatami_max": None,
        "area_min": None,
        "area_max": None,
    }
    used: list[tuple[int, int]] = []
    for pattern in _ROOM_TATAMI_PATTERNS:
        for match in pattern.finditer(query):
            if _span_overlaps(match.span(), used):
                continue
            room_name = _canonical_room_name(match.group("room"))
            value = float(match.group("value"))
            minimum, maximum = _numeric_tolerance(value)
            _append_unique(room["room_names"], room_name)
            phase = _layout_phase(query, match.start())
            if phase != "COMMON":
                _append_unique(room["phases"], phase)
            room.update(tatami_min=minimum, tatami_max=maximum)
            used.append(match.span())
            _chip(
                chips,
                field="room_tatami",
                label=f"{room_name}: 約{value:g}帖（{minimum:g}〜{maximum:g}帖）",
                effect={
                    "room_names": [room_name],
                    "phases": room["phases"],
                    "tatami_min": minimum,
                    "tatami_max": maximum,
                },
            )
            return room, used
    for pattern in _ROOM_AREA_PATTERNS:
        for match in pattern.finditer(query):
            if _span_overlaps(match.span(), used):
                continue
            room_name = _canonical_room_name(match.group("room"))
            value = float(match.group("value"))
            unit = match.group("unit")
            value_m2 = round(value * 3.305785, 2) if unit == "坪" else value
            minimum, maximum = _numeric_tolerance(value_m2)
            _append_unique(room["room_names"], room_name)
            phase = _layout_phase(query, match.start())
            if phase != "COMMON":
                _append_unique(room["phases"], phase)
            room.update(area_min=minimum, area_max=maximum)
            used.append(match.span())
            _chip(
                chips,
                field="room_area",
                label=f"{room_name}: 約{value:g}{unit}（±10%）",
                effect={
                    "room_names": [room_name],
                    "phases": room["phases"],
                    "area_min": minimum,
                    "area_max": maximum,
                },
            )
            return room, used
    return room, used


@lru_cache(maxsize=256)
def _parse_cached(query: str) -> dict[str, Any]:
    normalized = _normalize(query)
    building: dict[str, Any] = {
        "building_types": [],
        "structures": [],
        "uses": [],
        "area_types": [],
        "area_min": None,
        "area_max": None,
        "area_unit": "㎡",
        "layouts": [],
        "existing_layouts": [],
        "proposed_layouts": [],
        "floor_count_min": None,
        "floor_count_max": None,
        "building_age_min": None,
        "building_age_max": None,
    }
    chips: list[dict[str, Any]] = []
    _extract_named_values(
        normalized, _BUILDING_TYPES, building["building_types"],
        chips, "building_types", "建物種別",
    )
    _extract_named_values(
        normalized, _STRUCTURES, building["structures"],
        chips, "structures", "構造",
    )
    _extract_named_values(
        normalized, _USES, building["uses"],
        chips, "uses", "用途",
    )

    for match in _LAYOUT_PATTERN.finditer(normalized):
        value = f"{match.group(1)}{match.group(2).upper()}"
        phase = _layout_phase(normalized, match.start())
        if phase == "EXISTING":
            key = "existing_layouts"
            label = f"現況間取り: {value}"
        elif phase == "PROPOSED":
            key = "proposed_layouts"
            label = f"提案間取り: {value}"
        else:
            key = "layouts"
            label = f"間取り: {value}"
        _append_unique(building[key], value)
        _chip(chips, field=key, label=label, effect={key: [value]})

    room, room_spans = _extract_room_conditions(normalized, chips)
    building.update(_area_effect(normalized, chips, room_spans))

    floor_match = _FLOOR_COUNT_PATTERN.search(normalized)
    if floor_match:
        count = int(floor_match.group("ground") or floor_match.group("built"))
        building.update(floor_count_min=count, floor_count_max=count)
        _chip(
            chips,
            field="floor_count",
            label=f"建物階数: {count}階",
            effect={"floor_count_min": count, "floor_count_max": count},
        )
    age_match = _BUILDING_AGE_PATTERN.search(normalized)
    if age_match:
        years = int(age_match.group("years"))
        minimum, maximum = _numeric_tolerance(float(years))
        building.update(
            building_age_min=math.floor(minimum),
            building_age_max=max(math.ceil(maximum), years),
        )
        _chip(
            chips,
            field="building_age",
            label=f"築年数: 約{years}年（±10%）",
            effect={
                "building_age_min": building["building_age_min"],
                "building_age_max": building["building_age_max"],
            },
        )
    return {
        "original_query": query,
        "normalized_query": normalized,
        # Keep semantic retrieval intact; typed conditions narrow candidates
        # before Oracle Text/vector limits instead of replacing the query.
        "semantic_query": query.strip(),
        "metadata_filters": {"building": building, "room": room},
        "chips": chips,
        "area_tolerance_percent": AREA_TOLERANCE_PERCENT,
    }


def parse_query_conditions(query: str) -> dict[str, Any]:
    """Parse deterministic building conditions without a model/API call."""

    return copy.deepcopy(_parse_cached(query))
