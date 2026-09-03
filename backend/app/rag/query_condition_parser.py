from __future__ import annotations

import copy
import re
import unicodedata
from functools import lru_cache
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

    used: list[tuple[int, int]] = []
    range_match = _RANGE_AREA_PATTERN.search(query)
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

    bound_match = _BOUND_AREA_PATTERN.search(query)
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

    building.update(_area_effect(normalized, chips))
    return {
        "original_query": query,
        "normalized_query": normalized,
        # Keep semantic retrieval intact; typed conditions narrow candidates
        # before Oracle Text/vector limits instead of replacing the query.
        "semantic_query": query.strip(),
        "metadata_filters": {"building": building},
        "chips": chips,
        "area_tolerance_percent": AREA_TOLERANCE_PERCENT,
    }


def parse_query_conditions(query: str) -> dict[str, Any]:
    """Parse deterministic building conditions without a model/API call."""

    return copy.deepcopy(_parse_cached(query))
