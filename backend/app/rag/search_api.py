from __future__ import annotations

import asyncio
import json
import logging
import time
from difflib import SequenceMatcher
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.rag.classification_rules import normalize_comparable
from app.rag.clients import vlm_client
from app.rag.models import (
    RETRIEVAL_MODES,
    FieldFilter,
    MetadataSearchFilters,
    RetrievalMode,
    RetrievalModeOption,
    SearchV2Request,
    SearchV2Response,
)
from app.rag.search_pipeline import (
    available_retrieval_modes,
    principal_hash,
    search_pipeline,
)
from app.rag.query_condition_parser import parse_query_conditions

router = APIRouter(tags=["retrieval"])
logger = logging.getLogger(__name__)
SEARCH_EVENT_HEARTBEAT_SECONDS = 2.0
RETRIEVAL_MODE_CONTENT: dict[RetrievalMode, tuple[str, str]] = {
    "oracle_text": (
        "キーワード検索",
        "標準テキストをキーワードで照合します。",
    ),
    "text_vector": (
        "テキスト類似",
        "標準テキストの意味が近い候補を探します。",
    ),
    "vlm_text": (
        "VLM抽出キーワード",
        "VLMが抽出したテキストをキーワードで照合します。",
    ),
    "vlm_vector": (
        "VLM抽出類似",
        "VLMが抽出したテキストの意味が近い候補を探します。",
    ),
    "visual_vector": (
        "画像類似",
        "ページ画像と画像＋テキストのベクトルから探します。",
    ),
}


class DifyRetrievalSetting(BaseModel):
    top_k: int = Field(default=10, ge=1, le=100)
    score_threshold: float | None = None


class DifyRetrievalRequest(BaseModel):
    knowledge_id: str | None = None
    query: str = Field(min_length=1, max_length=4000)
    retrieval_setting: DifyRetrievalSetting = Field(default_factory=DifyRetrievalSetting)


class SearchFeedbackRequest(BaseModel):
    trace_id: str = Field(min_length=1, max_length=64)
    document_id: str | None = Field(default=None, max_length=64)
    evidence_id: str | None = Field(default=None, max_length=128)
    action: Literal["relevant", "irrelevant", "opened", "downloaded"]


class SearchConceptSuggestionRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=24, ge=1, le=30)


class QueryConditionParseRequest(BaseModel):
    query: str = Field(default="", max_length=4000)


class SearchEvidenceExplanationRequest(BaseModel):
    query: str = Field(default="", max_length=4000)
    file_name: str = Field(min_length=1, max_length=1024)
    page_number: int | None = Field(default=None, ge=1)
    relevance_percent: float | None = Field(default=None, ge=0, le=100)
    image_similarity_percent: float | None = Field(default=None, ge=0, le=100)
    retrieval_channels: list[str] = Field(default_factory=list, max_length=20)
    match_reasons: list[str] = Field(default_factory=list, max_length=30)
    text_excerpt: str = Field(default="", max_length=6000)
    caption: str = Field(default="", max_length=3000)
    visual_rank: int | None = Field(default=None, ge=1)
    text_rerank_rank: int | None = Field(default=None, ge=1)


class SearchEvidenceExplanationResponse(BaseModel):
    explanation: str


def _concept_value(item: object, key: str, default: object = "") -> object:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _lexical_concept_suggestion_ids(
    query: str,
    concepts: list[object],
    *,
    limit: int,
) -> list[str]:
    """Rank a safe local fallback when semantic suggestion is unavailable."""
    normalized_query = normalize_comparable(query)
    if not normalized_query:
        return []
    ranked: list[tuple[float, int, int, str]] = []
    query_chars = set(normalized_query)
    for item in concepts:
        concept_id = str(_concept_value(item, "concept_id"))
        label = normalize_comparable(
            str(
                _concept_value(item, "normalized_label")
                or _concept_value(item, "display_label")
            )
        )
        category = normalize_comparable(str(_concept_value(item, "category_name")))
        if not concept_id or not label:
            continue
        if label == normalized_query:
            score = 1.0
        elif label in normalized_query:
            score = 0.96
        elif normalized_query in label:
            score = 0.9
        else:
            sequence = SequenceMatcher(None, normalized_query, label).ratio()
            overlap = len(query_chars.intersection(label)) / max(1, len(set(label)))
            category_score = (
                SequenceMatcher(None, normalized_query, category).ratio() * 0.45
                if category
                else 0.0
            )
            score = max(sequence, overlap * 0.75, category_score)
        if score < 0.2:
            continue
        ranked.append(
            (
                score,
                int(_concept_value(item, "support_set_count", 0) or 0),
                int(_concept_value(item, "support_document_count", 0) or 0),
                concept_id,
            )
        )
    ranked.sort(key=lambda value: (-value[0], -value[1], -value[2], value[3]))
    return [value[3] for value in ranked[:limit]]


def _validated_ai_concept_ids(
    response: Any,
    *,
    allowed_ids: set[str],
    limit: int,
) -> list[str]:
    if not isinstance(response, dict):
        return []
    raw_ids = response.get("concept_ids")
    if not isinstance(raw_ids, list):
        return []
    result: list[str] = []
    for value in raw_ids:
        concept_id = str(value)
        if concept_id in allowed_ids and concept_id not in result:
            result.append(concept_id)
        if len(result) >= limit:
            break
    return result


def _sse(event: dict[str, object]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def _agui_event(
    event_type: str,
    *,
    run_id: str,
    thread_id: str,
    **payload: object,
) -> dict[str, object]:
    return {
        "type": event_type,
        "runId": run_id,
        "threadId": thread_id,
        "timestamp": time.time(),
        **payload,
    }


def _parse_filters(field_filters: str, document_types: str) -> tuple[list[FieldFilter], list[str]]:
    raw_filters = json.loads(field_filters)
    raw_document_types = json.loads(document_types)
    if not isinstance(raw_filters, list) or len(raw_filters) > 50:
        raise ValueError("field_filters must be an array with at most 50 items")
    if not isinstance(raw_document_types, list) or len(raw_document_types) > 50:
        raise ValueError("document_types must be an array with at most 50 items")
    return [FieldFilter.model_validate(item) for item in raw_filters], [
        str(item) for item in raw_document_types
    ]


def _parse_retrieval_modes(value: str | None) -> list[RetrievalMode] | None:
    if value is None:
        return None
    raw_modes = json.loads(value)
    if not isinstance(raw_modes, list) or not raw_modes:
        raise ValueError("retrieval_modes must be a non-empty array")
    if len(raw_modes) > len(RETRIEVAL_MODES):
        raise ValueError("retrieval_modes contains too many items")
    unknown = [item for item in raw_modes if item not in RETRIEVAL_MODES]
    if unknown:
        raise ValueError(f"unsupported retrieval_modes: {unknown}")
    return [mode for mode in RETRIEVAL_MODES if mode in raw_modes]


def _parse_concept_ids(value: str | object) -> list[str]:
    # Direct unit calls do not pass through FastAPI's dependency resolution,
    # so an omitted Form field can still be the FormInfo descriptor itself.
    serialized = value if isinstance(value, (str, bytes, bytearray)) else "[]"
    raw = json.loads(serialized or "[]")
    if not isinstance(raw, list) or len(raw) > 30:
        raise ValueError("selected_concept_ids must be an array with at most 30 items")
    return list(dict.fromkeys(str(item) for item in raw if str(item)))


def _parse_metadata_filters(value: str | object) -> MetadataSearchFilters:
    # Direct unit calls do not pass through FastAPI's dependency resolution,
    # so an omitted Form field can still be the FormInfo descriptor itself.
    serialized = value if isinstance(value, (str, bytes, bytearray)) else "{}"
    raw = json.loads(serialized or "{}")
    if not isinstance(raw, dict):
        raise ValueError("metadata_filters must be an object")
    return MetadataSearchFilters.model_validate(raw)


def _retrieval_mode_options(schema_ready: bool) -> list[dict[str, object]]:
    if not schema_ready:
        return [
            RetrievalModeOption(
                value=mode,
                label=RETRIEVAL_MODE_CONTENT[mode][0],
                description=RETRIEVAL_MODE_CONTENT[mode][1],
                available=False,
                unavailable_reason="検索索引が初期化されていません。",
            ).model_dump(mode="json")
            for mode in RETRIEVAL_MODES
        ]

    from app.rag.pipeline_repository import pipeline_repository
    from app.rag.profile_repository import profile_repository
    from app.rag.service_settings import retrieval_service_settings

    weights = retrieval_service_settings.get_weights()
    profiles = profile_repository.enabled_profiles()
    recipes = pipeline_repository.enabled_recipes()
    available = available_retrieval_modes(
        weights=weights,
        profiles=profiles,
        recipes=recipes,
    )
    options: list[dict[str, object]] = []
    for mode in RETRIEVAL_MODES:
        unavailable_reason: str | None = None
        if getattr(weights, mode) <= 0:
            unavailable_reason = "管理者設定で重みが0になっています。"
        elif mode in {"vlm_text", "vlm_vector"} and not profiles:
            unavailable_reason = "有効なVLM抽出プロファイルがありません。"
        elif mode not in available:
            unavailable_reason = "利用できるEmbeddingレシピがありません。"
        label, description = RETRIEVAL_MODE_CONTENT[mode]
        options.append(
            RetrievalModeOption(
                value=mode,
                label=label,
                description=description,
                available=mode in available,
                unavailable_reason=unavailable_reason,
            ).model_dump(mode="json")
        )
    return options


def _search_events(
    request: Request,
    *,
    query: str,
    top_k: int,
    min_score: float = 0.0,
    field_filters: list[FieldFilter],
    document_types: list[str],
    current_version_only: bool,
    filename_filter: str | None,
    metadata_filters: MetadataSearchFilters | None = None,
    selected_concept_ids: list[str] | None = None,
    concept_mode: str = "BOOST",
    image: bytes | None = None,
    image_media_type: str = "image/png",
    retrieval_modes: list[RetrievalMode] | None = None,
    verify: bool = False,
    debug: bool = False,
) -> StreamingResponse:
    run_id = uuid4().hex
    thread_id = f"search:{principal_hash(getattr(request.state, 'auth_username', None)) or 'anonymous'}"

    async def generate():
        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

        async def emit(event: dict[str, object]) -> None:
            await queue.put(_agui_event(
                str(event.pop("type")),
                run_id=run_id,
                thread_id=thread_id,
                **event,
            ))

        async def run_search() -> None:
            started = time.perf_counter()
            try:
                await emit({"type": "RUN_STARTED"})
                await emit({
                    "type": "STATE_SNAPSHOT",
                    "snapshot": {
                        "status": "started",
                        "message": "検索を開始しました",
                        "steps": [],
                        "result": None,
                    },
                })
                result = await search_pipeline.search(
                    query=query,
                    top_k=top_k,
                    min_score=min_score,
                    field_filters=field_filters,
                    document_types=document_types,
                    current_version_only=current_version_only,
                    user_hash=principal_hash(getattr(request.state, "auth_username", None)),
                    filename_filter=filename_filter,
                    metadata_filters=metadata_filters,
                    selected_concept_ids=selected_concept_ids,
                    concept_mode=concept_mode,
                    image=image,
                    image_media_type=image_media_type,
                    retrieval_modes=retrieval_modes,
                    verify=verify,
                    debug=debug,
                    progress=emit,
                )
                result_json = result.model_dump(mode="json")
                await emit({
                    "type": "STATE_DELTA",
                    "delta": [
                        {"op": "replace", "path": "/status", "value": "finished"},
                        {"op": "replace", "path": "/message", "value": "検索が完了しました"},
                        {"op": "replace", "path": "/result", "value": result_json},
                    ],
                })
                await emit({
                    "type": "RUN_FINISHED",
                    "result": result_json,
                    "elapsedMs": round((time.perf_counter() - started) * 1000),
                })
            except Exception as error:
                logger.exception("AG-UI search stream failed")
                await emit({
                    "type": "RUN_ERROR",
                    "message": str(error),
                    "elapsedMs": round((time.perf_counter() - started) * 1000),
                })
            finally:
                await queue.put(None)

        task = asyncio.create_task(run_search())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=SEARCH_EVENT_HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event is None:
                    break
                yield _sse(event)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/search/v2/query-conditions/parse")
async def parse_search_query_conditions(
    payload: QueryConditionParseRequest,
) -> dict[str, object]:
    started_at = time.perf_counter()
    result = parse_query_conditions(payload.query)
    result["elapsed_ms"] = round((time.perf_counter() - started_at) * 1000, 3)
    return result


@router.get("/search/v2/filters")
async def search_v2_filters(request: Request) -> dict[str, object]:
    from app.rag.pipeline_repository import pipeline_repository
    from app.rag.document_metadata_repository import document_metadata_repository
    from app.rag.document_metadata_schema import document_library_schema_status
    from app.rag.case_comparison_repository import case_comparison_repository

    # schema_ready()は同期DBコール（DB停止時は接続待ちで長時間ブロックする）。
    # イベントループを凍結させないよう必ずワーカースレッドで実行する。
    schema_ready = await asyncio.to_thread(pipeline_repository.schema_ready)
    document_library_ready = False
    folders: list[object] = []
    tag_groups: list[object] = []
    tags: list[object] = []
    customers: list[object] = []
    date_bounds: dict[str, object] = {"min_year": None, "max_year": None}
    search_concepts: list[object] = []
    search_concept_settings: dict[str, object] = {}
    building_conditions: dict[str, object] = {}
    if schema_ready:
        feature_status = await asyncio.to_thread(document_library_schema_status)
        document_library_ready = bool(feature_status.get("ready"))
        if document_library_ready:
            (
                folder_values,
                group_values,
                tag_values,
                customer_values,
                date_values,
                concept_values,
                concept_settings,
                building_condition_values,
            ) = (
                await asyncio.gather(
                    asyncio.to_thread(document_metadata_repository.folder_tree),
                    asyncio.to_thread(document_metadata_repository.list_tag_groups),
                    asyncio.to_thread(document_metadata_repository.list_tags),
                    asyncio.to_thread(
                        document_metadata_repository.customer_suggestions,
                        query="",
                        user_hash=principal_hash(
                            getattr(request.state, "auth_username", None)
                        ),
                        limit=100,
                    ),
                    asyncio.to_thread(document_metadata_repository.document_date_bounds),
                    asyncio.to_thread(
                        document_metadata_repository.list_search_concepts,
                        status="ACTIVE",
                        limit=500,
                        include_zero_support=False,
                    ),
                    asyncio.to_thread(
                        document_metadata_repository.get_concept_settings
                    ),
                    asyncio.to_thread(
                        case_comparison_repository.condition_options,
                        principal_hash(
                            getattr(request.state, "auth_username", None)
                        ),
                    ),
                )
            )
            folders = [item.model_dump(mode="json") for item in folder_values]
            tag_groups = [item.model_dump(mode="json") for item in group_values]
            tags = [item.model_dump(mode="json") for item in tag_values]
            customers = [item.model_dump(mode="json") for item in customer_values]
            date_bounds = date_values
            search_concepts = [
                item.model_dump(mode="json") for item in concept_values
            ]
            search_concept_settings = concept_settings.model_dump(mode="json")
            building_conditions = building_condition_values.model_dump(mode="json")
    return {
        "profile_retrieval_active": False,
        "v2_retrieval_active": schema_ready,
        "document_library_ready": document_library_ready,
        "fields": [],
        "folders": folders,
        "tag_groups": tag_groups,
        "tags": tags,
        "customer_suggestions": customers,
        "date_bounds": date_bounds,
        "search_concepts": search_concepts,
        "search_concept_settings": search_concept_settings,
        "building_conditions": building_conditions,
        "retrieval_modes": await asyncio.to_thread(
            _retrieval_mode_options,
            schema_ready,
        ),
    }


@router.post("/search/v2/concepts/suggest")
async def suggest_search_concepts(
    payload: SearchConceptSuggestionRequest,
) -> dict[str, object]:
    """Select only existing ACTIVE concepts related to a keyword or sentence."""
    from app.rag.document_metadata_repository import document_metadata_repository

    concepts = await asyncio.to_thread(
        document_metadata_repository.list_search_concepts,
        status="ACTIVE",
        limit=500,
        include_zero_support=False,
    )
    fallback_ids = _lexical_concept_suggestion_ids(
        payload.query,
        list(concepts),
        limit=payload.limit,
    )
    if not concepts:
        return {
            "concept_ids": [],
            "source": "EMPTY",
            "message": "利用できる検索条件がありません",
        }

    catalog = [
        {
            "concept_id": str(_concept_value(item, "concept_id")),
            "label": str(_concept_value(item, "display_label")),
            "facet": str(_concept_value(item, "facet")),
            "category": str(_concept_value(item, "category_name")),
        }
        for item in concepts
    ]
    prompt = (
        "ユーザーのキーワードまたは文章に関連する検索条件を、候補一覧から選んでください。\n"
        "表記が一致しなくても、同義語、言い換え、実現したい暮らし、空間、設備、"
        "デザインの関係を考慮してください。\n"
        "候補一覧にない条件を生成してはいけません。弱い連想だけの候補は選ばず、"
        f"最大{payload.limit}件まで関連度順に返してください。\n"
        "出力は {\"concept_ids\":[\"候補ID\"]} のJSONだけにしてください。\n\n"
        f"ユーザー入力:\n{json.dumps(payload.query, ensure_ascii=False)}\n\n"
        f"候補一覧:\n{json.dumps(catalog, ensure_ascii=False)}"
    )
    try:
        response = await vlm_client.generate_json(prompt=prompt)
        concept_ids = _validated_ai_concept_ids(
            response,
            allowed_ids={
                str(_concept_value(item, "concept_id")) for item in concepts
            },
            limit=payload.limit,
        )
    except Exception:
        logger.warning("AI検索条件サジェストに失敗したため文字類似へフォールバックします", exc_info=True)
        concept_ids = []

    if concept_ids:
        # Direct textual matches are deterministic and must not be lost if the
        # model returns only broader semantic suggestions.
        direct_ids = [
            concept_id
            for concept_id in fallback_ids
            if any(
                concept_id == str(_concept_value(item, "concept_id"))
                and normalize_comparable(
                    str(_concept_value(item, "display_label"))
                )
                in normalize_comparable(payload.query)
                for item in concepts
            )
        ]
        concept_ids = list(dict.fromkeys([*direct_ids, *concept_ids]))[: payload.limit]
        return {
            "concept_ids": concept_ids,
            "source": "AI",
            "message": f"{len(concept_ids)}件の関連候補を抽出しました",
        }
    return {
        "concept_ids": fallback_ids,
        "source": "LEXICAL",
        "message": (
            f"{len(fallback_ids)}件を文字の近さから表示しています"
            if fallback_ids
            else "関連する候補が見つかりませんでした"
        ),
    }


@router.post("/retrieval")
@router.post("/dify/retrieval")
async def dify_retrieval(
    payload: DifyRetrievalRequest, request: Request, response: Response
) -> dict[str, object]:
    """Dify external-knowledge compatibility adapter backed by retrieval-only v2."""
    result = await search_pipeline.search(
        query=payload.query,
        top_k=payload.retrieval_setting.top_k,
        field_filters=[],
        document_types=[],
        current_version_only=True,
        user_hash=principal_hash(getattr(request.state, "auth_username", None)),
    )
    response.headers["X-Score-Threshold-Deprecated"] = "true"
    records: list[dict[str, object]] = []
    for document in result.results:
        excerpts = [
            item.text_excerpt or item.caption
            for item in document.evidence
            if item.text_excerpt or item.caption
        ]
        records.append(
            {
                "content": "\n\n".join(excerpts),
                "score": document.score,
                "title": document.file_name,
                "metadata": {
                    "document_id": document.document_id,
                    "object_name": document.object_name,
                    "profile_slots": document.profile_slots,
                    "trace_id": result.trace_id,
                },
            }
        )
    return {"records": records}


@router.post("/search/v2/feedback")
async def search_v2_feedback(payload: SearchFeedbackRequest, request: Request) -> dict[str, object]:
    from app.rag.oracle_repository import rag_repository

    try:
        rag_repository.record_search_feedback(
            feedback_id=uuid4().hex,
            trace_id=payload.trace_id,
            document_id=payload.document_id,
            evidence_id=payload.evidence_id,
            action=payload.action,
            user_hash=principal_hash(getattr(request.state, "auth_username", None)),
        )
    except Exception as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"success": True}


@router.post(
    "/search/v2/evidence/explain",
    response_model=SearchEvidenceExplanationResponse,
)
async def explain_search_evidence(
    payload: SearchEvidenceExplanationRequest,
) -> SearchEvidenceExplanationResponse:
    """Explain one evidence score on demand without changing search ranking."""
    prompt = (
        "検索結果の1ページについて、なぜ検索候補になったのかを日本語で説明してください。\n"
        "入力にある検索語、検索経路、順位、抜粋、画像説明だけを根拠にしてください。\n"
        "関連度の数値を生成AIが計算したように説明してはいけません。関連度は別の検索・"
        "再順位付け処理の出力です。情報が不足する点は不足していると明記してください。\n"
        "検索経路の技術名は、利用者に分かる表現へ言い換えてください。\n"
        "2〜5文で簡潔にまとめ、出力は {\"explanation\":\"...\"} のJSONだけにしてください。\n\n"
        "検索エビデンス:\n"
        + json.dumps(payload.model_dump(), ensure_ascii=False)
    )
    try:
        response = await vlm_client.generate_json(prompt=prompt)
    except RuntimeError as error:
        raise HTTPException(
            status_code=503,
            detail=f"生成AIへ接続できません: {error}",
        ) from error
    except Exception as error:
        logger.exception("検索結果のAI解説に失敗しました")
        raise HTTPException(status_code=502, detail="生成AIの解説に失敗しました") from error
    explanation = str(response.get("explanation") or "").strip()
    if not explanation:
        raise HTTPException(status_code=502, detail="生成AIから解説を取得できませんでした")
    return SearchEvidenceExplanationResponse(explanation=explanation)


@router.post("/search/v2", response_model=SearchV2Response)
async def search_v2(payload: SearchV2Request, request: Request) -> SearchV2Response:
    try:
        return await search_pipeline.search(
            query=payload.query,
            top_k=payload.top_k,
            min_score=payload.min_score,
            field_filters=payload.field_filters,
            document_types=payload.document_types,
            current_version_only=payload.current_version_only,
            user_hash=principal_hash(getattr(request.state, "auth_username", None)),
            filename_filter=payload.filename_filter,
            metadata_filters=payload.metadata_filters,
            selected_concept_ids=payload.selected_concept_ids,
            concept_mode=payload.concept_mode,
            retrieval_modes=payload.retrieval_modes,
            verify=payload.verify,
            debug=payload.debug,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/search/v2/events")
async def search_v2_events(payload: SearchV2Request, request: Request) -> StreamingResponse:
    return _search_events(
        request,
        query=payload.query,
        top_k=payload.top_k,
        min_score=payload.min_score,
        field_filters=payload.field_filters,
        document_types=payload.document_types,
        current_version_only=payload.current_version_only,
        filename_filter=payload.filename_filter,
        metadata_filters=payload.metadata_filters,
        selected_concept_ids=payload.selected_concept_ids,
        concept_mode=payload.concept_mode,
        retrieval_modes=payload.retrieval_modes,
        verify=payload.verify,
        debug=payload.debug,
    )


@router.post("/search/v2/image", response_model=SearchV2Response)
async def search_v2_image(
    request: Request,
    image: UploadFile = File(...),
    query: str = Form(default="", max_length=4000),
    top_k: int = Form(default=20, ge=1, le=100),
    min_score: float = Form(default=0.0, ge=0.0, le=1.0),
    filename_filter: str | None = Form(default=None, max_length=1024),
    field_filters: str = Form(default="[]"),
    document_types: str = Form(default="[]"),
    metadata_filters: str = Form(default="{}"),
    selected_concept_ids: str = Form(default="[]"),
    concept_mode: Literal["BOOST", "REQUIRE_ALL"] = Form(default="BOOST"),
    current_version_only: bool = Form(default=True),
    retrieval_modes: str | None = Form(default=None),
    verify: bool = Form(default=False),
    debug: bool = Form(default=False),
) -> SearchV2Response:
    allowed = {"image/png", "image/jpeg", "image/webp"}
    if image.content_type not in allowed:
        raise HTTPException(status_code=400, detail="PNG, JPEG, or WebP image is required")
    content = await image.read()
    if not content or len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="image must be between 1 byte and 10 MiB")
    try:
        filters, types = _parse_filters(field_filters, document_types)
        modes = _parse_retrieval_modes(retrieval_modes)
        metadata = _parse_metadata_filters(metadata_filters)
        concepts = _parse_concept_ids(selected_concept_ids)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        return await search_pipeline.search(
            query=query,
            top_k=top_k,
            min_score=min_score,
            field_filters=filters,
            document_types=types,
            current_version_only=current_version_only,
            user_hash=principal_hash(getattr(request.state, "auth_username", None)),
            filename_filter=filename_filter,
            metadata_filters=metadata,
            selected_concept_ids=concepts,
            concept_mode=concept_mode,
            image=content,
            image_media_type=image.content_type,
            retrieval_modes=modes,
            verify=verify,
            debug=debug,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/search/v2/image/events")
async def search_v2_image_events(
    request: Request,
    image: UploadFile = File(...),
    query: str = Form(default="", max_length=4000),
    top_k: int = Form(default=20, ge=1, le=100),
    min_score: float = Form(default=0.0, ge=0.0, le=1.0),
    filename_filter: str | None = Form(default=None, max_length=1024),
    field_filters: str = Form(default="[]"),
    document_types: str = Form(default="[]"),
    metadata_filters: str = Form(default="{}"),
    selected_concept_ids: str = Form(default="[]"),
    concept_mode: Literal["BOOST", "REQUIRE_ALL"] = Form(default="BOOST"),
    current_version_only: bool = Form(default=True),
    retrieval_modes: str | None = Form(default=None),
    verify: bool = Form(default=False),
    debug: bool = Form(default=False),
) -> StreamingResponse:
    allowed = {"image/png", "image/jpeg", "image/webp"}
    if image.content_type not in allowed:
        raise HTTPException(status_code=400, detail="PNG, JPEG, or WebP image is required")
    content = await image.read()
    if not content or len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="image must be between 1 byte and 10 MiB")
    try:
        filters, types = _parse_filters(field_filters, document_types)
        modes = _parse_retrieval_modes(retrieval_modes)
        metadata = _parse_metadata_filters(metadata_filters)
        concepts = _parse_concept_ids(selected_concept_ids)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _search_events(
        request,
        query=query,
        top_k=top_k,
        min_score=min_score,
        field_filters=filters,
        document_types=types,
        current_version_only=current_version_only,
        filename_filter=filename_filter,
        metadata_filters=metadata,
        selected_concept_ids=concepts,
        concept_mode=concept_mode,
        image=content,
        image_media_type=image.content_type,
        retrieval_modes=modes,
        verify=verify,
        debug=debug,
    )
