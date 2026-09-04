from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

from app.rag.classification_rules import normalize_customer_name
from app.rag.document_metadata_models import (
    ActiveIngestBatch,
    BulkDocumentDeleteFailure,
    BulkDocumentDeleteResult,
    BulkDocumentSelection,
    BulkDocumentMetadataPatch,
    ClassificationRuleSet,
    ClassificationRuleSetUpsert,
    CustomerSuggestion,
    DocumentConcept,
    DocumentLibraryResponse,
    DocumentMetadata,
    DocumentMetadataPatch,
    DocumentSet,
    DocumentSetCreate,
    DocumentSetSuggestion,
    DocumentSetUpdate,
    ExistingObjectMigrationCandidate,
    ExistingObjectMigrationPreview,
    ExistingObjectMigrationRequest,
    ExistingObjectMigrationResult,
    FolderCreate,
    FolderNode,
    FolderRuleProfile,
    FolderRuleProfileUpsert,
    FolderUpdate,
    IngestBatch,
    IngestBatchCreate,
    IngestItemPatch,
    IngestItemReview,
    UNCLASSIFIED_FOLDER_ID,
    RuleEvaluation,
    RuleTestRequest,
    SearchConcept,
    SearchConceptSettings,
    SearchConceptUpdate,
    TagDefinition,
    TagGroup,
    TagGroupUpsert,
    TagUpsert,
)
from app.rag.document_metadata_repository import document_metadata_repository
from app.rag.draft_classifier import classify_document_preview, classify_filename
from app.rag.pipeline_api import create_job as create_pipeline_job
from app.rag.pipeline_models import PipelineJobRequest
from app.rag.pipeline_repository import pipeline_repository
from app.rag.page_image_cleanup import (
    LEGACY_PAGE_IMAGE_PATTERN,
    is_internal_pipeline_object,
)
from app.rag.search_pipeline import principal_hash
from app.services.oci_service import oci_service


router = APIRouter(prefix="/document-library", tags=["document-library"])

ALLOWED_EXTENSIONS = {
    value.strip().casefold()
    for value in os.getenv(
        "ALLOWED_EXTENSIONS",
        "pdf,xlsx,xls,docx,doc,pptx,ppt,png,jpg,jpeg,txt,md",
    ).split(",")
    if value.strip()
}
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", "200000000"))
MAX_BATCH_FILES = 20
_STABLE_STORAGE_OBJECT_PATTERN = re.compile(
    r"^documents/[a-f0-9]{32}/source\.[^/]+$", re.IGNORECASE
)


def filter_existing_source_objects(
    objects: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Return user source objects while protecting generated/application objects."""
    non_folders = [
        item
        for item in objects
        if not str(item.get("name") or "").endswith("/")
    ]
    protected_names = {
        str(item.get("name") or "")
        for item in non_folders
        if is_internal_pipeline_object(str(item.get("name") or ""))
        or _STABLE_STORAGE_OBJECT_PATTERN.match(str(item.get("name") or ""))
    }
    candidates = [
        item
        for item in non_folders
        if str(item.get("name") or "") not in protected_names
    ]
    original_bases = {
        re.sub(r"\.[^.]+$", "", str(item["name"]))
        for item in candidates
        if not LEGACY_PAGE_IMAGE_PATTERN.search(str(item.get("name") or ""))
    }
    sources = [
        item
        for item in candidates
        if not (
            LEGACY_PAGE_IMAGE_PATTERN.search(str(item.get("name") or ""))
            and str(item["name"]).rsplit("/", 1)[0] in original_bases
        )
    ]
    return sources, len(protected_names)


def _scan_existing_objects() -> tuple[str, str, list[dict[str, Any]], bool]:
    bucket = os.getenv("OCI_BUCKET") or ""
    if not bucket:
        raise RuntimeError("OCI_BUCKETが設定されていません")
    namespace_result = oci_service.get_namespace()
    if not namespace_result.get("success"):
        raise RuntimeError(
            str(namespace_result.get("message") or "Namespaceを取得できません")
        )
    namespace = str(namespace_result["namespace"])
    max_objects = max(1, int(os.getenv("MAX_OBJECTS_FETCH", "10000")))
    objects: list[dict[str, Any]] = []
    page_token: str | None = None
    truncated = False
    while len(objects) < max_objects:
        result = oci_service.list_objects(
            bucket_name=bucket,
            namespace=namespace,
            page_size=min(1000, max_objects - len(objects)),
            page_token=page_token,
        )
        if not result.get("success"):
            raise RuntimeError(
                str(result.get("message") or "Object一覧を取得できません")
            )
        objects.extend(result.get("objects") or [])
        page_token = result.get("next_start_with")
        if not page_token:
            break
    if page_token:
        truncated = True
    return bucket, namespace, objects, truncated


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
        message = str(error)
        status = 409 if "更新されました" in message else 503
        raise HTTPException(status_code=status, detail=message) from error
    raise HTTPException(status_code=500, detail=str(error)) from error


@router.get("/folders", response_model=list[FolderNode])
def list_folders() -> list[FolderNode]:
    try:
        return document_metadata_repository.folder_tree()
    except Exception as error:
        _raise_api_error(error)


@router.post("/folders", response_model=FolderNode, status_code=201)
def create_folder(payload: FolderCreate) -> FolderNode:
    try:
        return document_metadata_repository.create_folder(payload)
    except Exception as error:
        _raise_api_error(error)


@router.patch("/folders/{folder_id}", response_model=FolderNode)
def update_folder(folder_id: str, payload: FolderUpdate) -> FolderNode:
    try:
        return document_metadata_repository.update_folder(folder_id, payload)
    except Exception as error:
        _raise_api_error(error)


@router.delete("/folders/{folder_id}")
def delete_folder(folder_id: str) -> dict[str, bool]:
    try:
        document_metadata_repository.delete_folder(folder_id)
        return {"success": True}
    except Exception as error:
        _raise_api_error(error)


@router.get("/document-sets", response_model=list[DocumentSet])
def list_document_sets(
    q: str = Query(default="", max_length=400),
    include_archived: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[DocumentSet]:
    try:
        return document_metadata_repository.list_document_sets(
            query=q, include_archived=include_archived, limit=limit
        )
    except Exception as error:
        _raise_api_error(error)


@router.post("/document-sets", response_model=DocumentSet, status_code=201)
def create_document_set(
    payload: DocumentSetCreate, request: Request
) -> DocumentSet:
    try:
        return document_metadata_repository.create_document_set(
            payload, created_by_hash=_user_hash(request)
        )
    except Exception as error:
        _raise_api_error(error)


@router.patch("/document-sets/{document_set_id}", response_model=DocumentSet)
def update_document_set(
    document_set_id: str, payload: DocumentSetUpdate
) -> DocumentSet:
    try:
        return document_metadata_repository.update_document_set(
            document_set_id, payload
        )
    except Exception as error:
        _raise_api_error(error)


@router.get(
    "/documents/{document_id}/document-set-suggestions",
    response_model=list[DocumentSetSuggestion],
)
def document_set_suggestions(
    document_id: str,
    limit: int = Query(default=10, ge=1, le=50),
) -> list[DocumentSetSuggestion]:
    try:
        return document_metadata_repository.suggest_document_sets(
            document_id, limit=limit
        )
    except Exception as error:
        _raise_api_error(error)


@router.get("/documents", response_model=DocumentLibraryResponse)
async def list_documents(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    folder_id: str | None = None,
    include_descendants: bool = False,
    query: str | None = Query(default=None, max_length=1024),
    sort: Literal[
        "updated_desc", "created_desc", "updated_asc", "filename_asc"
    ] = "updated_desc",
) -> DocumentLibraryResponse:
    try:
        result = await asyncio.to_thread(
            document_metadata_repository.list_documents,
            page=page,
            page_size=page_size,
            folder_id=folder_id,
            include_descendants=include_descendants,
            query=query,
            sort=sort,
        )
        statuses = await asyncio.to_thread(
            pipeline_repository.statuses_by_object,
            [item.object_name for item in result.items],
            "serving",
        )
        for item in result.items:
            item.processing = statuses.get(item.object_name)
        return result
    except Exception as error:
        _raise_api_error(error)


@router.get("/documents/{document_id}/metadata", response_model=DocumentMetadata)
def get_document_metadata(document_id: str) -> DocumentMetadata:
    try:
        return document_metadata_repository.get_document_metadata(document_id)
    except Exception as error:
        _raise_api_error(error)


@router.get(
    "/documents/{document_id}/concepts", response_model=list[DocumentConcept]
)
def get_document_concepts(document_id: str) -> list[DocumentConcept]:
    try:
        return document_metadata_repository.list_document_concepts(document_id)
    except Exception as error:
        _raise_api_error(error)


@router.get("/search-concepts", response_model=list[SearchConcept])
def list_search_concepts(
    status: str = Query(default="ACTIVE", pattern="^(ACTIVE|PENDING|HIDDEN|MERGED)$"),
    facet: str | None = Query(default=None, pattern="^(BEFORE|AFTER|OTHER)$"),
    q: str = Query(default="", max_length=400),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[SearchConcept]:
    try:
        return document_metadata_repository.list_search_concepts(
            status=status, facet=facet, query=q, limit=limit, offset=offset
        )
    except Exception as error:
        _raise_api_error(error)


def _processing_job_payload(
    summary: dict[str, Any],
    object_name: str,
) -> dict[str, Any]:
    job = pipeline_repository.get_job(str(summary["job_id"]))
    object_steps = [
        step
        for step in job.get("steps", [])
        if str(step.get("object_name")) == object_name
    ]
    return {
        "job_id": str(job["job_id"]),
        "status": str(job["status"]),
        "mode": str(job["job_mode"]),
        "publish_mode": str(job["publish_mode"]),
        "total_steps": int(job.get("total_steps") or 0),
        "completed_steps": int(job.get("completed_steps") or 0),
        "failed_steps": int(job.get("failed_steps") or 0),
        "error_summary": (
            str(job["error_summary"]) if job.get("error_summary") else None
        ),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "affected_object_count": int(summary.get("affected_object_count") or 0),
        "retry_of_job_id": summary.get("retry_of_job_id"),
        "is_additional": bool(summary.get("is_additional")),
        "tab_label": str(summary.get("tab_label") or "全体工程"),
        "steps": [
            {
                "step_id": str(step["step_id"]),
                "kind": str(step["stage_kind"]),
                "component_key": str(step["component_key"]),
                "status": str(step["status"]),
                "progress_current": int(step.get("progress_current") or 0),
                "progress_total": int(step.get("progress_total") or 0),
                "attempt_count": int(step.get("attempt_count") or 0),
                "error_summary": (
                    str(step["error_summary"])
                    if step.get("error_summary")
                    else None
                ),
                "depends_on": [
                    str(value) for value in step.get("depends_on", [])
                ],
                "started_at": step.get("started_at"),
                "completed_at": step.get("completed_at"),
                "updated_at": step.get("updated_at"),
            }
            for step in object_steps
        ],
    }


@router.get("/documents/{document_id}/processing")
def get_document_processing(document_id: str) -> dict[str, Any]:
    try:
        result = pipeline_repository.processing_status(document_id, "latest")
        latest_job = result.get("latest_job")
        if not latest_job:
            result["job"] = None
            result["job_history"] = []
            result["overall_job_id"] = None
            return result
        object_name = str(result["object_name"])
        lineage_loader = getattr(pipeline_repository, "job_lineage_for_object", None)
        summaries = (
            lineage_loader(object_name)
            if callable(lineage_loader)
            else []
        )
        if not summaries:
            summaries = [
                {
                    **latest_job,
                    "retry_of_job_id": None,
                    "is_additional": False,
                    "tab_label": "全体工程",
                }
            ]
        history = [
            _processing_job_payload(summary, object_name)
            for summary in summaries
        ]
        result["job_history"] = history
        result["overall_job_id"] = history[0]["job_id"]
        result["job"] = history[-1]
        return result
    except Exception as error:
        _raise_api_error(error)


@router.patch("/documents/{document_id}/metadata", response_model=DocumentMetadata)
def patch_document_metadata(
    document_id: str, payload: DocumentMetadataPatch, request: Request
) -> DocumentMetadata:
    try:
        return document_metadata_repository.patch_document_metadata(
            document_id,
            payload,
            changed_by_hash=_user_hash(request),
        )
    except Exception as error:
        _raise_api_error(error)


@router.post("/documents/bulk-metadata", response_model=list[DocumentMetadata])
def bulk_patch_document_metadata(
    payload: BulkDocumentMetadataPatch, request: Request
) -> list[DocumentMetadata]:
    results: list[DocumentMetadata] = []
    try:
        for document_id in list(dict.fromkeys(payload.document_ids)):
            results.append(
                document_metadata_repository.patch_document_metadata(
                    document_id,
                    payload.patch,
                    changed_by_hash=_user_hash(request),
                )
            )
        return results
    except Exception as error:
        _raise_api_error(error)

@router.delete("/documents", response_model=BulkDocumentDeleteResult)
async def bulk_delete_documents(
    payload: BulkDocumentSelection,
) -> BulkDocumentDeleteResult:
    """Delete selected registered documents and their generated objects."""
    try:
        records = await asyncio.to_thread(
            document_metadata_repository.documents_for_action,
            payload.document_ids,
        )
        record_by_id = {str(row["document_id"]): row for row in records}
        failures: list[BulkDocumentDeleteFailure] = [
            BulkDocumentDeleteFailure(
                document_id=document_id,
                error="文書が見つからないか、既に削除されています",
            )
            for document_id in payload.document_ids
            if document_id not in record_by_id
        ]
        namespace_result = await asyncio.to_thread(oci_service.get_namespace)
        if not namespace_result.get("success"):
            raise RuntimeError(
                str(namespace_result.get("message") or "Namespaceを取得できません")
            )
        namespace = str(namespace_result["namespace"])
        deleted_ids: list[str] = []
        cleanup_warnings: list[BulkDocumentDeleteFailure] = []
        for document_id in payload.document_ids:
            row = record_by_id.get(document_id)
            if not row:
                continue
            try:
                artifact_names = await asyncio.to_thread(
                    document_metadata_repository.document_artifact_object_names,
                    document_id,
                )
                object_names = list(
                    dict.fromkeys([*artifact_names, str(row["object_name"])])
                )
                # DB references are removed first. If Object Storage cleanup
                # later fails, the user-visible library remains consistent and
                # only an orphaned object needs an operational cleanup.
                deleted = await asyncio.to_thread(
                    document_metadata_repository.delete_registered_document,
                    document_id,
                )
                if deleted != 1:
                    raise RuntimeError("Oracle上の文書を削除できませんでした")
                deleted_ids.append(document_id)
                for object_name in object_names:
                    result = await asyncio.to_thread(
                        oci_service.delete_object,
                        object_name,
                        str(row["bucket"]),
                        namespace,
                    )
                    if not result.get("success"):
                        cleanup_warnings.append(
                            BulkDocumentDeleteFailure(
                                document_id=document_id,
                                file_name=str(row.get("file_name") or ""),
                                error=str(
                                    result.get("message")
                                    or f"Object削除に失敗しました: {object_name}"
                                ),
                            )
                        )
            except Exception as error:
                failures.append(
                    BulkDocumentDeleteFailure(
                        document_id=document_id,
                        file_name=str(row.get("file_name") or ""),
                        error=str(error),
                    )
                )
        return BulkDocumentDeleteResult(
            success=not failures,
            deleted_count=len(deleted_ids),
            failed_count=len(failures),
            cleanup_warning_count=len(cleanup_warnings),
            deleted_document_ids=deleted_ids,
            failures=failures,
            cleanup_warnings=cleanup_warnings,
        )
    except Exception as error:
        _raise_api_error(error)


@router.get("/customer-name-suggestions", response_model=list[CustomerSuggestion])
def customer_name_suggestions(
    request: Request,
    q: str = Query(default="", max_length=400),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[CustomerSuggestion]:
    try:
        return document_metadata_repository.customer_suggestions(
            query=q,
            user_hash=_user_hash(request),
            limit=limit,
        )
    except Exception as error:
        _raise_api_error(error)


@router.get("/customer-name-normalize")
def normalize_customer(
    request: Request,
    value: str = Query(max_length=400),
) -> dict[str, Any]:
    try:
        ruleset = document_metadata_repository.resolve_ruleset()
        result = normalize_customer_name(
            value,
            suffixes=ruleset.config.customer_suffixes,
            version=ruleset.config.normalization_version,
        )
        suggestions = document_metadata_repository.customer_suggestions(
            query=result.search_key,
            user_hash=_user_hash(request),
            limit=20,
        )
        similar_names = [
            suggestion.value
            for suggestion in suggestions
            if suggestion.value != result.raw
            and (
                suggestion.search_key == result.search_key
                or suggestion.normalized == result.normalized
            )
        ][:10]
        return {
            "raw": result.raw,
            "normalized": result.normalized,
            "search_key": result.search_key,
            "normalization_version": result.version,
            "similar_names": similar_names,
            "similarity_warning": bool(similar_names),
        }
    except Exception as error:
        _raise_api_error(error)


@router.get(
    "/migration/existing-objects/preview",
    response_model=ExistingObjectMigrationPreview,
)
async def preview_existing_object_migration(
    max_candidates: int = Query(default=200, ge=1, le=1000),
) -> ExistingObjectMigrationPreview:
    """Preview unregistered source objects without issuing per-object HEAD calls."""
    try:
        bucket, namespace, objects, scan_truncated = await asyncio.to_thread(
            _scan_existing_objects
        )
        sources, protected_count = filter_existing_source_objects(objects)
        source_by_name = {
            str(item.get("name") or ""): item
            for item in sources
            if item.get("name")
        }
        registered = await asyncio.to_thread(
            document_metadata_repository.registered_object_names,
            list(source_by_name),
        )
        unregistered = [
            item
            for name, item in source_by_name.items()
            if name not in registered
        ]
        unregistered.sort(key=lambda item: str(item.get("name") or ""))
        visible = unregistered[:max_candidates]
        return ExistingObjectMigrationPreview(
            bucket=bucket,
            namespace=namespace,
            scanned_object_count=len(objects),
            source_object_count=len(source_by_name),
            registered_object_count=len(registered),
            unregistered_object_count=len(unregistered),
            protected_internal_count=protected_count,
            scan_truncated=scan_truncated,
            candidates_truncated=len(unregistered) > len(visible),
            candidates=[
                ExistingObjectMigrationCandidate(
                    object_name=str(item["name"]),
                    original_filename=PurePosixPath(str(item["name"])).name,
                    size=int(item.get("size") or 0),
                    time_created=(
                        str(item["time_created"])
                        if item.get("time_created")
                        else None
                    ),
                )
                for item in visible
            ],
        )
    except Exception as error:
        _raise_api_error(error)


@router.post(
    "/migration/existing-objects/apply",
    response_model=ExistingObjectMigrationResult,
)
async def apply_existing_object_migration(
    payload: ExistingObjectMigrationRequest,
) -> ExistingObjectMigrationResult:
    """Register legacy objects in-place under Unclassified; never rename or index."""
    try:
        bucket, namespace, objects, scan_truncated = await asyncio.to_thread(
            _scan_existing_objects
        )
        if scan_truncated:
            raise ValueError(
                "Object一覧がMAX_OBJECTS_FETCHを超えました。上限を増やして再実行してください"
            )
        sources, _ = filter_existing_source_objects(objects)
        source_by_name = {
            str(item.get("name") or ""): item
            for item in sources
            if item.get("name")
        }
        if payload.object_names is None:
            selected_names = list(source_by_name)
        else:
            selected_names = list(dict.fromkeys(payload.object_names))
            missing = [name for name in selected_names if name not in source_by_name]
            if missing:
                raise ValueError(
                    "移行対象ではないObjectが含まれています: "
                    + ", ".join(missing[:10])
                )
        registered = await asyncio.to_thread(
            document_metadata_repository.registered_object_names,
            selected_names,
        )
        skipped_count = len(registered)
        imported_ids: list[str] = []
        failures: list[dict[str, str]] = []
        for object_name in selected_names:
            if object_name in registered:
                continue
            item = source_by_name[object_name]
            metadata = await asyncio.to_thread(
                oci_service.get_object_metadata,
                bucket,
                namespace,
                object_name,
            )
            original_filename = PurePosixPath(object_name).name
            media_type: str | None = None
            file_size = int(item.get("size") or 0)
            if metadata.get("success"):
                original_filename = str(
                    metadata.get("original_filename") or original_filename
                )
                media_type = metadata.get("content_type")
                try:
                    file_size = int(metadata.get("content_length") or file_size)
                except (TypeError, ValueError):
                    pass
            document_id = uuid4().hex
            try:
                await asyncio.to_thread(
                    document_metadata_repository.register_uploaded_document,
                    document_id=document_id,
                    bucket=bucket,
                    object_name=object_name,
                    original_filename=original_filename,
                    media_type=media_type,
                    file_size=file_size,
                    folder_id=UNCLASSIFIED_FOLDER_ID,
                    storage_key_version=1,
                    status="UNPROCESSED",
                    metadata_source="MIGRATION",
                )
                imported_ids.append(document_id)
            except Exception as error:
                now_registered = await asyncio.to_thread(
                    document_metadata_repository.registered_object_names,
                    [object_name],
                )
                if object_name in now_registered:
                    skipped_count += 1
                else:
                    failures.append(
                        {"object_name": object_name, "error": str(error)}
                    )
        return ExistingObjectMigrationResult(
            success=not failures,
            imported_count=len(imported_ids),
            skipped_count=skipped_count,
            failed_count=len(failures),
            imported_document_ids=imported_ids,
            failures=failures,
            object_names_changed=False,
            reindex_queued=False,
        )
    except Exception as error:
        _raise_api_error(error)


@router.get("/settings/tag-groups", response_model=list[TagGroup])
def list_tag_groups() -> list[TagGroup]:
    try:
        return document_metadata_repository.list_tag_groups()
    except Exception as error:
        _raise_api_error(error)


@router.post("/settings/tag-groups", response_model=TagGroup, status_code=201)
def create_tag_group(payload: TagGroupUpsert) -> TagGroup:
    try:
        return document_metadata_repository.upsert_tag_group(payload)
    except Exception as error:
        _raise_api_error(error)


@router.put("/settings/tag-groups/{group_id}", response_model=TagGroup)
def update_tag_group(group_id: str, payload: TagGroupUpsert) -> TagGroup:
    try:
        return document_metadata_repository.upsert_tag_group(payload, group_id)
    except Exception as error:
        _raise_api_error(error)


@router.get("/settings/tags", response_model=list[TagDefinition])
def list_tags() -> list[TagDefinition]:
    try:
        return document_metadata_repository.list_tags()
    except Exception as error:
        _raise_api_error(error)


@router.post("/settings/tags", response_model=TagDefinition, status_code=201)
def create_tag(payload: TagUpsert) -> TagDefinition:
    try:
        return document_metadata_repository.upsert_tag(payload)
    except Exception as error:
        _raise_api_error(error)


@router.put("/settings/tags/{tag_id}", response_model=TagDefinition)
def update_tag(tag_id: str, payload: TagUpsert) -> TagDefinition:
    try:
        return document_metadata_repository.upsert_tag(payload, tag_id)
    except Exception as error:
        _raise_api_error(error)


@router.get(
    "/settings/search-concepts", response_model=SearchConceptSettings
)
def get_search_concept_settings() -> SearchConceptSettings:
    try:
        return document_metadata_repository.get_concept_settings()
    except Exception as error:
        _raise_api_error(error)


@router.put(
    "/settings/search-concepts", response_model=SearchConceptSettings
)
def update_search_concept_settings(
    payload: SearchConceptSettings,
) -> SearchConceptSettings:
    try:
        return document_metadata_repository.update_concept_settings(payload)
    except Exception as error:
        _raise_api_error(error)


@router.patch(
    "/settings/search-concepts/bulk-status",
    response_model=list[SearchConcept],
)
def update_search_concept_statuses(
    payload: SearchConceptBulkStatusUpdate,
) -> list[SearchConcept]:
    try:
        return document_metadata_repository.update_search_concept_statuses(
            payload
        )
    except Exception as error:
        _raise_api_error(error)


@router.patch(
    "/settings/search-concepts/{concept_id}", response_model=SearchConcept
)
def update_search_concept(
    concept_id: str, payload: SearchConceptUpdate
) -> SearchConcept:
    try:
        return document_metadata_repository.update_search_concept(
            concept_id, payload
        )
    except Exception as error:
        _raise_api_error(error)


@router.get("/settings/rulesets", response_model=list[ClassificationRuleSet])
def list_rulesets() -> list[ClassificationRuleSet]:
    try:
        return document_metadata_repository.list_rulesets()
    except Exception as error:
        _raise_api_error(error)


@router.post("/settings/rulesets", response_model=ClassificationRuleSet, status_code=201)
def create_ruleset(payload: ClassificationRuleSetUpsert) -> ClassificationRuleSet:
    try:
        return document_metadata_repository.upsert_ruleset(payload)
    except Exception as error:
        _raise_api_error(error)


@router.put("/settings/rulesets/{ruleset_id}", response_model=ClassificationRuleSet)
def update_ruleset(
    ruleset_id: str, payload: ClassificationRuleSetUpsert
) -> ClassificationRuleSet:
    try:
        return document_metadata_repository.upsert_ruleset(payload, ruleset_id)
    except Exception as error:
        _raise_api_error(error)


@router.get("/settings/folder-profiles", response_model=list[FolderRuleProfile])
def list_folder_profiles() -> list[FolderRuleProfile]:
    try:
        return document_metadata_repository.list_folder_rule_profiles()
    except Exception as error:
        _raise_api_error(error)


@router.put(
    "/settings/folder-profiles/{folder_id}", response_model=FolderRuleProfile
)
def update_folder_profile(
    folder_id: str, payload: FolderRuleProfileUpsert
) -> FolderRuleProfile:
    try:
        return document_metadata_repository.upsert_folder_rule_profile(
            folder_id, payload
        )
    except Exception as error:
        _raise_api_error(error)


@router.post("/settings/rules/test", response_model=list[RuleEvaluation])
def test_rules(payload: RuleTestRequest) -> list[RuleEvaluation]:
    try:
        tags = document_metadata_repository.list_tags()
        return [
            classify_filename(filename, config=payload.config, tags=tags)
            for filename in payload.filenames
        ]
    except Exception as error:
        _raise_api_error(error)


@router.post("/ingest/batches", response_model=IngestBatch, status_code=201)
def create_ingest_batch(payload: IngestBatchCreate, request: Request) -> IngestBatch:
    try:
        return document_metadata_repository.create_ingest_batch(
            target_folder_id=payload.target_folder_id,
            ruleset_id=payload.ruleset_id,
            created_by_hash=_user_hash(request),
        )
    except Exception as error:
        _raise_api_error(error)


@router.post("/ingest/batches/{batch_id}/items")
async def upload_ingest_items(
    batch_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    if not files or len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=422,
            detail=f"アップロード可能なファイル数は1〜{MAX_BATCH_FILES}件です",
        )
    try:
        await asyncio.to_thread(
            document_metadata_repository.require_ingest_batch_owner,
            batch_id,
            _user_hash(request),
        )
        batch, _ = await asyncio.to_thread(
            document_metadata_repository.list_ingest_batch, batch_id
        )
        ruleset = await asyncio.to_thread(
            document_metadata_repository.resolve_ruleset,
            ruleset_id=batch.ruleset_id,
        )
        tags = await asyncio.to_thread(document_metadata_repository.list_tags)
    except Exception as error:
        _raise_api_error(error)

    bucket = os.getenv("OCI_BUCKET") or ""
    if not bucket:
        raise HTTPException(status_code=503, detail="OCI_BUCKETが設定されていません")
    uploaded: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for file in files:
        filename = str(file.filename or "").strip()
        extension = PurePosixPath(filename).suffix.casefold().removeprefix(".")
        if not filename or extension not in ALLOWED_EXTENSIONS:
            failed.append({"filename": filename, "error": "未対応のファイル形式です"})
            continue
        content = await file.read(MAX_FILE_SIZE + 1)
        if not content or len(content) > MAX_FILE_SIZE:
            failed.append({"filename": filename, "error": "ファイルサイズが不正です"})
            continue
        document_id = uuid4().hex
        object_name = f"documents/{document_id}/source.{extension}"
        media_type = file.content_type or "application/octet-stream"
        put_ok = await asyncio.to_thread(
            oci_service.upload_file,
            content,
            object_name,
            media_type,
            filename,
            len(content),
        )
        if not put_ok:
            failed.append({"filename": filename, "error": "Object Storage保存に失敗しました"})
            continue
        try:
            await asyncio.to_thread(
                document_metadata_repository.register_uploaded_document,
                document_id=document_id,
                bucket=bucket,
                object_name=object_name,
                original_filename=filename,
                media_type=media_type,
                file_size=len(content),
                folder_id=batch.target_folder_id,
                status="DRAFT",
            )
            rule_result = classify_filename(
                filename,
                config=ruleset.config,
                tags=tags,
            )
            item_id = await asyncio.to_thread(
                document_metadata_repository.add_ingest_item,
                batch_id=batch_id,
                document_id=document_id,
                original_filename=filename,
                object_name=object_name,
                media_type=media_type,
                file_size=len(content),
                content_sha256=hashlib.sha256(content).hexdigest(),
                folder_id=batch.target_folder_id,
                rule_result=rule_result,
                ruleset_config=ruleset.config,
            )
            uploaded.append(
                {
                    "item_id": item_id,
                    "document_id": document_id,
                    "filename": filename,
                    "rule_result": rule_result.model_dump(mode="json"),
                }
            )
        except Exception as error:
            try:
                await asyncio.to_thread(oci_service.delete_object, object_name)
            except Exception:
                # Object creation itself may have failed, so best-effort cleanup
                # must not hide the original upload error or strand the batch.
                pass
            try:
                await asyncio.to_thread(
                    document_metadata_repository.discard_draft_document,
                    document_id,
                )
            except Exception:
                # Preserve the primary upload error for the user; the draft is
                # excluded from the library and can be cleaned by the audit job.
                pass
            failed.append({"filename": filename, "error": str(error)})
    batch, items = await asyncio.to_thread(
        document_metadata_repository.list_ingest_batch, batch_id
    )
    if not items:
        await asyncio.to_thread(
            document_metadata_repository.cancel_ingest_batch, batch_id
        )
        batch, items = await asyncio.to_thread(
            document_metadata_repository.list_ingest_batch, batch_id
        )
    return {
        "success": not failed,
        "batch": batch.model_dump(mode="json"),
        "items": [item.model_dump(mode="json") for item in items],
        "uploaded": uploaded,
        "failed": failed,
    }


@router.get("/ingest/active-batches", response_model=list[ActiveIngestBatch])
def list_active_ingest_batches(request: Request) -> list[ActiveIngestBatch]:
    try:
        return document_metadata_repository.list_active_ingest_batches(
            _user_hash(request)
        )
    except Exception as error:
        _raise_api_error(error)


@router.get("/ingest/batches/{batch_id}")
def get_ingest_batch(batch_id: str, request: Request) -> dict[str, Any]:
    try:
        document_metadata_repository.require_ingest_batch_owner(
            batch_id, _user_hash(request)
        )
        batch, items = document_metadata_repository.list_ingest_batch(batch_id)
        return {"batch": batch, "items": items}
    except Exception as error:
        _raise_api_error(error)


@router.delete("/ingest/batches/{batch_id}")
async def cancel_ingest_batch(batch_id: str, request: Request) -> dict[str, Any]:
    try:
        await asyncio.to_thread(
            document_metadata_repository.require_ingest_batch_owner,
            batch_id,
            _user_hash(request),
        )
        batch, items = await asyncio.to_thread(
            document_metadata_repository.list_ingest_batch, batch_id
        )
        await asyncio.to_thread(
            document_metadata_repository.ensure_ingest_batch_discardable,
            batch_id,
        )
        failed: list[str] = []
        for item in items:
            result = await asyncio.to_thread(
                oci_service.delete_object, item.object_name
            )
            if isinstance(result, dict) and not result.get("success"):
                failed.append(item.original_filename)
        if failed:
            raise RuntimeError(
                "一時Objectを削除できませんでした: " + ", ".join(failed)
            )
        object_names = await asyncio.to_thread(
            document_metadata_repository.cancel_ingest_batch, batch_id
        )
        return {
            "success": True,
            "batch_id": batch_id,
            "deleted_objects": len(object_names),
            "recoverable": False,
        }
    except Exception as error:
        _raise_api_error(error)


@router.post("/ingest/items/{item_id}/classify", response_model=IngestItemReview)
async def classify_ingest_item(item_id: str, request: Request) -> IngestItemReview:
    try:
        item = await asyncio.to_thread(document_metadata_repository.get_ingest_item, item_id)
        await asyncio.to_thread(
            document_metadata_repository.require_ingest_batch_owner,
            item.batch_id,
            _user_hash(request),
        )
        batch, _ = await asyncio.to_thread(
            document_metadata_repository.list_ingest_batch, item.batch_id
        )
        ruleset = await asyncio.to_thread(
            document_metadata_repository.resolve_ruleset,
            ruleset_id=batch.ruleset_id,
        )
        tags = await asyncio.to_thread(document_metadata_repository.list_tags)
        content = await asyncio.to_thread(oci_service.download_object, item.object_name)
        if not content:
            raise FileNotFoundError("ドラフトObjectを取得できません")
        result = await classify_document_preview(
            filename=item.original_filename,
            content=content,
            media_type=item.media_type or "application/octet-stream",
            config=ruleset.config,
            tags=tags,
        )
        raw = {
            **result.raw_llm_result,
            "preview": result.preview,
            "warnings": result.warnings,
            "degradations": result.degradations,
        }
        await asyncio.to_thread(
            document_metadata_repository.set_ingest_llm_result,
            item_id,
            candidates=result.llm_candidates,
            raw_result=raw,
            error_summary=" / ".join(result.degradations)[:2000] or None,
        )
        return await asyncio.to_thread(document_metadata_repository.get_ingest_item, item_id)
    except Exception as error:
        _raise_api_error(error)


@router.patch("/ingest/items/{item_id}", response_model=IngestItemReview)
def review_ingest_item(
    item_id: str, payload: IngestItemPatch, request: Request
) -> IngestItemReview:
    review = payload.model_dump(exclude={"expected_row_version"}, exclude_unset=True)
    try:
        item = document_metadata_repository.get_ingest_item(item_id)
        document_metadata_repository.require_ingest_batch_owner(
            item.batch_id, _user_hash(request)
        )
        return document_metadata_repository.save_ingest_review(
            item_id,
            review=review,
            expected_row_version=payload.expected_row_version,
        )
    except Exception as error:
        _raise_api_error(error)


@router.post("/ingest/batches/{batch_id}/confirm")
async def confirm_ingest_batch(batch_id: str, request: Request) -> dict[str, Any]:
    try:
        await asyncio.to_thread(
            document_metadata_repository.require_ingest_batch_owner,
            batch_id,
            _user_hash(request),
        )
        batch, items = await asyncio.to_thread(
            document_metadata_repository.list_ingest_batch, batch_id
        )
        pending = [
            item for item in items
            if item.state not in {"CONFIRMED", "REGISTERED", "INDEX_QUEUED", "INDEXED"}
        ]
        if pending:
            raise ValueError("未確認のファイルがあります")
        object_names: list[str] = []
        document_ids: list[str] = []
        for item in items:
            if item.state == "CONFIRMED":
                document_id, object_name = await asyncio.to_thread(
                    document_metadata_repository.commit_ingest_item,
                    item.item_id,
                    changed_by_hash=_user_hash(request),
                )
                document_ids.append(document_id)
                object_names.append(object_name)
            elif item.state == "REGISTERED":
                document_ids.append(item.document_id)
                object_names.append(item.object_name)
        if not object_names and batch.pipeline_job_id:
            return {
                "success": True,
                "batch_id": batch_id,
                "job_id": batch.pipeline_job_id,
                "document_ids": [item.document_id for item in items],
                "reused": True,
            }
        if not object_names:
            raise ValueError("索引対象の文書がありません")
        accepted = await asyncio.to_thread(
            create_pipeline_job,
            PipelineJobRequest(object_names=object_names, mode="FULL"),
            f"ingest-batch:{batch_id}",
        )
        await asyncio.to_thread(
            document_metadata_repository.mark_ingest_index_queued,
            batch_id,
            accepted.job_id,
        )
        return {
            "success": True,
            "batch_id": batch_id,
            "job_id": accepted.job_id,
            "document_ids": document_ids,
            "reused": accepted.reused,
        }
    except Exception as error:
        _raise_api_error(error)
