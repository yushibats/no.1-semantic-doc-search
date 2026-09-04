from __future__ import annotations

import asyncio
import json
import os
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.rag.classification_rules import evaluate_filename_rules, normalize_customer_name
from app.rag.clients import vlm_client
from app.rag.document_metadata_models import (
    ClassificationRuleSetConfig,
    RuleCandidate,
    RuleEvaluation,
    TagDefinition,
)
from app.rag.index_pipeline import (
    PageExtraction,
    _convert_file_to_images_worker,
    _native_pages,
    _run_ocr,
)
from app.rag.service_settings import retrieval_service_settings
from app.services.oci_service import oci_service


class _LlmTagCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag_id: str = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(min_length=1, max_length=2000)


class _LlmCustomerCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=400)
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(min_length=1, max_length=2000)


class _LlmDateCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    year: int = Field(ge=1000, le=9999)
    month: int = Field(ge=1, le=12)
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(min_length=1, max_length=2000)


class _LlmClassificationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tags: list[_LlmTagCandidate] = Field(default_factory=list, max_length=100)
    customer: _LlmCustomerCandidate | None = None
    document_date: _LlmDateCandidate | None = None


class DraftClassificationResult(BaseModel):
    rule_result: RuleEvaluation
    llm_candidates: list[RuleCandidate] = Field(default_factory=list)
    raw_llm_result: dict[str, Any] = Field(default_factory=dict)
    preview: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    degradations: list[str] = Field(default_factory=list)


_RASTER_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"}


def _timeout_seconds(name: str, default: float) -> float:
    try:
        return max(float(os.getenv(name, str(default))), 0.1)
    except (TypeError, ValueError):
        return default


def _environment_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _preview_payload(
    selected_native: dict[int, str],
    images: list[tuple[bytes, str]],
    ocr_engines: list[str],
    *,
    ocr_skipped: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pages": sorted(selected_native),
        "text_length": len("\n\n".join(selected_native.values())),
        "image_count": len(images),
        "ocr_engines": list(dict.fromkeys(ocr_engines)),
    }
    if ocr_skipped:
        payload["ocr_skipped"] = ocr_skipped
    return payload


def classify_filename(
    filename: str,
    *,
    config: ClassificationRuleSetConfig,
    tags: list[TagDefinition],
) -> RuleEvaluation:
    return evaluate_filename_rules(filename, config, tags)


def _photo_tag_guidance(tags: list[TagDefinition]) -> str:
    if not any(tag.active and tag.code == "photo" for tag in tags):
        return ""
    return (
        "文書種別の「写真」は、建物・室内・設備・現場など実世界をカメラで"
        "撮影した画像にだけ使用してください。CG・パース画像・図面・スキャン文書は"
        "「写真」にしないでください。判別できなければ候補を返さないでください。\n"
    )


def _proposal_material_tag_guidance(tags: list[TagDefinition]) -> str:
    if not any(tag.active and tag.code == "proposal_material" for tag in tags):
        return ""
    return (
        "文書種別の「提案資料」は、顧客向け提案書やプレゼン資料など、説明・計画・"
        "図面・CG等を複数ページにまとめた資料に使用してください。個別の平面図、"
        "パース画像、現場写真には、それぞれの専用文書種別を使用してください。"
        "表紙や本文に提案書・ご提案・プレゼン等の根拠がなければ候補を返さないでください。\n"
    )


async def classify_document_preview(
    *,
    filename: str,
    content: bytes,
    media_type: str,
    config: ClassificationRuleSetConfig,
    tags: list[TagDefinition],
) -> DraftClassificationResult:
    rule_result = classify_filename(filename, config=config, tags=tags)
    extension = PurePosixPath(filename).suffix.casefold().removeprefix(".")
    warnings = list(rule_result.warnings)
    degradations: list[str] = []
    try:
        native_pages = await asyncio.wait_for(
            asyncio.to_thread(_native_pages, content, extension),
            timeout=_timeout_seconds("DRAFT_PREVIEW_NATIVE_TIMEOUT_SECONDS", 8),
        )
    except TimeoutError:
        native_pages = {}
        message = "ネイティブテキスト抽出が時間切れになったため省略しました"
        warnings.append(message)
        degradations.append(message)
    except Exception as error:
        native_pages = {}
        message = f"ネイティブテキスト抽出を利用できませんでした: {error}"
        warnings.append(message)
        degradations.append(message)
    selected_native = {
        page: text[: config.preview_text_limit]
        for page, text in sorted(native_pages.items())[: config.preview_page_limit]
    }
    total_text = "\n\n".join(selected_native.values())[: config.preview_text_limit]

    images: list[tuple[bytes, str]] = []
    try:
        success, rendered, render_error = await asyncio.wait_for(
            asyncio.to_thread(
                _convert_file_to_images_worker,
                content,
                extension,
                filename,
                160,
                1,
                config.preview_page_limit,
            ),
            timeout=_timeout_seconds("DRAFT_PREVIEW_RENDER_TIMEOUT_SECONDS", 15),
        )
    except TimeoutError:
        success, rendered, render_error = False, [], "ページ画像生成が時間切れになりました"
    except Exception as error:
        success, rendered, render_error = False, [], str(error)
    if success:
        images = [(image, "image/png") for _, image in rendered[: config.preview_page_limit]]
    elif not total_text:
        warnings.append(f"先行ページ画像を生成できませんでした: {render_error}")

    ocr_engines: list[str] = []
    ocr_skipped: str | None = None
    should_run_ocr = (
        len(total_text.strip()) < 200
        and bool(images)
        and (
            extension not in _RASTER_EXTENSIONS
            or _environment_flag("DRAFT_PREVIEW_OCR_RASTER_IMAGES", False)
        )
    )
    if extension in _RASTER_EXTENSIONS and len(total_text.strip()) < 200 and images:
        ocr_skipped = "RASTER_IMAGE_FAST_PATH"
    if should_run_ocr:
        ocr_settings = retrieval_service_settings.get_ocr(mask_secrets=False)
        if ocr_settings.enabled:
            pages = [
                PageExtraction(page_number=index, image=image, image_dpi=160)
                for index, (image, _) in enumerate(images, start=1)
            ]
            degraded: list[str] = []
            async def run_preview_ocr() -> None:
                for page in pages:
                    await _run_ocr(page, degraded)

            try:
                await asyncio.wait_for(
                    run_preview_ocr(),
                    timeout=_timeout_seconds("DRAFT_PREVIEW_OCR_TIMEOUT_SECONDS", 20),
                )
            except TimeoutError:
                degraded.append("OCRが時間切れになりました")
            except Exception as error:
                degraded.append(str(error))
            for page in pages:
                if page.ocr_blocks:
                    selected_native[page.page_number] = "\n\n".join(
                        block.text for block in page.ocr_blocks if block.text
                    )[: config.preview_text_limit]
                if page.ocr_engine:
                    ocr_engines.append(page.ocr_engine)
            if degraded:
                messages = [f"先行OCRを利用できませんでした: {item}" for item in degraded]
                warnings.extend(messages)
                degradations.extend(messages)
            total_text = "\n\n".join(selected_native.values())[: config.preview_text_limit]

    enterprise = oci_service.get_enterprise_ai_settings()
    if not enterprise.base_url or not enterprise.api_key or not enterprise.model:
        warnings.append("Enterprise AI VLMが未設定のため、LLM候補化をスキップしました")
        return DraftClassificationResult(
            rule_result=rule_result,
            raw_llm_result={"skipped": True, "reason": "VLM_NOT_CONFIGURED"},
            preview=_preview_payload(
                selected_native, images, ocr_engines, ocr_skipped=ocr_skipped
            ),
            warnings=warnings,
            degradations=degradations,
        )

    tag_by_id = {tag.tag_id: tag for tag in tags if tag.active}
    resolved_tag_ids = {
        str(candidate.target_key)
        for candidate in rule_result.candidates
        if candidate.field_kind == "TAG"
        and candidate.confirmed
        and not candidate.ambiguous
        and candidate.target_key in tag_by_id
    }
    resolved_groups = {
        tag_by_id[candidate.target_key].group_id
        for candidate in rule_result.candidates
        if candidate.field_kind == "TAG"
        and candidate.confirmed
        and not candidate.ambiguous
        and candidate.target_key in tag_by_id
        and tag_by_id[candidate.target_key].selection_mode == "SINGLE"
    }
    allowed_tags = [
        {
            "tag_id": tag.tag_id,
            "code": tag.code,
            "name": tag.name,
            "group": tag.group_name,
            "selection_mode": tag.selection_mode,
        }
        for tag in tags
        if tag.active
        and tag.group_id not in resolved_groups
        and tag.tag_id not in resolved_tag_ids
    ]
    customer_resolved = any(
        candidate.field_kind == "CUSTOMER" for candidate in rule_result.candidates
    )
    date_resolved = any(
        candidate.field_kind == "DATE" and candidate.confirmed
        for candidate in rule_result.candidates
    )
    prompt = (
        f"{config.llm_prompt}\n\n"
        "出力JSONは tags, customer, document_date の3キーだけを使用してください。\n"
        "tagsは tag_id, confidence, evidence。提示されていないtag_idは禁止です。\n"
        f"{_photo_tag_guidance(tags)}"
        f"{_proposal_material_tag_guidance(tags)}"
        "customerは value, confidence, evidence。不明ならnullです。\n"
        "document_dateは year, month, confidence, evidence。不明ならnullです。\n"
        "推測で補完せず、ページまたはファイル名に根拠がある値だけを返してください。\n\n"
        f"ファイル名: {filename}\n"
        f"未決定タグ候補: {json.dumps(allowed_tags, ensure_ascii=False)}\n"
        f"顧客名は既に候補あり: {str(customer_resolved).lower()}\n"
        f"年月は既に確定: {str(date_resolved).lower()}\n"
        f"先頭ページ抽出テキスト:\n{total_text}"
    )
    try:
        raw = await asyncio.wait_for(
            vlm_client.generate_json(prompt=prompt, images=images),
            timeout=_timeout_seconds("DRAFT_PREVIEW_VLM_TIMEOUT_SECONDS", 20),
        )
        output = _LlmClassificationOutput.model_validate(raw)
    except TimeoutError:
        message = "内容AI解析が時間切れになったため、ファイル名解析の候補だけを使用します"
        warnings.append(message)
        degradations.append(message)
        return DraftClassificationResult(
            rule_result=rule_result,
            raw_llm_result={"skipped": True, "reason": "VLM_TIMEOUT"},
            preview=_preview_payload(
                selected_native, images, ocr_engines, ocr_skipped=ocr_skipped
            ),
            warnings=warnings,
            degradations=degradations,
        )
    except Exception as error:
        message = f"内容AI解析を利用できないため、ファイル名解析の候補だけを使用します: {error}"
        warnings.append(message)
        degradations.append(message)
        return DraftClassificationResult(
            rule_result=rule_result,
            raw_llm_result={
                "skipped": True,
                "reason": "VLM_ERROR",
                "error": str(error)[:1000],
            },
            preview=_preview_payload(
                selected_native, images, ocr_engines, ocr_skipped=ocr_skipped
            ),
            warnings=warnings,
            degradations=degradations,
        )

    candidates: list[RuleCandidate] = []
    for item in output.tags:
        tag = tag_by_id.get(item.tag_id)
        if tag is None or tag.group_id in resolved_groups:
            warnings.append(f"LLMが許可されていないタグ {item.tag_id} を返したため除外しました")
            continue
        candidates.append(
            RuleCandidate(
                field_kind="TAG",
                target_key=tag.tag_id,
                value_raw=tag.name,
                value_normalized=tag.code,
                source="LLM",
                confidence=item.confidence,
                evidence={"text": item.evidence, "pages": sorted(selected_native)},
                confirmed=False,
            )
        )
    if output.customer and not customer_resolved:
        normalized = normalize_customer_name(
            output.customer.value,
            suffixes=config.customer_suffixes,
            version=config.normalization_version,
        )
        candidates.append(
            RuleCandidate(
                field_kind="CUSTOMER",
                target_key="customer_name",
                value_raw=normalized.raw,
                value_normalized=normalized.normalized,
                source="LLM",
                confidence=output.customer.confidence,
                evidence={"text": output.customer.evidence, "pages": sorted(selected_native)},
                confirmed=False,
            )
        )
    if output.document_date and not date_resolved:
        candidates.append(
            RuleCandidate(
                field_kind="DATE",
                target_key="document_year_month",
                value_raw=f"{output.document_date.year:04d}-{output.document_date.month:02d}",
                value_normalized=f"{output.document_date.year:04d}-{output.document_date.month:02d}",
                source="LLM",
                confidence=output.document_date.confidence,
                evidence={"text": output.document_date.evidence, "pages": sorted(selected_native)},
                confirmed=False,
            )
        )
    return DraftClassificationResult(
        rule_result=rule_result,
        llm_candidates=candidates,
        raw_llm_result=output.model_dump(mode="json"),
        preview=_preview_payload(
            selected_native, images, ocr_engines, ocr_skipped=ocr_skipped
        ),
        warnings=warnings,
        degradations=degradations,
    )
