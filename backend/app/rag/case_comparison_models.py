from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


PageContentKind = Literal[
    "FLOOR_PLAN",
    "SITE_PLAN",
    "ELEVATION",
    "AREA_TABLE",
    "PERSPECTIVE",
    "PHOTO",
    "OTHER",
]
PagePhase = Literal["EXISTING", "PROPOSED", "COMPLETED", "UNKNOWN"]
ClassificationSource = Literal["RULE", "VLM", "USER", "MIGRATION"]
FactPhase = Literal["COMMON", "EXISTING", "PROPOSED", "COMPLETED"]
FactCode = Literal[
    "BUILDING_TYPE",
    "STRUCTURE",
    "USE",
    "AREA_TYPE",
    "AREA_VALUE",
    "LAYOUT",
]


class PageReference(BaseModel):
    document_id: str = Field(min_length=1, max_length=64)
    revision_id: str = Field(min_length=1, max_length=64)
    page_number: int = Field(ge=1)


class PageClassification(PageReference):
    file_name: str = ""
    release_id: str | None = None
    artifact_id: str | None = None
    image_url: str | None = None
    content_kind: PageContentKind = "OTHER"
    phase: PagePhase = "UNKNOWN"
    floor_code: str | None = None
    plan_variant: str | None = None
    source: ClassificationSource = "RULE"
    confidence: float = Field(default=0.0, ge=0, le=1)
    confirmed: bool = False
    user_locked: bool = False
    evidence: dict = Field(default_factory=dict)
    updated_at: datetime | None = None


class PageClassificationPatch(BaseModel):
    content_kind: PageContentKind
    phase: PagePhase
    floor_code: str | None = Field(default=None, max_length=32)
    plan_variant: str | None = Field(default=None, max_length=64)
    confirmed: bool = True


class DocumentSetFact(BaseModel):
    fact_id: str
    document_set_id: str
    fact_code: FactCode
    phase: FactPhase = "COMMON"
    value_text: str | None = None
    value_number: float | None = None
    unit: str | None = None
    source: ClassificationSource = "RULE"
    confidence: float = Field(default=0.0, ge=0, le=1)
    confirmed: bool = False
    user_locked: bool = False
    evidence_document_id: str | None = None
    evidence_revision_id: str | None = None
    evidence_page_number: int | None = None
    evidence: dict = Field(default_factory=dict)
    updated_at: datetime | None = None


class DocumentSetFactInput(BaseModel):
    fact_code: FactCode
    phase: FactPhase = "COMMON"
    value_text: str | None = Field(default=None, max_length=400)
    value_number: float | None = None
    unit: str | None = Field(default=None, max_length=32)
    confirmed: bool = True
    evidence_document_id: str | None = Field(default=None, max_length=64)
    evidence_revision_id: str | None = Field(default=None, max_length=64)
    evidence_page_number: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_value(self) -> "DocumentSetFactInput":
        if not (self.value_text or "").strip() and self.value_number is None:
            raise ValueError("value_textまたはvalue_numberを指定してください")
        return self


class DocumentSetFactsPatch(BaseModel):
    items: list[DocumentSetFactInput] = Field(default_factory=list, max_length=50)


class ComparisonSelectionRequest(BaseModel):
    before: PageReference
    after: PageReference
    floor_code: str | None = Field(default=None, max_length=32)
    plan_variant: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def different_pages(self) -> "ComparisonSelectionRequest":
        if self.before == self.after:
            raise ValueError("現況と提案には異なるページを指定してください")
        return self


class ComparisonPair(BaseModel):
    pair_id: str | None = None
    document_set_id: str
    floor_code: str | None = None
    plan_variant: str | None = None
    before: PageClassification | None = None
    after: PageClassification | None = None
    source: Literal["AUTO", "USER"] = "AUTO"
    user_locked: bool = False
    complete: bool = False
    missing_reason: str | None = None


class DocumentSetComparison(BaseModel):
    document_set_id: str
    label: str
    pair: ComparisonPair
    all_pages: list[PageClassification] = Field(default_factory=list)
    before_candidates: list[PageClassification] = Field(default_factory=list)
    after_candidates: list[PageClassification] = Field(default_factory=list)
    facts: list[DocumentSetFact] = Field(default_factory=list)


class ComparisonAnalysisRequest(ComparisonSelectionRequest):
    force: bool = False


class ComparisonAnalysis(BaseModel):
    analysis_id: str
    document_set_id: str
    pair_id: str | None = None
    status: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED"]
    cached: bool = False
    prompt_version: str
    result: dict | None = None
    error_summary: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BuildingConditionOptions(BaseModel):
    building_types: list[str] = Field(default_factory=list)
    structures: list[str] = Field(default_factory=list)
    uses: list[str] = Field(default_factory=list)
    area_types: list[str] = Field(default_factory=list)
    existing_layouts: list[str] = Field(default_factory=list)
    proposed_layouts: list[str] = Field(default_factory=list)
