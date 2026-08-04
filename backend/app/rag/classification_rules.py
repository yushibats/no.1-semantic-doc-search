from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import PurePath
from typing import Iterable

from app.rag.document_metadata_models import (
    ClassificationRule,
    ClassificationRuleSetConfig,
    RuleCandidate,
    RuleEvaluation,
    TagDefinition,
)


UPLOAD_PREFIX_PATTERN = re.compile(
    r"^\d{8}_\d{6}_[0-9a-fA-F]{8}_",
)
COMPACT_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<year>19\d{2}|20\d{2})(?P<month>0[1-9]|1[0-2])"
    r"(?P<day>0[1-9]|[12]\d|3[01])(?!\d)"
)
SEPARATED_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<year>19\d{2}|20\d{2})[-_./年]"
    r"(?P<month>0?[1-9]|1[0-2])[-_./月]"
    r"(?P<day>0?[1-9]|[12]\d|3[01])日?(?!\d)"
)


@dataclass(frozen=True)
class CustomerNameNormalization:
    raw: str
    normalized: str
    search_key: str
    version: int = 1


def clean_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = "".join(
        character for character in normalized
        if unicodedata.category(character) not in {"Cc", "Cf"}
    )
    return " ".join(normalized.split())


def normalize_comparable(value: str) -> str:
    return clean_text(value).casefold()


def original_filename_from_object_name(object_name: str) -> str:
    name = PurePath(str(object_name or "")).name
    return UPLOAD_PREFIX_PATTERN.sub("", name)


def normalize_filename(value: str) -> str:
    return normalize_comparable(original_filename_from_object_name(value))


def normalize_customer_name(
    value: str,
    *,
    suffixes: Iterable[str] = (),
    version: int = 1,
) -> CustomerNameNormalization:
    raw = clean_text(value)
    normalized = normalize_comparable(raw)
    search_key = normalized
    normalized_suffixes = sorted(
        {normalize_comparable(item) for item in suffixes if clean_text(item)},
        key=len,
        reverse=True,
    )
    for suffix in normalized_suffixes:
        if suffix and search_key.endswith(suffix) and len(search_key) > len(suffix):
            search_key = search_key[: -len(suffix)].rstrip()
            break
    return CustomerNameNormalization(
        raw=raw,
        normalized=normalized,
        search_key=search_key,
        version=version,
    )


def _explicit_date(
    filename: str, custom_patterns: Iterable[str] = ()
) -> tuple[int, int] | None:
    patterns: list[re.Pattern[str]] = [COMPACT_DATE_PATTERN, SEPARATED_DATE_PATTERN]
    for raw_pattern in custom_patterns:
        if not raw_pattern or len(raw_pattern) > 500:
            continue
        try:
            compiled = re.compile(raw_pattern, re.IGNORECASE)
        except re.error:
            continue
        if not {"year", "month", "day"}.issubset(compiled.groupindex):
            continue
        patterns.append(compiled)
    for pattern in patterns:
        for match in pattern.finditer(filename):
            year = int(match.group("year"))
            month = int(match.group("month"))
            day = int(match.group("day"))
            try:
                date(year, month, day)
            except ValueError:
                continue
            return year, month
    return None


def _matches(rule: ClassificationRule, *, filename: str, extension: str) -> bool:
    condition = rule.condition
    exact = {normalize_comparable(item) for item in condition.filename_exact if clean_text(item)}
    if exact and filename not in exact:
        return False
    extensions = {
        normalize_comparable(item).removeprefix(".")
        for item in condition.extensions
        if clean_text(item)
    }
    if extensions and extension not in extensions:
        return False
    all_terms = [normalize_comparable(item) for item in condition.all_terms if clean_text(item)]
    if all_terms and not all(term in filename for term in all_terms):
        return False
    any_terms = [normalize_comparable(item) for item in condition.any_terms if clean_text(item)]
    if any_terms and not any(term in filename for term in any_terms):
        return False
    excluded = [normalize_comparable(item) for item in condition.exclude_terms if clean_text(item)]
    if any(term in filename for term in excluded):
        return False
    return bool(exact or extensions or all_terms or any_terms)


def evaluate_filename_rules(
    original_filename: str,
    config: ClassificationRuleSetConfig,
    tag_definitions: Iterable[TagDefinition] = (),
) -> RuleEvaluation:
    normalized_filename = normalize_filename(original_filename)
    extension = PurePath(normalized_filename).suffix.casefold().removeprefix(".")
    matching = [
        rule for rule in config.rules
        if rule.enabled and _matches(rule, filename=normalized_filename, extension=extension)
    ]
    matching.sort(key=lambda value: (-value.priority, value.rule_id))
    tags = {item.tag_id: item for item in tag_definitions}
    candidates: list[RuleCandidate] = []
    warnings: list[str] = []

    proposed_by_group: dict[str, list[tuple[int, str, str]]] = {}
    for rule in matching:
        for tag_id in rule.tag_ids:
            definition = tags.get(tag_id)
            if definition is None:
                warnings.append(f"ルール {rule.name} が存在しないタグ {tag_id} を参照しています")
                continue
            group_key = definition.group_id or definition.group_code or tag_id
            proposed_by_group.setdefault(group_key, []).append(
                (rule.priority, tag_id, rule.rule_id)
            )

    for group_key, proposed in proposed_by_group.items():
        definition = tags[proposed[0][1]]
        if definition.selection_mode == "MULTI":
            unique: dict[str, tuple[int, str]] = {}
            for priority, tag_id, rule_id in proposed:
                current = unique.get(tag_id)
                if current is None or priority > current[0]:
                    unique[tag_id] = (priority, rule_id)
            for tag_id, (_, rule_id) in unique.items():
                tag = tags[tag_id]
                candidates.append(
                    RuleCandidate(
                        field_kind="TAG",
                        target_key=tag_id,
                        value_raw=tag.name,
                        value_normalized=tag.code,
                        source="RULE",
                        confidence=1,
                        evidence={"rule_id": rule_id, "filename": original_filename},
                        confirmed=True,
                    )
                )
            continue
        highest = max(value[0] for value in proposed)
        finalists = {(tag_id, rule_id) for priority, tag_id, rule_id in proposed if priority == highest}
        tag_ids = {tag_id for tag_id, _ in finalists}
        if len(tag_ids) != 1:
            warnings.append(f"排他タググループ {group_key} の判定が競合しました")
            for tag_id, rule_id in sorted(finalists):
                tag = tags[tag_id]
                candidates.append(
                    RuleCandidate(
                        field_kind="TAG",
                        target_key=tag_id,
                        value_raw=tag.name,
                        value_normalized=tag.code,
                        source="RULE",
                        confidence=1,
                        evidence={"rule_id": rule_id, "filename": original_filename},
                        ambiguous=True,
                    )
                )
            continue
        tag_id = next(iter(tag_ids))
        rule_id = next(rule_id for candidate, rule_id in finalists if candidate == tag_id)
        tag = tags[tag_id]
        candidates.append(
            RuleCandidate(
                field_kind="TAG",
                target_key=tag_id,
                value_raw=tag.name,
                value_normalized=tag.code,
                source="RULE",
                confidence=1,
                evidence={"rule_id": rule_id, "filename": original_filename},
                confirmed=True,
            )
        )

    explicit_date = _explicit_date(normalized_filename, config.date_patterns)
    if explicit_date:
        year, month = explicit_date
        candidates.append(
            RuleCandidate(
                field_kind="DATE",
                target_key="document_year_month",
                value_raw=f"{year:04d}-{month:02d}",
                value_normalized=f"{year:04d}-{month:02d}",
                source="AUTO_FILENAME",
                confidence=1,
                evidence={"filename": original_filename, "precision": "DAY"},
                confirmed=True,
            )
        )

    for rule in matching:
        if not rule.customer_pattern:
            continue
        try:
            pattern = re.compile(rule.customer_pattern, re.IGNORECASE)
        except re.error:
            warnings.append(f"ルール {rule.name} の顧客名パターンが不正です")
            continue
        match = pattern.search(original_filename_from_object_name(original_filename))
        if not match:
            continue
        value = match.groupdict().get("customer") or match.group(0)
        normalized = normalize_customer_name(
            value,
            suffixes=config.customer_suffixes,
            version=config.normalization_version,
        )
        if not normalized.raw:
            continue
        candidates.append(
            RuleCandidate(
                field_kind="CUSTOMER",
                target_key="customer_name",
                value_raw=normalized.raw,
                value_normalized=normalized.normalized,
                source="AUTO_FILENAME",
                confidence=1,
                evidence={"rule_id": rule.rule_id, "filename": original_filename},
                confirmed=False,
            )
        )
        break

    return RuleEvaluation(
        original_filename=original_filename_from_object_name(original_filename),
        normalized_filename=normalized_filename,
        matched_rule_ids=[item.rule_id for item in matching],
        candidates=candidates,
        warnings=list(dict.fromkeys(warnings)),
    )
