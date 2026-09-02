from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.rag.case_comparison_models import (
    BuildingConditionOptions,
    ComparisonAnalysis,
    ComparisonAnalysisRequest,
    ComparisonSelectionRequest,
    DocumentSetComparison,
    DocumentSetFact,
    DocumentSetFactsPatch,
    PageClassification,
    PageClassificationPatch,
)
from app.rag.case_comparison_repository import case_comparison_repository
from app.rag.clients import vlm_client
from app.rag.pipeline_repository import pipeline_repository
from app.rag.search_pipeline import principal_hash
from app.services.oci_service import oci_service


router = APIRouter(prefix="/document-library", tags=["case-comparison"])
logger = logging.getLogger(__name__)
_analysis_tasks: set[asyncio.Task[Any]] = set()


def _user_hash(request: Request) -> str | None:
    return principal_hash(getattr(request.state, "auth_username", None))


def _raise_api_error(error: Exception) -> None:
    if isinstance(error, HTTPException):
        raise error
    if isinstance(error, LookupError):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, ValueError):
        raise HTTPException(status_code=422, detail=str(error)) from error
    if isinstance(error, RuntimeError):
        raise HTTPException(status_code=503, detail=str(error)) from error
    raise HTTPException(status_code=500, detail=str(error)) from error


@router.get(
    "/document-sets/{document_set_id}/comparison",
    response_model=DocumentSetComparison,
)
def get_comparison(
    document_set_id: str,
    request: Request,
    refresh: bool = False,
) -> DocumentSetComparison:
    try:
        return case_comparison_repository.comparison(
            document_set_id,
            _user_hash(request),
            refresh=refresh,
        )
    except Exception as error:
        _raise_api_error(error)


@router.post("/document-sets/{document_set_id}/comparison/refresh")
def refresh_comparison(document_set_id: str, request: Request) -> dict[str, int]:
    try:
        return case_comparison_repository.refresh_set(
            document_set_id, _user_hash(request)
        )
    except Exception as error:
        _raise_api_error(error)


@router.put(
    "/document-sets/{document_set_id}/comparison",
    response_model=DocumentSetComparison,
)
def save_comparison(
    document_set_id: str,
    payload: ComparisonSelectionRequest,
    request: Request,
) -> DocumentSetComparison:
    try:
        return case_comparison_repository.save_pair(
            document_set_id, payload, _user_hash(request)
        )
    except Exception as error:
        _raise_api_error(error)


@router.patch(
    "/page-classifications/{document_id}/{revision_id}/{page_number}",
    response_model=PageClassification,
)
def update_page_classification(
    document_id: str,
    revision_id: str,
    page_number: int,
    payload: PageClassificationPatch,
    request: Request,
) -> PageClassification:
    try:
        from app.rag.case_comparison_models import PageReference

        return case_comparison_repository.update_page(
            PageReference(
                document_id=document_id,
                revision_id=revision_id,
                page_number=page_number,
            ),
            payload,
            _user_hash(request),
        )
    except Exception as error:
        _raise_api_error(error)


@router.put(
    "/document-sets/{document_set_id}/building-facts",
    response_model=list[DocumentSetFact],
)
def update_building_facts(
    document_set_id: str,
    payload: DocumentSetFactsPatch,
    request: Request,
) -> list[DocumentSetFact]:
    try:
        user_hash = _user_hash(request)
        return case_comparison_repository.replace_facts(
            document_set_id, payload.items, user_hash
        )
    except Exception as error:
        _raise_api_error(error)


@router.get(
    "/building-condition-options",
    response_model=BuildingConditionOptions,
)
def building_condition_options(request: Request) -> BuildingConditionOptions:
    try:
        return case_comparison_repository.condition_options(_user_hash(request))
    except Exception as error:
        _raise_api_error(error)


def _page_text(document_id: str, page_number: int) -> str:
    response = pipeline_repository.list_page_texts(
        document_id,
        selector="serving",
        page_number=page_number,
    )
    preferred = ["PAGE_TEXT", "MINERU_TEXT", "NATIVE_TEXT", "OCR_TEXT", "VLM_TEXT"]
    by_kind: dict[str, list[str]] = {kind: [] for kind in preferred}
    for item in response.get("items") or []:
        kind = str(item.get("artifact_kind") or "")
        text = str(item.get("raw_text") or "").strip()
        if kind in by_kind and text:
            by_kind[kind].append(text)
    sections: list[str] = []
    for kind in preferred:
        if by_kind[kind]:
            sections.append(f"[{kind}]\n" + "\n".join(by_kind[kind]))
    return "\n\n".join(sections)[:24000]


def _page_image(page: Any) -> tuple[bytes, str]:
    artifact = pipeline_repository.get_page_image_artifact(
        page.document_id,
        str(page.release_id),
        str(page.artifact_id),
    )
    content = oci_service.download_object(str(artifact["object_name"]))
    if not content:
        raise RuntimeError("比較対象のページ画像を取得できません")
    return content, str(artifact.get("media_type") or "image/png")


async def _run_analysis(analysis_id: str) -> None:
    try:
        if not case_comparison_repository.claim_analysis(analysis_id):
            return
        context = case_comparison_repository.analysis_context(analysis_id)
        comparison = context["comparison"]
        before = context["before"]
        after = context["after"]
        before_image, after_image, before_text, after_text = await asyncio.gather(
            asyncio.to_thread(_page_image, before),
            asyncio.to_thread(_page_image, after),
            asyncio.to_thread(_page_text, before.document_id, before.page_number),
            asyncio.to_thread(_page_text, after.document_id, after.page_number),
        )
        facts = [item.model_dump(mode="json") for item in comparison.facts]
        prompt = f"""
あなたは住宅リフォーム図面の比較担当です。1枚目は現況（Before）、2枚目は提案（After）の平面図です。
画像、保存済み抽出テキスト、型付き属性を照合し、確認できる変更だけを日本語で説明してください。
Afterは完成実績ではなく提案内容です。「改善された」「完成した」と断定せず、「提案されている」と表現してください。
寸法や室名が読めない場合は推測せず、uncertaintyに記録してください。

案件: {comparison.label}
Before: {before.file_name} page:{before.page_number}, floor={before.floor_code or '不明'}
After: {after.file_name} page:{after.page_number}, floor={after.floor_code or '不明'}
型付き属性: {json.dumps(facts, ensure_ascii=False)}

[Before保存テキスト]
{before_text}

[After保存テキスト]
{after_text}

次のJSONだけを返してください。
{{
  "summary": "変更提案の要約（2～4文）",
  "change_items": [
    {{
      "category": "間取り|動線|収納|水回り|面積|性能|その他",
      "before": "現況で確認できる内容",
      "after": "提案図で確認できる内容",
      "evidence_before": "ページ上の根拠",
      "evidence_after": "ページ上の根拠",
      "confidence": 0.0,
      "uncertainty": "不確実性。なければ空文字"
    }}
  ],
  "unchanged_or_unclear": ["変化を断定できない項目"],
  "proposal_status": "PROPOSED_NOT_COMPLETED"
}}
""".strip()
        result = await vlm_client.generate_json(
            prompt=prompt,
            images=[before_image, after_image],
        )
        if not isinstance(result, dict):
            raise RuntimeError("生成AIから比較結果のJSONを取得できません")
        result["proposal_status"] = "PROPOSED_NOT_COMPLETED"
        case_comparison_repository.set_analysis_status(
            analysis_id, "COMPLETED", result=result
        )
    except Exception as error:
        logger.exception("case comparison analysis failed: %s", analysis_id)
        case_comparison_repository.set_analysis_status(
            analysis_id, "FAILED", error_summary=str(error)[:2000]
        )


def _start_analysis_task(analysis_id: str) -> None:
    task = asyncio.create_task(_run_analysis(analysis_id))
    _analysis_tasks.add(task)
    task.add_done_callback(_analysis_tasks.discard)


@router.post(
    "/document-sets/{document_set_id}/comparison-analyses",
    response_model=ComparisonAnalysis,
)
async def create_comparison_analysis(
    document_set_id: str,
    payload: ComparisonAnalysisRequest,
    request: Request,
) -> ComparisonAnalysis:
    try:
        user_hash = _user_hash(request)
        analysis = case_comparison_repository.create_analysis(
            document_set_id, payload, user_hash, force=payload.force
        )
        if analysis.status == "PENDING" and not analysis.cached:
            _start_analysis_task(analysis.analysis_id)
        return analysis
    except Exception as error:
        _raise_api_error(error)


@router.get(
    "/comparison-analyses/{analysis_id}",
    response_model=ComparisonAnalysis,
)
def get_comparison_analysis(
    analysis_id: str, request: Request
) -> ComparisonAnalysis:
    try:
        return case_comparison_repository.get_analysis(
            analysis_id, _user_hash(request)
        )
    except Exception as error:
        _raise_api_error(error)
