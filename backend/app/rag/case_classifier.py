from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.rag.models import VlmExtractionOutput


TATAMI_AREA_M2 = 1.62


@dataclass(frozen=True)
class PageInference:
    content_kind: str = "OTHER"
    phase: str = "UNKNOWN"
    floor_code: str | None = None
    plan_variant: str | None = None
    source: str = "RULE"
    confidence: float = 0.0
    confirmed: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SetFactCandidate:
    fact_code: str
    phase: str
    value_text: str | None = None
    value_number: float | None = None
    unit: str | None = None
    source: str = "RULE"
    confidence: float = 0.0
    confirmed: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SetAreaCandidate:
    area_type: str
    phase: str
    value: float
    unit: str
    source: str = "RULE"
    confidence: float = 0.0
    confirmed: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SetNumericFactCandidate:
    fact_code: str
    phase: str
    value: float
    unit: str
    source: str = "RULE"
    confidence: float = 0.0
    confirmed: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoomMeasurementCandidate:
    phase: str
    room_name: str
    area_m2: float | None
    tatami_equivalent: float | None
    floor_code: str | None = None
    basis: str = "PRINTED_TATAMI"
    source: str = "RULE"
    confidence: float = 0.0
    confirmed: bool = False
    review_status: str = "REVIEW_REQUIRED"
    searchable: bool = True
    evidence: dict[str, Any] = field(default_factory=dict)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip()


def _match_label(patterns: tuple[tuple[str, str], ...], text: str) -> tuple[str, str] | None:
    for label, pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return label, match.group(0)
    return None


def classify_page(
    *,
    file_name: str,
    page_text: str = "",
    vlm_text: str = "",
    tag_codes: set[str] | None = None,
) -> PageInference:
    """Infer comparison metadata, preferring deterministic document/page signals.

    VLM text is only used when filename, confirmed document tags and normalized
    page text cannot decide a field.  Such results remain unconfirmed candidates.
    """

    tags = {value.casefold() for value in (tag_codes or set())}
    file_text = _normalize(file_name)
    page_rule_text = _normalize(page_text)
    rule_text = _normalize(f"{file_name}\n{page_text}")
    ai_text = _normalize(vlm_text)
    evidence: dict[str, Any] = {"file_name": file_name, "matched": []}
    source = "RULE"
    confidence = 0.25
    confirmed = False

    content_patterns = (
        ("FLOOR_PLAN", r"(?:平面図|間取り図|\bPLAN\s*[-_ ]?[A-Z0-9]*\b)"),
        ("SITE_PLAN", r"(?:配置図|敷地図)"),
        ("ELEVATION", r"(?:立面図|展開図)"),
        ("AREA_TABLE", r"(?:面積表|求積表)"),
        ("PERSPECTIVE", r"(?:内観パース|外観パース|パース図|CGパース)"),
        ("PHOTO", r"(?:施工写真|現場写真|完成写真|室内写真|外観写真)"),
    )
    content_kind = "OTHER"
    content_match: tuple[str, str] | None = None
    # A multi-page proposal can contain plans, perspectives and tables while the
    # document itself has a broad tag. Explicit page text therefore wins over
    # document-level tags; tags and filename are deterministic fallbacks.
    content_match = _match_label(content_patterns, page_rule_text)
    if content_match:
        content_kind = content_match[0]
        confidence, confirmed = 0.97, True
    elif "floor_plan" in tags:
        content_kind, content_match = "FLOOR_PLAN", ("FLOOR_PLAN", "tag:floor_plan")
        confidence, confirmed = 0.99, True
    elif "perspective" in tags:
        content_kind, content_match = "PERSPECTIVE", ("PERSPECTIVE", "tag:perspective")
        confidence, confirmed = 0.99, True
    elif "photo" in tags:
        content_kind, content_match = "PHOTO", ("PHOTO", "tag:photo")
        confidence, confirmed = 0.99, True
    else:
        content_match = _match_label(content_patterns, file_text)
        if content_match:
            content_kind = content_match[0]
            confidence, confirmed = 0.94, True
        else:
            content_match = _match_label(content_patterns, ai_text)
            if content_match:
                content_kind = content_match[0]
                source, confidence, confirmed = "VLM", 0.72, False
    if content_match:
        evidence["matched"].append({"field": "content_kind", "text": content_match[1]})

    phase_patterns = (
        ("EXISTING", r"(?:現況|現状|既存|改修前|リフォーム前|\bBEFORE\b)"),
        ("COMPLETED", r"(?:竣工|完成後|施工後|改修後写真)"),
        ("PROPOSED", r"(?:提案|計画|改修案|リフォーム案|\bAFTER\b|\bPLAN\b)"),
    )
    phase = "UNKNOWN"
    phase_match: tuple[str, str] | None = None
    if "existing" in tags:
        phase, phase_match = "EXISTING", ("EXISTING", "tag:existing")
        confidence, confirmed = max(confidence, 0.99), True
    elif "plan" in tags:
        phase, phase_match = "PROPOSED", ("PROPOSED", "tag:plan")
        confidence, confirmed = max(confidence, 0.99), True
    else:
        phase_match = _match_label(phase_patterns, rule_text)
        if phase_match:
            phase = phase_match[0]
            confidence, confirmed = max(confidence, 0.94), True
        else:
            phase_match = _match_label(phase_patterns, ai_text)
            if phase_match:
                phase = phase_match[0]
                source, confidence, confirmed = "VLM", max(confidence, 0.7), False
    if phase_match:
        evidence["matched"].append({"field": "phase", "text": phase_match[1]})

    floor_code: str | None = None
    if "floor_1f" in tags:
        floor_code = "1F"
    elif "floor_2f" in tags:
        floor_code = "2F"
    else:
        floor_match = re.search(
            r"(?<!\d)([1-9])\s*(?:F|階)(?![A-Za-z0-9])|(?:地階|地下)\s*([1-9])",
            rule_text,
            flags=re.IGNORECASE,
        )
        if floor_match:
            floor_code = f"{floor_match.group(1)}F" if floor_match.group(1) else f"B{floor_match.group(2)}F"
        elif ai_text:
            floor_match = re.search(r"(?<!\d)([1-9])\s*(?:F|階)(?![A-Za-z0-9])", ai_text, re.IGNORECASE)
            if floor_match:
                floor_code = f"{floor_match.group(1)}F"
                source, confirmed = "VLM", False
                confidence = min(max(confidence, 0.68), 0.8)
    if floor_code:
        evidence["matched"].append({"field": "floor_code", "text": floor_code})

    variant_match = re.search(r"\bPLAN\s*[-_ ]?([A-Z0-9]+)\b", rule_text, re.IGNORECASE)
    if not variant_match and ai_text:
        variant_match = re.search(r"\bPLAN\s*[-_ ]?([A-Z0-9]+)\b", ai_text, re.IGNORECASE)
        if variant_match:
            source, confirmed = "VLM", False
    plan_variant = variant_match.group(1).upper() if variant_match else None
    if plan_variant:
        evidence["matched"].append({"field": "plan_variant", "text": plan_variant})

    # A page is comparison-ready only when both role axes are explicit.
    if content_kind == "OTHER" or phase == "UNKNOWN":
        confirmed = False
    return PageInference(
        content_kind=content_kind,
        phase=phase,
        floor_code=floor_code,
        plan_variant=plan_variant,
        source=source,
        confidence=round(confidence, 3),
        confirmed=confirmed,
        evidence=evidence,
    )


_AREA_TYPE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("専有面積", r"専有面積"),
    ("延床面積", r"(?:延床面積|延べ床面積|延べ面積|延床|延べ床)"),
    ("建築面積", r"建築面積"),
    ("敷地面積", r"(?:敷地面積|土地面積)"),
    ("施工対象面積", r"(?:施工対象面積|施工面積|改修面積|リフォーム面積|工事対象面積)"),
    ("部屋面積", r"(?:部屋面積|室面積|居室面積)"),
    ("不明", r"(?<![\w一-龥])面積"),
)
_AREA_LABEL_PATTERN = "|".join(f"(?:{pattern})" for _, pattern in _AREA_TYPE_PATTERNS)
_AREA_PATTERN = re.compile(
    rf"(?P<label>{_AREA_LABEL_PATTERN})\s*[:：=]?\s*"
    r"(?P<value>\d{1,5}(?:\.\d+)?)\s*(?P<unit>㎡|m2|m²|平米|坪)",
    re.IGNORECASE,
)
_BARE_AREA_PATTERN = re.compile(
    r"(?P<value>\d{1,5}(?:\.\d+)?)\s*(?P<unit>㎡|m2|m²|平米|坪)",
    re.IGNORECASE,
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
        rf"(?P<room>{_ROOM_NAME_PATTERN})\s*(?P<value>\d{{1,4}}(?:\.\d+)?)\s*(?P<unit>㎡|m2|m²|平米|坪)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<value>\d{{1,4}}(?:\.\d+)?)\s*(?P<unit>㎡|m2|m²|平米|坪)(?:の)?\s*(?P<room>{_ROOM_NAME_PATTERN})",
        re.IGNORECASE,
    ),
)


def _canonical_area_type(label: str) -> str:
    normalized = _normalize(label)
    for area_type, pattern in _AREA_TYPE_PATTERNS:
        if re.fullmatch(pattern, normalized, re.IGNORECASE):
            return area_type
    return "不明"


def _display_area_evidence(value: str) -> str:
    return re.sub(r"(?i)m2", "㎡", value)


def _area_context(text: str, match: re.Match[str], radius: int = 80) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return text[start:end].strip()


def _canonical_room_name(value: str) -> str:
    upper = value.upper()
    if upper in {"LDK", "DK", "WIC"}:
        return upper
    if value == "ウォークインクローゼット":
        return "WIC"
    return value


def _room_measurement_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern in (*_ROOM_TATAMI_PATTERNS, *_ROOM_AREA_PATTERNS):
        spans.extend(match.span() for match in pattern.finditer(text))
    return spans


def _json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for offset, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[offset:])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def extract_room_measurements(
    *,
    file_name: str,
    page_text: str = "",
    vlm_text: str = "",
    page_phase: str = "UNKNOWN",
    floor_code: str | None = None,
) -> list[RoomMeasurementCandidate]:
    """Extract printed and geometric room sizes without hiding estimates.

    Printed values and a single high-confidence rectangle are auto-verified.
    More complex geometry remains visibly review-required but is searchable
    immediately, matching the product's recall-first behavior.
    """

    rule_text = _normalize(f"{file_name}\n{page_text}")
    phase = page_phase if page_phase in {"EXISTING", "PROPOSED", "COMPLETED"} else "COMMON"
    candidates: list[RoomMeasurementCandidate] = []
    seen: set[tuple[str, str, float | None, float | None, str]] = set()

    def append_candidate(candidate: RoomMeasurementCandidate) -> None:
        key = (
            candidate.phase,
            candidate.room_name.casefold(),
            candidate.area_m2,
            candidate.tatami_equivalent,
            candidate.basis,
        )
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)

    for pattern in _ROOM_TATAMI_PATTERNS:
        for match in pattern.finditer(rule_text):
            tatami = round(float(match.group("value")), 2)
            append_candidate(RoomMeasurementCandidate(
                phase=phase,
                room_name=_canonical_room_name(match.group("room")),
                area_m2=round(tatami * TATAMI_AREA_M2, 2),
                tatami_equivalent=tatami,
                floor_code=floor_code,
                basis="PRINTED_TATAMI",
                source="RULE",
                confidence=0.99,
                confirmed=True,
                review_status="AUTO_VERIFIED",
                evidence={
                    "matched_text": match.group(0),
                    "conversion_m2_per_tatami": TATAMI_AREA_M2,
                    "context": _area_context(rule_text, match),
                },
            ))

    for pattern in _ROOM_AREA_PATTERNS:
        for match in pattern.finditer(rule_text):
            value = float(match.group("value"))
            unit = match.group("unit").casefold()
            area_m2 = round(value * 3.305785, 2) if unit == "坪" else round(value, 2)
            append_candidate(RoomMeasurementCandidate(
                phase=phase,
                room_name=_canonical_room_name(match.group("room")),
                area_m2=area_m2,
                tatami_equivalent=round(area_m2 / TATAMI_AREA_M2, 2),
                floor_code=floor_code,
                basis="PRINTED_AREA",
                source="RULE",
                confidence=0.99,
                confirmed=True,
                review_status="AUTO_VERIFIED",
                evidence={
                    "matched_text": match.group(0),
                    "conversion_m2_per_tatami": TATAMI_AREA_M2,
                    "context": _area_context(rule_text, match),
                },
            ))

    for raw in _json_objects(vlm_text):
        if "room_area_estimates" not in raw:
            continue
        try:
            output = VlmExtractionOutput.model_validate(raw)
        except (TypeError, ValueError):
            continue
        for estimate in output.room_area_estimates:
            rectangles = estimate.rectangles
            verified_rectangle = (
                len(rectangles) == 1
                and rectangles[0].operation == "ADD"
                and estimate.confidence >= 0.8
            )
            basis = "VERIFIED_RECTANGLE" if verified_rectangle else "ESTIMATED_L_SHAPE"
            dimensions = [
                {
                    "width_mm": item.width_mm,
                    "depth_mm": item.depth_mm,
                    "operation": item.operation,
                }
                for item in rectangles
            ]
            append_candidate(RoomMeasurementCandidate(
                phase=phase,
                room_name=_canonical_room_name(estimate.room_name),
                area_m2=round(float(estimate.area_m2 or 0), 2) or None,
                tatami_equivalent=(
                    round(float(estimate.tatami_equivalent or 0), 2) or None
                ),
                floor_code=floor_code,
                basis=basis,
                source="VLM",
                confidence=float(estimate.confidence),
                confirmed=verified_rectangle,
                review_status=(
                    "AUTO_VERIFIED" if verified_rectangle else "REVIEW_REQUIRED"
                ),
                searchable=True,
                evidence={
                    "source_locator": estimate.source_locator,
                    "evidence": estimate.evidence,
                    "rectangles": dimensions,
                    "conversion_m2_per_tatami": TATAMI_AREA_M2,
                    "approximate": not verified_rectangle,
                },
            ))
        break
    return candidates


def extract_set_numeric_facts(
    *,
    file_name: str,
    page_text: str = "",
    vlm_text: str = "",
) -> list[SetNumericFactCandidate]:
    """Extract strict numeric building facts from explicit text."""

    rule_text = _normalize(f"{file_name}\n{page_text}")
    ai_text = _normalize(vlm_text)
    candidates: list[SetNumericFactCandidate] = []
    definitions = (
        (
            "FLOOR_COUNT",
            re.compile(r"(?:地上\s*(?P<ground>[1-9]\d?)\s*階|(?P<built>[1-9]\d?)\s*階(?:建て|建))"),
            "階",
        ),
        ("BUILDING_AGE", re.compile(r"築\s*(?P<years>\d{1,3})\s*年"), "年"),
    )
    for fact_code, pattern, unit in definitions:
        match = pattern.search(rule_text)
        source, confidence, confirmed = "RULE", 0.97, True
        if not match:
            match = pattern.search(ai_text)
            source, confidence, confirmed = "VLM", 0.72, False
        if not match:
            continue
        group = (
            match.groupdict().get("ground")
            or match.groupdict().get("built")
            or match.groupdict().get("years")
        )
        candidates.append(SetNumericFactCandidate(
            fact_code=fact_code,
            phase="COMMON",
            value=float(group),
            unit=unit,
            source=source,
            confidence=confidence,
            confirmed=confirmed,
            evidence={"matched_text": match.group(0)},
        ))
    return candidates


def extract_set_areas(
    *,
    file_name: str,
    page_text: str = "",
    vlm_text: str = "",
    page_phase: str = "UNKNOWN",
) -> list[SetAreaCandidate]:
    """Extract every explicitly labelled area while preserving type/value pairing."""

    rule_text = _normalize(f"{file_name}\n{page_text}")
    ai_text = _normalize(vlm_text)
    phase = page_phase if page_phase in {"EXISTING", "PROPOSED", "COMPLETED"} else "COMMON"
    candidates: list[SetAreaCandidate] = []
    seen: set[tuple[str, str, float, str]] = set()

    def append_matches(text: str, *, source: str, confidence: float, confirmed: bool) -> None:
        typed_spans: list[tuple[int, int]] = []
        room_spans = _room_measurement_spans(text)
        for match in _AREA_PATTERN.finditer(text):
            typed_spans.append(match.span())
            area_type = _canonical_area_type(match.group("label"))
            value = float(match.group("value"))
            unit_raw = match.group("unit").casefold()
            unit = "㎡" if unit_raw in {"㎡", "m2", "m²", "平米"} else "坪"
            key = (phase, area_type, value, unit)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(SetAreaCandidate(
                area_type=area_type,
                phase=phase,
                value=value,
                unit=unit,
                source=source,
                confidence=confidence,
                confirmed=confirmed,
                evidence={
                    "matched_text": _display_area_evidence(match.group(0)),
                    "context": _display_area_evidence(_area_context(text, match)),
                },
            ))

        for match in _BARE_AREA_PATTERN.finditer(text):
            if any(match.start() < end and match.end() > start for start, end in typed_spans):
                continue
            if any(match.start() < end and match.end() > start for start, end in room_spans):
                continue
            value = float(match.group("value"))
            unit_raw = match.group("unit").casefold()
            unit = "㎡" if unit_raw in {"㎡", "m2", "m²", "平米"} else "坪"
            key = (phase, "不明", value, unit)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(SetAreaCandidate(
                area_type="不明",
                phase=phase,
                value=value,
                unit=unit,
                source=source,
                confidence=min(confidence, 0.90),
                confirmed=confirmed,
                evidence={
                    "matched_text": _display_area_evidence(match.group(0)),
                    "context": _display_area_evidence(_area_context(text, match)),
                },
            ))

    append_matches(rule_text, source="RULE", confidence=0.96, confirmed=True)
    # VLM is supplementary. Values found only in generated text remain candidates.
    append_matches(ai_text, source="VLM", confidence=0.72, confirmed=False)
    return candidates



def extract_set_facts(
    *,
    file_name: str,
    page_text: str = "",
    vlm_text: str = "",
    page_phase: str = "UNKNOWN",
) -> list[SetFactCandidate]:
    """Extract conservative typed case facts from explicit source text."""

    rule_text = _normalize(f"{file_name}\n{page_text}")
    ai_text = _normalize(vlm_text)
    phase = page_phase if page_phase in {"EXISTING", "PROPOSED", "COMPLETED"} else "COMMON"
    facts: list[SetFactCandidate] = []

    definitions: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
        ("BUILDING_TYPE", (
            ("マンション", r"(?:マンション|共同住宅|集合住宅)"),
            ("戸建て", r"(?:戸建て|戸建住宅|一戸建て)"),
        )),
        ("STRUCTURE", (
            ("SRC造", r"(?:SRC造|鉄骨鉄筋コンクリート造)"),
            ("RC造", r"(?:RC造|鉄筋コンクリート造)"),
            ("S造", r"(?:S造|鉄骨造)"),
            ("木造", r"木造"),
        )),
        ("USE", (
            ("住宅", r"(?:専用住宅|住宅用途|住宅)"),
            ("店舗", r"(?:店舗併用|店舗用途|店舗)"),
            ("事務所", r"(?:事務所用途|オフィス|事務所)"),
        )),
    )
    for fact_code, patterns in definitions:
        match = _match_label(patterns, rule_text)
        source, confidence, confirmed = "RULE", 0.94, True
        if not match:
            match = _match_label(patterns, ai_text)
            source, confidence, confirmed = "VLM", 0.7, False
        if match:
            facts.append(SetFactCandidate(
                fact_code=fact_code,
                phase="COMMON",
                value_text=match[0],
                source=source,
                confidence=confidence,
                confirmed=confirmed,
                evidence={"matched_text": match[1]},
            ))

    layout_pattern = r"(?<!\d)([1-9])\s*(LDK|DK|K)(?![A-Z])"
    layout_match = re.search(layout_pattern, rule_text, re.IGNORECASE)
    layout_source, layout_confidence, layout_confirmed = "RULE", 0.94, True
    if not layout_match:
        layout_match = re.search(layout_pattern, ai_text, re.IGNORECASE)
        layout_source, layout_confidence, layout_confirmed = "VLM", 0.7, False
    if layout_match:
        value = f"{layout_match.group(1)}{layout_match.group(2).upper()}"
        facts.append(SetFactCandidate(
            fact_code="LAYOUT",
            phase=phase,
            value_text=value,
            source=layout_source,
            confidence=layout_confidence,
            confirmed=layout_confirmed,
            evidence={"matched_text": layout_match.group(0)},
        ))

    # Keep one representative legacy pair for the existing comparison UI. The
    # complete, correctly paired list is stored by extract_set_areas().
    areas = extract_set_areas(
        file_name=file_name,
        page_text=page_text,
        vlm_text=vlm_text,
        page_phase=page_phase,
    )
    if areas:
        area = max(areas, key=lambda item: (item.confirmed, item.confidence))
        facts.extend((
            SetFactCandidate(
                fact_code="AREA_TYPE", phase="COMMON", value_text=area.area_type,
                source=area.source, confidence=area.confidence, confirmed=area.confirmed,
                evidence=area.evidence,
            ),
            SetFactCandidate(
                fact_code="AREA_VALUE", phase="COMMON", value_number=area.value, unit=area.unit,
                source=area.source, confidence=area.confidence, confirmed=area.confirmed,
                evidence=area.evidence,
            ),
        ))

    return facts
