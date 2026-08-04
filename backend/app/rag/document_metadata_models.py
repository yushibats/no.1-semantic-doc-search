from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ROOT_FOLDER_ID = "folder_root"
UNCLASSIFIED_FOLDER_ID = "folder_unclassified"

MetadataSource = Literal[
    "AUTO_FILENAME",
    "RULE",
    "LLM",
    "USER",
    "FOLDER_DEFAULT",
    "MIGRATION",
]
DatePrecision = Literal["UNKNOWN", "YEAR", "YEAR_MONTH", "DAY"]
CandidateStatus = Literal["PENDING", "CONFIRMED", "REJECTED"]
ConceptFacet = Literal["BEFORE", "AFTER", "OTHER"]
ConceptStatus = Literal["PENDING", "ACTIVE", "HIDDEN", "MERGED"]
IngestState = Literal[
    "RECEIVED",
    "TEMP_STORED",
    "PREVIEW_EXTRACTING",
    "RULE_CLASSIFIED",
    "LLM_PENDING",
    "LLM_RUNNING",
    "LLM_SKIPPED",
    "REVIEW_REQUIRED",
    "CONFIRMED",
    "COMMITTING",
    "REGISTERED",
    "INDEX_QUEUED",
    "INDEXED",
    "PARTIAL_FAILED",
    "FAILED",
    "CANCELLED",
]


class FolderNode(BaseModel):
    folder_id: str
    parent_folder_id: str | None = None
    name: str
    normalized_name: str
    depth: int = 0
    is_system: bool = False
    document_count: int = 0
    descendant_document_count: int = 0
    children: list["FolderNode"] = Field(default_factory=list)


class FolderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=400)
    parent_folder_id: str = Field(default=ROOT_FOLDER_ID, max_length=64)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value or value in {".", ".."}:
            raise ValueError("フォルダ名が不正です")
        return value


class FolderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=400)
    parent_folder_id: str | None = Field(default=None, max_length=64)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value or value in {".", ".."}:
            raise ValueError("フォルダ名が不正です")
        return value


class TagGroupUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,62}$")
    name: str = Field(min_length=1, max_length=200)
    selection_mode: Literal["SINGLE", "MULTI"] = "MULTI"
    active: bool = True
    sort_order: int = Field(default=0, ge=-100_000, le=100_000)


class TagGroup(TagGroupUpsert):
    group_id: str


class TagUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(max_length=64)
    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,62}$")
    name: str = Field(min_length=1, max_length=200)
    active: bool = True
    sort_order: int = Field(default=0, ge=-100_000, le=100_000)


class TagDefinition(TagUpsert):
    tag_id: str
    group_code: str | None = None
    group_name: str | None = None
    selection_mode: Literal["SINGLE", "MULTI"] = "MULTI"


class DocumentTagAssignment(BaseModel):
    tag_id: str
    code: str = ""
    name: str = ""
    group_id: str = ""
    group_name: str = ""
    source: MetadataSource = "USER"
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: dict[str, Any] | None = None
    confirmed: bool = True
    user_locked: bool = True


class DocumentSetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=400)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("label")
    @classmethod
    def clean_label(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("案件グループ名を入力してください")
        return value


class DocumentSetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=400)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["ACTIVE", "ARCHIVED"] | None = None


class DocumentSet(BaseModel):
    document_set_id: str
    label: str
    normalized_label: str
    description: str | None = None
    status: Literal["ACTIVE", "ARCHIVED"] = "ACTIVE"
    document_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DocumentSetSuggestion(DocumentSet):
    score: float = Field(default=0, ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)
    requires_confirmation: bool = True


class DocumentMetadata(BaseModel):
    document_id: str
    folder_id: str = UNCLASSIFIED_FOLDER_ID
    folder_name: str = "未分類"
    document_set_id: str | None = None
    document_set_label: str | None = None
    document_year: int | None = Field(default=None, ge=1000, le=9999)
    document_month: int | None = Field(default=None, ge=1, le=12)
    date_precision: DatePrecision = "UNKNOWN"
    date_source: MetadataSource | None = None
    date_confirmed: bool = False
    customer_name_raw: str | None = Field(default=None, max_length=400)
    customer_name_normalized: str | None = Field(default=None, max_length=400)
    customer_name_search_key: str | None = Field(default=None, max_length=400)
    customer_source: MetadataSource | None = None
    customer_confirmed: bool = False
    customer_confidence: float | None = Field(default=None, ge=0, le=1)
    customer_normalization_version: int = 1
    row_version: int = 1
    tags: list[DocumentTagAssignment] = Field(default_factory=list)


class DocumentMetadataPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder_id: str | None = Field(default=None, max_length=64)
    document_set_id: str | None = Field(default=None, max_length=64)
    document_year: int | None = Field(default=None, ge=1000, le=9999)
    document_month: int | None = Field(default=None, ge=1, le=12)
    date_precision: DatePrecision | None = None
    date_source: MetadataSource | None = None
    date_confirmed: bool | None = None
    customer_name_raw: str | None = Field(default=None, max_length=400)
    customer_source: MetadataSource | None = None
    customer_confirmed: bool | None = None
    customer_confidence: float | None = Field(default=None, ge=0, le=1)
    tag_ids: list[str] | None = Field(default=None, max_length=200)
    expected_row_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_date(self) -> "DocumentMetadataPatch":
        fields = self.model_fields_set
        year = self.document_year
        month = self.document_month
        precision = self.date_precision
        if "document_month" in fields and month is not None and year is None and "document_year" in fields:
            raise ValueError("月を設定する場合は年も設定してください")
        if precision in {"YEAR_MONTH", "DAY"} and (year is None or month is None):
            raise ValueError("年月精度には年と月が必要です")
        if precision == "YEAR" and year is None:
            raise ValueError("年精度には年が必要です")
        return self


class BulkDocumentMetadataPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_ids: list[str] = Field(min_length=1, max_length=500)
    patch: DocumentMetadataPatch

class BulkDocumentSelection(BaseModel):
    """A bounded, de-duplicated set of registered documents for a bulk action."""

    model_config = ConfigDict(extra="forbid")

    document_ids: list[str] = Field(min_length=1, max_length=500)

    @field_validator("document_ids")
    @classmethod
    def normalize_document_ids(cls, values: list[str]) -> list[str]:
        normalized = list(
            dict.fromkeys(value.strip() for value in values if value.strip())
        )
        if not normalized:
            raise ValueError("操作する文書を選択してください")
        return normalized


class BulkDocumentDeleteFailure(BaseModel):
    document_id: str
    file_name: str | None = None
    error: str


class BulkDocumentDeleteResult(BaseModel):
    success: bool = True
    deleted_count: int = 0
    failed_count: int = 0
    cleanup_warning_count: int = 0
    deleted_document_ids: list[str] = Field(default_factory=list)
    failures: list[BulkDocumentDeleteFailure] = Field(default_factory=list)
    cleanup_warnings: list[BulkDocumentDeleteFailure] = Field(default_factory=list)


class DocumentLibraryItem(BaseModel):
    document_id: str
    bucket: str
    object_name: str
    file_name: str
    media_type: str | None = None
    file_size: int | None = None
    status: str
    uploaded_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: DocumentMetadata
    processing: dict[str, Any] | None = None


class DocumentLibraryResponse(BaseModel):
    success: bool = True
    items: list[DocumentLibraryItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class RuleCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename_exact: list[str] = Field(default_factory=list, max_length=200)
    all_terms: list[str] = Field(default_factory=list, max_length=200)
    any_terms: list[str] = Field(default_factory=list, max_length=200)
    exclude_terms: list[str] = Field(default_factory=list, max_length=200)
    extensions: list[str] = Field(default_factory=list, max_length=100)


class ClassificationRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    priority: int = Field(default=0, ge=-100_000, le=100_000)
    condition: RuleCondition = Field(default_factory=RuleCondition)
    tag_ids: list[str] = Field(default_factory=list, max_length=100)
    customer_pattern: str | None = Field(default=None, max_length=1000)


class ClassificationRuleSetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    normalization_version: int = Field(default=1, ge=1)
    customer_suffixes: list[str] = Field(default_factory=list, max_length=100)
    date_patterns: list[str] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "Optional strong filename regexes with named year/month/day groups. "
            "The date is accepted only when all three groups form a real date."
        ),
    )
    rules: list[ClassificationRule] = Field(default_factory=list, max_length=1000)
    llm_prompt: str = Field(
        default=(
            "ファイル名と文書の抜粋から未決定の属性だけを抽出してください。"
            "タグは提示された候補IDだけを使い、顧客名は根拠に明記された文字列だけを返してください。"
        ),
        min_length=1,
        max_length=40_000,
    )
    preview_page_limit: int = Field(default=3, ge=1, le=10)
    preview_text_limit: int = Field(default=12_000, ge=1000, le=100_000)


class ClassificationRuleSetUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,62}$")
    name: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    config: ClassificationRuleSetConfig


class ClassificationRuleSet(ClassificationRuleSetUpsert):
    ruleset_id: str
    revision_id: str
    revision_no: int
    config_hash: str


class FolderDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag_ids: list[str] = Field(default_factory=list, max_length=200)
    customer_name_raw: str | None = Field(default=None, max_length=400)
    document_year: int | None = Field(default=None, ge=1000, le=9999)
    document_month: int | None = Field(default=None, ge=1, le=12)
    date_precision: DatePrecision = "UNKNOWN"

    @model_validator(mode="after")
    def validate_date(self) -> "FolderDefaults":
        if self.document_month is not None and self.document_year is None:
            raise ValueError("フォルダ既定月には年が必要です")
        if self.date_precision in {"YEAR_MONTH", "DAY"} and (
            self.document_year is None or self.document_month is None
        ):
            raise ValueError("年月精度には年と月が必要です")
        if self.date_precision == "YEAR" and self.document_year is None:
            raise ValueError("年精度には年が必要です")
        return self


class FolderRuleProfileUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ruleset_id: str = Field(max_length=64)
    inherit_to_descendants: bool = True
    defaults: FolderDefaults = Field(default_factory=FolderDefaults)


class FolderRuleProfile(FolderRuleProfileUpsert):
    folder_id: str


class RuleCandidate(BaseModel):
    field_kind: Literal["TAG", "CUSTOMER", "DATE"]
    target_key: str | None = None
    value_raw: str
    value_normalized: str | None = None
    source: MetadataSource
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False
    ambiguous: bool = False


class RuleEvaluation(BaseModel):
    original_filename: str
    normalized_filename: str
    matched_rule_ids: list[str] = Field(default_factory=list)
    candidates: list[RuleCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RuleTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filenames: list[str] = Field(min_length=1, max_length=200)
    config: ClassificationRuleSetConfig


class CustomerSuggestion(BaseModel):
    value: str
    normalized: str
    search_key: str
    document_count: int
    last_used_at: datetime | None = None
    similarity_warning: bool = False


class IngestBatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_folder_id: str = Field(default=UNCLASSIFIED_FOLDER_ID, max_length=64)
    ruleset_id: str | None = Field(default=None, max_length=64)


class IngestBatch(BaseModel):
    batch_id: str
    status: str
    target_folder_id: str
    ruleset_id: str | None = None
    pipeline_job_id: str | None = None
    total_items: int = 0
    confirmed_items: int = 0
    failed_items: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ActiveIngestBatch(IngestBatch):
    target_folder_name: str
    analysis_completed_items: int = 0
    analysis_pending_items: int = 0
    registered_items: int = 0
    discardable: bool = False


class IngestItemReview(BaseModel):
    item_id: str
    batch_id: str
    document_id: str
    original_filename: str
    object_name: str
    media_type: str | None = None
    file_size: int
    state: IngestState
    folder_id: str
    rule_result: RuleEvaluation | None = None
    llm_result: dict[str, Any] | None = None
    review: dict[str, Any] = Field(default_factory=dict)
    candidates: list[RuleCandidate] = Field(default_factory=list)
    metadata: DocumentMetadata | None = None
    error_summary: str | None = None
    row_version: int = 1


class IngestItemPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder_id: str | None = Field(default=None, max_length=64)
    document_set_id: str | None = Field(default=None, max_length=64)
    customer_name_raw: str | None = Field(default=None, max_length=400)
    document_year: int | None = Field(default=None, ge=1000, le=9999)
    document_month: int | None = Field(default=None, ge=1, le=12)
    date_precision: DatePrecision | None = None
    tag_ids: list[str] | None = Field(default=None, max_length=200)
    expected_row_version: int = Field(ge=1)


class SearchConceptSettings(BaseModel):
    enabled: bool = True
    auto_publish: bool = True
    auto_publish_confidence: float = Field(default=0.85, ge=0, le=1)
    min_support_sets: int = Field(default=2, ge=1, le=1000)
    max_concepts_per_document: int = Field(default=16, ge=1, le=100)
    initial_display_limit: int = Field(default=8, ge=1, le=100)
    input_text_limit: int = Field(default=24_000, ge=1000, le=100_000)
    prompt_text: str = Field(min_length=1, max_length=40_000)
    taxonomy_revision: int = Field(default=1, ge=1)


class SearchConcept(BaseModel):
    concept_id: str
    facet: ConceptFacet
    category_code: str
    category_name: str
    display_label: str
    normalized_label: str
    status: ConceptStatus = "PENDING"
    merged_into_id: str | None = None
    support_document_count: int = 0
    support_set_count: int = 0


class SearchConceptUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_label: str | None = Field(default=None, min_length=1, max_length=400)
    category_code: str | None = Field(default=None, min_length=1, max_length=64)
    category_name: str | None = Field(default=None, min_length=1, max_length=200)
    facet: ConceptFacet | None = None
    status: ConceptStatus | None = None
    merged_into_id: str | None = Field(default=None, max_length=64)


class DocumentConcept(BaseModel):
    concept: SearchConcept
    confidence: float = Field(ge=0, le=1)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    source_kinds: list[str] = Field(default_factory=list)
    user_locked: bool = False


class ConceptExtractionItem(BaseModel):
    label: str = Field(min_length=1, max_length=400)
    facet: ConceptFacet
    category_code: str = Field(min_length=1, max_length=64)
    category_name: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0, le=1)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    source_kinds: list[str] = Field(default_factory=list, max_length=10)
    existing_concept_id: str | None = Field(default=None, max_length=64)


class ConceptExtractionOutput(BaseModel):
    concepts: list[ConceptExtractionItem] = Field(default_factory=list, max_length=100)


class ExistingObjectMigrationCandidate(BaseModel):
    object_name: str
    original_filename: str
    size: int = 0
    time_created: str | None = None


class ExistingObjectMigrationPreview(BaseModel):
    bucket: str
    namespace: str
    scanned_object_count: int
    source_object_count: int
    registered_object_count: int
    unregistered_object_count: int
    protected_internal_count: int
    scan_truncated: bool = False
    candidates_truncated: bool = False
    candidates: list[ExistingObjectMigrationCandidate] = Field(default_factory=list)


class ExistingObjectMigrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: Literal["IMPORT_EXISTING_OBJECTS_TO_UNCLASSIFIED"]
    object_names: list[str] | None = Field(default=None, max_length=10_000)


class ExistingObjectMigrationResult(BaseModel):
    success: bool = True
    imported_count: int
    skipped_count: int
    failed_count: int
    imported_document_ids: list[str] = Field(default_factory=list)
    failures: list[dict[str, str]] = Field(default_factory=list)
    object_names_changed: bool = False
    reindex_queued: bool = False
