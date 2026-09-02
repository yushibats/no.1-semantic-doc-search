from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


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
            r"(?<!\d)([1-9])\s*(?:F|階)(?!\w)|(?:地階|地下)\s*([1-9])",
            rule_text,
            flags=re.IGNORECASE,
        )
        if floor_match:
            floor_code = f"{floor_match.group(1)}F" if floor_match.group(1) else f"B{floor_match.group(2)}F"
        elif ai_text:
            floor_match = re.search(r"(?<!\d)([1-9])\s*(?:F|階)(?!\w)", ai_text, re.IGNORECASE)
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

    area_pattern = re.compile(
        r"(専有面積|延床面積|延べ床面積|床面積|施工面積)\s*[:：]?\s*"
        r"(\d+(?:\.\d+)?)\s*(㎡|m2|m²|平米|坪)",
        re.IGNORECASE,
    )
    area_match = area_pattern.search(rule_text)
    area_source, area_confidence, area_confirmed = "RULE", 0.96, True
    if not area_match:
        area_match = area_pattern.search(ai_text)
        area_source, area_confidence, area_confirmed = "VLM", 0.72, False
    if area_match:
        unit = "㎡" if area_match.group(3).casefold() in {"㎡", "m2", "m²", "平米"} else "坪"
        facts.extend((
            SetFactCandidate(
                fact_code="AREA_TYPE", phase="COMMON", value_text=area_match.group(1),
                source=area_source, confidence=area_confidence, confirmed=area_confirmed,
                evidence={"matched_text": area_match.group(0)},
            ),
            SetFactCandidate(
                fact_code="AREA_VALUE", phase="COMMON", value_number=float(area_match.group(2)), unit=unit,
                source=area_source, confidence=area_confidence, confirmed=area_confirmed,
                evidence={"matched_text": area_match.group(0)},
            ),
        ))

    return facts
