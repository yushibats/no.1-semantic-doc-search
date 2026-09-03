from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import Any, Iterator
from uuid import uuid4

from app.rag.case_classifier import (
    classify_page,
    extract_set_areas,
    extract_set_facts,
)
from app.rag.case_comparison_models import (
    BuildingConditionOptions,
    ComparisonAnalysis,
    ComparisonPair,
    ComparisonSelectionRequest,
    DocumentSetComparison,
    DocumentSetFact,
    DocumentSetFactInput,
    PageClassification,
    PageClassificationPatch,
    PageReference,
)
from app.rag.document_metadata_schema import document_library_schema_status
from app.services.database_service import database_service


PROMPT_VERSION = "20260902_001"


def _lob_text(value: object) -> str:
    if value is None:
        return ""
    if hasattr(value, "read"):
        value = value.read()
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _json_value(value: object, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    text = _lob_text(value).strip()
    return json.loads(text) if text else default


class CaseComparisonRepository:
    @contextmanager
    def connection(self) -> Iterator[Any]:
        if not database_service._ensure_pool_initialized():
            raise RuntimeError("database connection is not configured")
        with database_service.pool_manager.acquire_connection() as connection:
            yield connection

    @staticmethod
    def rows(cursor: Any) -> list[dict[str, Any]]:
        columns = [item[0].lower() for item in cursor.description or []]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def require_schema(self) -> None:
        status = document_library_schema_status()
        if not status["ready"]:
            raise RuntimeError("document library schema is not initialized")

    @staticmethod
    def _access_sql(alias: str = "d") -> str:
        return f"""
            EXISTS (
                SELECT 1 FROM sds_document_acl access_acl
                WHERE access_acl.document_id={alias}.document_id
                  AND (
                    (:user_hash IS NOT NULL
                     AND access_acl.principal_type='public_authenticated')
                    OR (:user_hash IS NOT NULL
                        AND access_acl.principal_type IN ('user','service')
                        AND access_acl.principal_hash=:user_hash)
                  )
            )
        """

    def _set_label(self, document_set_id: str, user_hash: str | None) -> str:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT ds.label
                FROM sds_document_sets ds
                WHERE ds.document_set_id=:document_set
                  AND ds.status='ACTIVE'
                  AND EXISTS (
                    SELECT 1
                    FROM sds_document_metadata dm
                    JOIN sds_documents d ON d.document_id=dm.document_id
                    WHERE dm.document_set_id=ds.document_set_id
                      AND d.is_current=1
                      AND {self._access_sql('d')}
                  )
                """,
                {"document_set": document_set_id, "user_hash": user_hash},
            )
            row = cursor.fetchone()
        if not row:
            raise LookupError("案件グループが見つからないか参照権限がありません")
        return str(row[0])

    def _document_snapshot(self, document_id: str) -> dict[str, Any]:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.document_id, d.file_name, d.current_revision_id,
                       d.serving_release_id, dm.document_set_id
                FROM sds_documents d
                JOIN sds_document_metadata dm ON dm.document_id=d.document_id
                WHERE d.document_id=:document AND d.is_current=1
                  AND d.serving_release_id IS NOT NULL
                """,
                {"document": document_id},
            )
            rows = self.rows(cursor)
            if not rows:
                raise LookupError("公開済み文書が見つかりません")
            document = rows[0]
            cursor.execute(
                """
                SELECT t.code
                FROM sds_document_tags dt
                JOIN sds_tags t ON t.tag_id=dt.tag_id
                WHERE dt.document_id=:document AND dt.confirmed=1
                """,
                {"document": document_id},
            )
            document["tag_codes"] = {str(row[0]) for row in cursor.fetchall()}
            cursor.execute(
                """
                SELECT a.page_number, a.artifact_kind, a.raw_text, c.component_key
                FROM sds_index_release_components c
                JOIN sds_artifacts a ON a.stage_run_id=c.stage_run_id
                WHERE c.release_id=:release
                  AND a.artifact_kind IN
                      ('PAGE_IMAGE','PAGE_TEXT','NATIVE_TEXT','MINERU_TEXT','OCR_TEXT','VLM_TEXT')
                ORDER BY a.page_number, a.artifact_kind
                """,
                {"release": document["serving_release_id"]},
            )
            document["artifacts"] = self.rows(cursor)
        return document

    def refresh_document(self, document_id: str) -> dict[str, int]:
        """Refresh page roles and conservative typed facts for a serving revision."""

        self.require_schema()
        document = self._document_snapshot(document_id)
        pages: dict[int, dict[str, list[str] | bool]] = {}
        for artifact in document["artifacts"]:
            if artifact.get("page_number") is None:
                continue
            page_no = int(artifact["page_number"])
            page = pages.setdefault(
                page_no,
                {"rendered": False, "page": [], "fallback": [], "vlm": []},
            )
            kind = str(artifact["artifact_kind"])
            if kind == "PAGE_IMAGE":
                page["rendered"] = True
            elif kind == "VLM_TEXT":
                page["vlm"].append(_lob_text(artifact.get("raw_text"))[:30000])
            elif kind == "PAGE_TEXT":
                page["page"].append(_lob_text(artifact.get("raw_text"))[:30000])
            elif kind in {"NATIVE_TEXT", "MINERU_TEXT", "OCR_TEXT"}:
                page["fallback"].append(_lob_text(artifact.get("raw_text"))[:30000])

        classifications = 0
        facts_written = 0
        areas_written = 0
        with self.connection() as connection, connection.cursor() as cursor:
            if document.get("document_set_id"):
                # Re-analysis must not leave an obsolete automatic area as an
                # active search condition. User-confirmed rows are never changed.
                cursor.execute(
                    """
                    UPDATE sds_document_set_areas
                    SET confirmed=0, updated_at=SYSTIMESTAMP
                    WHERE evidence_document_id=:document
                      AND user_locked=0
                    """,
                    {"document": document_id},
                )
            for page_number, page in pages.items():
                if not page["rendered"]:
                    continue
                normalized_text = "\n".join(page["page"]).strip()
                if not normalized_text:
                    normalized_text = "\n".join(page["fallback"]).strip()
                vlm_text = "\n".join(page["vlm"]).strip()
                inference = classify_page(
                    file_name=str(document["file_name"]),
                    page_text=normalized_text,
                    vlm_text=vlm_text,
                    tag_codes=document["tag_codes"],
                )
                cursor.execute(
                    """
                    MERGE INTO sds_page_classifications target
                    USING (SELECT :revision revision_id, :page_number page_number FROM dual) source
                    ON (target.document_revision_id=source.revision_id
                        AND target.page_number=source.page_number)
                    WHEN MATCHED THEN UPDATE SET
                        target.document_id=:document,
                        target.content_kind=:content_kind,
                        target.phase=:phase,
                        target.floor_code=:floor_code,
                        target.plan_variant=:plan_variant,
                        target.source=:source,
                        target.confidence=:confidence,
                        target.confirmed=:confirmed,
                        target.evidence_json=:evidence,
                        target.updated_at=SYSTIMESTAMP
                    WHERE target.user_locked=0
                    WHEN NOT MATCHED THEN INSERT
                        (document_revision_id, page_number, document_id, content_kind,
                         phase, floor_code, plan_variant, source, confidence, confirmed,
                         user_locked, evidence_json)
                    VALUES
                        (:revision, :page_number, :document, :content_kind, :phase,
                         :floor_code, :plan_variant, :source, :confidence, :confirmed,
                         0, :evidence)
                    """,
                    {
                        "revision": str(document["current_revision_id"]),
                        "page_number": page_number,
                        "document": document_id,
                        "content_kind": inference.content_kind,
                        "phase": inference.phase,
                        "floor_code": inference.floor_code,
                        "plan_variant": inference.plan_variant,
                        "source": inference.source,
                        "confidence": inference.confidence,
                        "confirmed": int(inference.confirmed),
                        "evidence": json.dumps(inference.evidence, ensure_ascii=False),
                    },
                )
                classifications += 1
                if not document.get("document_set_id"):
                    continue
                for fact in extract_set_facts(
                    file_name=str(document["file_name"]),
                    page_text=normalized_text,
                    vlm_text=vlm_text,
                    page_phase=inference.phase,
                ):
                    cursor.execute(
                        """
                        MERGE INTO sds_document_set_facts target
                        USING (SELECT :document_set document_set_id, :fact_code fact_code,
                                      :phase phase FROM dual) source
                        ON (target.document_set_id=source.document_set_id
                            AND target.fact_code=source.fact_code AND target.phase=source.phase)
                        WHEN MATCHED THEN UPDATE SET
                            target.value_text=:value_text,
                            target.value_number=:value_number,
                            target.unit=:unit,
                            target.source=:source,
                            target.confidence=:confidence,
                            target.confirmed=:confirmed,
                            target.evidence_document_id=:document,
                            target.evidence_revision_id=:revision,
                            target.evidence_page_number=:page_number,
                            target.evidence_json=:evidence,
                            target.updated_at=SYSTIMESTAMP
                        WHERE target.user_locked=0
                          AND (:confirmed=1 OR target.confirmed=0)
                        WHEN NOT MATCHED THEN INSERT
                            (fact_id, document_set_id, fact_code, phase, value_text,
                             value_number, unit, source, confidence, confirmed, user_locked,
                             evidence_document_id, evidence_revision_id,
                             evidence_page_number, evidence_json)
                        VALUES
                            (:fact_id, :document_set, :fact_code, :phase, :value_text,
                             :value_number, :unit, :source, :confidence, :confirmed, 0,
                             :document, :revision, :page_number, :evidence)
                        """,
                        {
                            "fact_id": uuid4().hex,
                            "document_set": str(document["document_set_id"]),
                            "fact_code": fact.fact_code,
                            "phase": fact.phase,
                            "value_text": fact.value_text,
                            "value_number": fact.value_number,
                            "unit": fact.unit,
                            "source": fact.source,
                            "confidence": fact.confidence,
                            "confirmed": int(fact.confirmed),
                            "document": document_id,
                            "revision": str(document["current_revision_id"]),
                            "page_number": page_number,
                            "evidence": json.dumps(fact.evidence, ensure_ascii=False),
                        },
                    )
                    facts_written += 1
                for area in extract_set_areas(
                    file_name=str(document["file_name"]),
                    page_text=normalized_text,
                    vlm_text=vlm_text,
                    page_phase=inference.phase,
                ):
                    cursor.execute(
                        """
                        MERGE INTO sds_document_set_areas target
                        USING (
                            SELECT :document_set document_set_id, :phase phase,
                                   :area_type area_type, :area_value area_value,
                                   :unit unit
                            FROM dual
                        ) source
                        ON (
                            target.document_set_id=source.document_set_id
                            AND target.phase=source.phase
                            AND target.area_type=source.area_type
                            AND target.area_value=source.area_value
                            AND target.unit=source.unit
                        )
                        WHEN MATCHED THEN UPDATE SET
                            target.source=:source,
                            target.confidence=:confidence,
                            target.confirmed=:confirmed,
                            target.evidence_document_id=:document,
                            target.evidence_revision_id=:revision,
                            target.evidence_page_number=:page_number,
                            target.evidence_json=:evidence,
                            target.updated_at=SYSTIMESTAMP
                        WHERE target.user_locked=0
                          AND (:confirmed=1 OR target.confirmed=0)
                        WHEN NOT MATCHED THEN INSERT
                            (area_id, document_set_id, phase, area_type,
                             area_value, unit, source, confidence, confirmed,
                             user_locked, evidence_document_id,
                             evidence_revision_id, evidence_page_number,
                             evidence_json)
                        VALUES
                            (:area_id, :document_set, :phase, :area_type,
                             :area_value, :unit, :source, :confidence,
                             :confirmed, 0, :document, :revision,
                             :page_number, :evidence)
                        """,
                        {
                            "area_id": uuid4().hex,
                            "document_set": str(document["document_set_id"]),
                            "phase": area.phase,
                            "area_type": area.area_type,
                            "area_value": area.value,
                            "unit": area.unit,
                            "source": area.source,
                            "confidence": area.confidence,
                            "confirmed": int(area.confirmed),
                            "document": document_id,
                            "revision": str(document["current_revision_id"]),
                            "page_number": page_number,
                            "evidence": json.dumps(
                                area.evidence, ensure_ascii=False
                            ),
                        },
                    )
                    areas_written += 1
            connection.commit()
        return {
            "classifications": classifications,
            "facts": facts_written,
            "areas": areas_written,
        }

    def refresh_set(self, document_set_id: str, user_hash: str | None) -> dict[str, int]:
        self._set_label(document_set_id, user_hash)
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT d.document_id
                FROM sds_document_metadata dm
                JOIN sds_documents d ON d.document_id=dm.document_id
                WHERE dm.document_set_id=:document_set AND d.is_current=1
                  AND d.serving_release_id IS NOT NULL
                  AND {self._access_sql('d')}
                """,
                {"document_set": document_set_id, "user_hash": user_hash},
            )
            document_ids = [str(row[0]) for row in cursor.fetchall()]
        total = {"documents": 0, "classifications": 0, "facts": 0, "areas": 0}
        for document_id in document_ids:
            result = self.refresh_document(document_id)
            total["documents"] += 1
            total["classifications"] += result["classifications"]
            total["facts"] += result["facts"]
            total["areas"] += result["areas"]
        return total

    @staticmethod
    def _page_from_row(row: dict[str, Any]) -> PageClassification:
        document_id = str(row["document_id"])
        release_id = str(row["release_id"])
        artifact_id = str(row["artifact_id"])
        return PageClassification(
            document_id=document_id,
            revision_id=str(row["document_revision_id"]),
            page_number=int(row["page_number"]),
            file_name=str(row["file_name"]),
            release_id=release_id,
            artifact_id=artifact_id,
            image_url=(
                f"/documents/{document_id}/releases/{release_id}/"
                f"page-images/{artifact_id}/content"
            ),
            content_kind=str(row["content_kind"]),
            phase=str(row["phase"]),
            floor_code=str(row["floor_code"]) if row.get("floor_code") else None,
            plan_variant=str(row["plan_variant"]) if row.get("plan_variant") else None,
            source=str(row["source"]),
            confidence=float(row["confidence"] or 0),
            confirmed=bool(row["confirmed"]),
            user_locked=bool(row["user_locked"]),
            evidence=_json_value(row.get("evidence_json"), {}),
            updated_at=row.get("updated_at"),
        )

    def page_candidates(
        self, document_set_id: str, user_hash: str | None
    ) -> list[PageClassification]:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT pc.document_revision_id, pc.page_number, pc.document_id,
                       pc.content_kind, pc.phase, pc.floor_code, pc.plan_variant,
                       pc.source, pc.confidence, pc.confirmed, pc.user_locked,
                       pc.evidence_json, pc.updated_at,
                       d.file_name, d.serving_release_id release_id,
                       image.artifact_id
                FROM sds_page_classifications pc
                JOIN sds_documents d
                  ON d.document_id=pc.document_id
                 AND d.current_revision_id=pc.document_revision_id
                 AND d.is_current=1
                JOIN sds_document_metadata dm ON dm.document_id=d.document_id
                JOIN sds_index_release_components render
                  ON render.release_id=d.serving_release_id
                 AND render.component_key='render'
                JOIN sds_artifacts image
                  ON image.stage_run_id=render.stage_run_id
                 AND image.document_revision_id=pc.document_revision_id
                 AND image.page_number=pc.page_number
                 AND image.artifact_kind='PAGE_IMAGE'
                WHERE dm.document_set_id=:document_set
                  AND {self._access_sql('d')}
                ORDER BY pc.confirmed DESC, pc.confidence DESC,
                         d.file_name, pc.page_number
                """,
                {"document_set": document_set_id, "user_hash": user_hash},
            )
            return [self._page_from_row(row) for row in self.rows(cursor)]

    def facts(self, document_set_id: str) -> list[DocumentSetFact]:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT fact_id, document_set_id, fact_code, phase, value_text,
                       value_number, unit, source, confidence, confirmed, user_locked,
                       evidence_document_id, evidence_revision_id,
                       evidence_page_number, evidence_json, updated_at
                FROM sds_document_set_facts
                WHERE document_set_id=:document_set
                ORDER BY fact_code, phase
                """,
                {"document_set": document_set_id},
            )
            return [
                DocumentSetFact(
                    fact_id=str(row["fact_id"]),
                    document_set_id=str(row["document_set_id"]),
                    fact_code=str(row["fact_code"]),
                    phase=str(row["phase"]),
                    value_text=str(row["value_text"]) if row.get("value_text") else None,
                    value_number=float(row["value_number"]) if row.get("value_number") is not None else None,
                    unit=str(row["unit"]) if row.get("unit") else None,
                    source=str(row["source"]),
                    confidence=float(row["confidence"] or 0),
                    confirmed=bool(row["confirmed"]),
                    user_locked=bool(row["user_locked"]),
                    evidence_document_id=str(row["evidence_document_id"]) if row.get("evidence_document_id") else None,
                    evidence_revision_id=str(row["evidence_revision_id"]) if row.get("evidence_revision_id") else None,
                    evidence_page_number=int(row["evidence_page_number"]) if row.get("evidence_page_number") else None,
                    evidence=_json_value(row.get("evidence_json"), {}),
                    updated_at=row.get("updated_at"),
                )
                for row in self.rows(cursor)
            ]

    @staticmethod
    def _same_reference(page: PageClassification, reference: PageReference) -> bool:
        return (
            page.document_id == reference.document_id
            and page.revision_id == reference.revision_id
            and page.page_number == reference.page_number
        )

    def comparison(
        self, document_set_id: str, user_hash: str | None, *, refresh: bool = False
    ) -> DocumentSetComparison:
        self.require_schema()
        label = self._set_label(document_set_id, user_hash)
        if refresh:
            self.refresh_set(document_set_id, user_hash)
        candidates = self.page_candidates(document_set_id, user_hash)
        before_candidates = [
            item for item in candidates
            if item.confirmed
            and item.content_kind == "FLOOR_PLAN"
            and item.phase == "EXISTING"
        ]
        after_candidates = [
            item for item in candidates
            if item.confirmed
            and item.content_kind == "FLOOR_PLAN"
            and item.phase == "PROPOSED"
        ]

        saved: dict[str, Any] | None = None
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM sds_comparison_pairs WHERE document_set_id=:document_set",
                {"document_set": document_set_id},
            )
            rows = self.rows(cursor)
            saved = rows[0] if rows else None

        before: PageClassification | None = None
        after: PageClassification | None = None
        source = "AUTO"
        pair_id: str | None = None
        floor_code: str | None = None
        plan_variant: str | None = None
        if saved:
            pair_id = str(saved["pair_id"])
            source = str(saved["source"])
            floor_code = str(saved["floor_code"]) if saved.get("floor_code") else None
            plan_variant = str(saved["plan_variant"]) if saved.get("plan_variant") else None
            before_ref = PageReference(
                document_id=str(saved["before_document_id"]),
                revision_id=str(saved["before_revision_id"]),
                page_number=int(saved["before_page_number"]),
            )
            after_ref = PageReference(
                document_id=str(saved["after_document_id"]),
                revision_id=str(saved["after_revision_id"]),
                page_number=int(saved["after_page_number"]),
            )
            before = next((item for item in before_candidates if self._same_reference(item, before_ref)), None)
            after = next((item for item in after_candidates if self._same_reference(item, after_ref)), None)
            if not before or not after:
                pair_id = None
                source = "AUTO"
                floor_code = None
                plan_variant = None
                saved = None
        if not before and before_candidates:
            before = before_candidates[0]
        if before and after_candidates and not after:
            # Explicit floor labels must never be crossed by automatic pairing.
            # A user can still choose a different-floor page manually when needed.
            compatible_after = [
                item for item in after_candidates
                if not before.floor_code
                or not item.floor_code
                or item.floor_code == before.floor_code
            ]
            after = next(
                (
                    item for item in compatible_after
                    if not before.plan_variant
                    or item.plan_variant == before.plan_variant
                ),
                compatible_after[0] if compatible_after else None,
            )
        if not before and after_candidates:
            after = after or after_candidates[0]

        missing_reason = None
        if not before_candidates and not after_candidates:
            missing_reason = "現況図・提案図として確認された平面図がありません"
        elif not before_candidates:
            missing_reason = "現況図として確認された平面図がありません"
        elif not after_candidates:
            missing_reason = "提案図として確認された平面図がありません"
        elif before and not after:
            missing_reason = (
                "同じ階として確認できる提案図がありません。"
                "右側の候補から手動で選択できます"
            )
        return DocumentSetComparison(
            document_set_id=document_set_id,
            label=label,
            pair=ComparisonPair(
                pair_id=pair_id,
                document_set_id=document_set_id,
                floor_code=floor_code or (before.floor_code if before else None),
                plan_variant=plan_variant or (after.plan_variant if after else None),
                before=before,
                after=after,
                source=source,
                user_locked=bool(saved and saved.get("user_locked")),
                complete=bool(before and after),
                missing_reason=missing_reason,
            ),
            all_pages=candidates,
            before_candidates=before_candidates,
            after_candidates=after_candidates,
            facts=self.facts(document_set_id),
        )

    def save_pair(
        self,
        document_set_id: str,
        selection: ComparisonSelectionRequest,
        user_hash: str | None,
    ) -> DocumentSetComparison:
        self._set_label(document_set_id, user_hash)
        candidates = self.page_candidates(document_set_id, user_hash)
        before = next((item for item in candidates if self._same_reference(item, selection.before)), None)
        after = next((item for item in candidates if self._same_reference(item, selection.after)), None)
        if not before or not after:
            raise ValueError("選択したページが案件グループに存在しません")
        if not before.confirmed or not after.confirmed:
            raise ValueError("現況図と提案図はページ分類を確認してから選択してください")
        if before.phase != "EXISTING" or before.content_kind != "FLOOR_PLAN":
            raise ValueError("左側には現況の平面図を指定してください")
        if after.phase != "PROPOSED" or after.content_kind != "FLOOR_PLAN":
            raise ValueError("右側には提案の平面図を指定してください")
        pair_id = uuid4().hex
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                MERGE INTO sds_comparison_pairs target
                USING (SELECT :document_set document_set_id FROM dual) source
                ON (target.document_set_id=source.document_set_id)
                WHEN MATCHED THEN UPDATE SET
                    target.floor_code=:floor_code,
                    target.plan_variant=:plan_variant,
                    target.before_document_id=:before_document,
                    target.before_revision_id=:before_revision,
                    target.before_page_number=:before_page,
                    target.after_document_id=:after_document,
                    target.after_revision_id=:after_revision,
                    target.after_page_number=:after_page,
                    target.source='USER', target.user_locked=1,
                    target.updated_by_hash=:user_hash,
                    target.updated_at=SYSTIMESTAMP
                WHEN NOT MATCHED THEN INSERT
                    (pair_id, document_set_id, floor_code, plan_variant,
                     before_document_id, before_revision_id, before_page_number,
                     after_document_id, after_revision_id, after_page_number,
                     source, user_locked, updated_by_hash)
                VALUES
                    (:pair_id, :document_set, :floor_code, :plan_variant,
                     :before_document, :before_revision, :before_page,
                     :after_document, :after_revision, :after_page,
                     'USER', 1, :user_hash)
                """,
                {
                    "pair_id": pair_id,
                    "document_set": document_set_id,
                    "floor_code": selection.floor_code or before.floor_code or after.floor_code,
                    "plan_variant": selection.plan_variant or after.plan_variant,
                    "before_document": before.document_id,
                    "before_revision": before.revision_id,
                    "before_page": before.page_number,
                    "after_document": after.document_id,
                    "after_revision": after.revision_id,
                    "after_page": after.page_number,
                    "user_hash": user_hash,
                },
            )
            connection.commit()
        return self.comparison(document_set_id, user_hash)

    def update_page(
        self,
        reference: PageReference,
        patch: PageClassificationPatch,
        user_hash: str | None,
    ) -> PageClassification:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE sds_page_classifications pc
                SET content_kind=:content_kind, phase=:phase, floor_code=:floor_code,
                    plan_variant=:plan_variant, source='USER', confidence=1,
                    confirmed=:confirmed, user_locked=1, updated_at=SYSTIMESTAMP
                WHERE pc.document_revision_id=:revision AND pc.page_number=:page_number
                  AND pc.document_id=:document
                  AND EXISTS (
                    SELECT 1 FROM sds_documents d
                    WHERE d.document_id=pc.document_id AND {self._access_sql('d')}
                  )
                """,
                {
                    "content_kind": patch.content_kind,
                    "phase": patch.phase,
                    "floor_code": patch.floor_code,
                    "plan_variant": patch.plan_variant,
                    "confirmed": int(patch.confirmed),
                    "revision": reference.revision_id,
                    "page_number": reference.page_number,
                    "document": reference.document_id,
                    "user_hash": user_hash,
                },
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise LookupError("ページ分類が見つからないか更新権限がありません")
            connection.commit()
        # Return the enriched candidate from its set after resolving membership.
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT document_set_id FROM sds_document_metadata WHERE document_id=:document",
                {"document": reference.document_id},
            )
            row = cursor.fetchone()
        if not row or not row[0]:
            raise ValueError("文書が案件グループに所属していません")
        candidates = self.page_candidates(str(row[0]), user_hash)
        result = next((item for item in candidates if self._same_reference(item, reference)), None)
        if not result:
            raise LookupError("更新したページ分類を取得できません")
        return result

    @staticmethod
    def _merge_fact(
        cursor: Any,
        document_set_id: str,
        item: DocumentSetFactInput,
        user_hash: str | None,
    ) -> None:
        cursor.execute(
            """
            MERGE INTO sds_document_set_facts target
            USING (SELECT :document_set document_set_id, :fact_code fact_code,
                          :phase phase FROM dual) source
            ON (target.document_set_id=source.document_set_id
                AND target.fact_code=source.fact_code AND target.phase=source.phase)
            WHEN MATCHED THEN UPDATE SET
                target.value_text=:value_text, target.value_number=:value_number,
                target.unit=:unit, target.source='USER', target.confidence=1,
                target.confirmed=:confirmed, target.user_locked=1,
                target.evidence_document_id=:evidence_document,
                target.evidence_revision_id=:evidence_revision,
                target.evidence_page_number=:evidence_page,
                target.evidence_json=:evidence,
                target.updated_at=SYSTIMESTAMP
            WHEN NOT MATCHED THEN INSERT
                (fact_id, document_set_id, fact_code, phase, value_text,
                 value_number, unit, source, confidence, confirmed, user_locked,
                 evidence_document_id, evidence_revision_id,
                 evidence_page_number, evidence_json)
            VALUES
                (:fact_id, :document_set, :fact_code, :phase, :value_text,
                 :value_number, :unit, 'USER', 1, :confirmed, 1,
                 :evidence_document, :evidence_revision, :evidence_page, :evidence)
            """,
            {
                "fact_id": uuid4().hex,
                "document_set": document_set_id,
                "fact_code": item.fact_code,
                "phase": item.phase,
                "value_text": item.value_text.strip() if item.value_text else None,
                "value_number": item.value_number,
                "unit": item.unit,
                "confirmed": int(item.confirmed),
                "evidence_document": item.evidence_document_id,
                "evidence_revision": item.evidence_revision_id,
                "evidence_page": item.evidence_page_number,
                "evidence": json.dumps({"updated_by": user_hash}, ensure_ascii=False),
            },
        )

    def upsert_fact(
        self,
        document_set_id: str,
        item: DocumentSetFactInput,
        user_hash: str | None,
    ) -> None:
        self._set_label(document_set_id, user_hash)
        with self.connection() as connection, connection.cursor() as cursor:
            try:
                self._merge_fact(cursor, document_set_id, item, user_hash)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def replace_facts(
        self,
        document_set_id: str,
        items: list[DocumentSetFactInput],
        user_hash: str | None,
    ) -> list[DocumentSetFact]:
        """Atomically replace editable typed facts, including explicit clears."""

        self._set_label(document_set_id, user_hash)
        editable_keys = {
            ("BUILDING_TYPE", "COMMON"),
            ("STRUCTURE", "COMMON"),
            ("USE", "COMMON"),
            ("AREA_TYPE", "COMMON"),
            ("AREA_VALUE", "COMMON"),
            ("LAYOUT", "EXISTING"),
            ("LAYOUT", "PROPOSED"),
        }
        supplied_keys = [(item.fact_code, item.phase) for item in items]
        if len(set(supplied_keys)) != len(supplied_keys):
            raise ValueError("同じ建物条件を重複して指定できません")
        if set(supplied_keys) - editable_keys:
            raise ValueError("編集対象外の建物条件が含まれています")

        supplied = {(item.fact_code, item.phase): item for item in items}
        missing = editable_keys - set(supplied_keys)
        with self.connection() as connection, connection.cursor() as cursor:
            try:
                for item in items:
                    self._merge_fact(cursor, document_set_id, item, user_hash)
                if missing:
                    conditions: list[str] = []
                    binds: dict[str, Any] = {"document_set": document_set_id}
                    for index, (fact_code, phase) in enumerate(sorted(missing)):
                        binds[f"code_{index}"] = fact_code
                        binds[f"phase_{index}"] = phase
                        conditions.append(
                            f"(fact_code=:code_{index} AND phase=:phase_{index})"
                        )
                    cursor.execute(
                        "DELETE FROM sds_document_set_facts "
                        "WHERE document_set_id=:document_set AND ("
                        + " OR ".join(conditions)
                        + ")",
                        binds,
                    )

                # The legacy editor still sends AREA_TYPE and AREA_VALUE as
                # separate facts. Keep the paired area record in sync without
                # physically deleting user-entered history.
                cursor.execute(
                    """
                    UPDATE sds_document_set_areas
                    SET confirmed=0, updated_at=SYSTIMESTAMP
                    WHERE document_set_id=:document_set
                      AND phase='COMMON' AND user_locked=1
                    """,
                    {"document_set": document_set_id},
                )
                area_type_item = supplied.get(("AREA_TYPE", "COMMON"))
                area_value_item = supplied.get(("AREA_VALUE", "COMMON"))
                if (
                    area_type_item
                    and area_type_item.value_text
                    and area_value_item
                    and area_value_item.value_number is not None
                ):
                    area_type = area_type_item.value_text.strip()
                    allowed_area_types = {
                        "専有面積", "延床面積", "建築面積", "敷地面積",
                        "施工対象面積", "部屋面積", "不明",
                    }
                    if area_type not in allowed_area_types:
                        raise ValueError("面積種別が不正です")
                    area_value = float(area_value_item.value_number)
                    if area_value <= 0:
                        raise ValueError("面積は0より大きい値を指定してください")
                    unit = area_value_item.unit or "㎡"
                    if unit not in {"㎡", "坪"}:
                        raise ValueError("面積の単位が不正です")
                    cursor.execute(
                        """
                        MERGE INTO sds_document_set_areas target
                        USING (
                            SELECT :document_set document_set_id, 'COMMON' phase,
                                   :area_type area_type, :area_value area_value,
                                   :unit unit
                            FROM dual
                        ) source
                        ON (
                            target.document_set_id=source.document_set_id
                            AND target.phase=source.phase
                            AND target.area_type=source.area_type
                            AND target.area_value=source.area_value
                            AND target.unit=source.unit
                        )
                        WHEN MATCHED THEN UPDATE SET
                            target.source='USER', target.confidence=1,
                            target.confirmed=1, target.user_locked=1,
                            target.evidence_json=:evidence,
                            target.updated_at=SYSTIMESTAMP
                        WHEN NOT MATCHED THEN INSERT
                            (area_id, document_set_id, phase, area_type,
                             area_value, unit, source, confidence, confirmed,
                             user_locked, evidence_json)
                        VALUES
                            (:area_id, :document_set, 'COMMON', :area_type,
                             :area_value, :unit, 'USER', 1, 1, 1, :evidence)
                        """,
                        {
                            "area_id": uuid4().hex,
                            "document_set": document_set_id,
                            "area_type": area_type,
                            "area_value": area_value,
                            "unit": unit,
                            "evidence": json.dumps(
                                {"updated_by": user_hash}, ensure_ascii=False
                            ),
                        },
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.facts(document_set_id)

    def condition_options(self, user_hash: str | None) -> BuildingConditionOptions:
        access = self._access_sql("d")
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT DISTINCT f.fact_code, f.phase, f.value_text
                FROM sds_document_set_facts f
                WHERE f.confirmed=1 AND f.value_text IS NOT NULL
                  AND EXISTS (
                    SELECT 1 FROM sds_document_metadata dm
                    JOIN sds_documents d ON d.document_id=dm.document_id
                    WHERE dm.document_set_id=f.document_set_id
                      AND d.is_current=1 AND d.serving_release_id IS NOT NULL
                      AND {access}
                  )
                ORDER BY f.fact_code, f.phase, f.value_text
                """,
                {"user_hash": user_hash},
            )
            rows = self.rows(cursor)
            cursor.execute(
                f"""
                SELECT DISTINCT area.area_type
                FROM sds_document_set_areas area
                WHERE area.confirmed=1
                  AND EXISTS (
                    SELECT 1 FROM sds_document_metadata dm
                    JOIN sds_documents d ON d.document_id=dm.document_id
                    WHERE dm.document_set_id=area.document_set_id
                      AND d.is_current=1 AND d.serving_release_id IS NOT NULL
                      AND {access}
                  )
                ORDER BY area.area_type
                """,
                {"user_hash": user_hash},
            )
            area_types = [str(row["area_type"]) for row in self.rows(cursor)]
        values: dict[str, list[str]] = {
            "building_types": [], "structures": [], "uses": [],
            "area_types": area_types,
            "existing_layouts": [], "proposed_layouts": [],
        }
        mapping = {
            ("BUILDING_TYPE", "COMMON"): "building_types",
            ("STRUCTURE", "COMMON"): "structures",
            ("USE", "COMMON"): "uses",
            ("LAYOUT", "EXISTING"): "existing_layouts",
            ("LAYOUT", "PROPOSED"): "proposed_layouts",
        }
        for row in rows:
            target = mapping.get((str(row["fact_code"]), str(row["phase"])))
            if target:
                values[target].append(str(row["value_text"]))
        for key in values:
            values[key] = sorted(set(values[key]))
        return BuildingConditionOptions(**values)

    @staticmethod
    def analysis_input_hash(
        selection: ComparisonSelectionRequest,
        facts: list[DocumentSetFact],
    ) -> str:
        stable_facts = sorted(
            (
                {
                    "fact_code": item.fact_code,
                    "phase": item.phase,
                    "value_text": item.value_text,
                    "value_number": item.value_number,
                    "unit": item.unit,
                    "confirmed": item.confirmed,
                    "evidence_document_id": item.evidence_document_id,
                    "evidence_revision_id": item.evidence_revision_id,
                    "evidence_page_number": item.evidence_page_number,
                }
                for item in facts
            ),
            key=lambda item: (item["fact_code"], item["phase"]),
        )
        payload = {
            "before": selection.before.model_dump(),
            "after": selection.after.model_dump(),
            # Ignore row IDs and timestamps so a no-op save can reuse the cache.
            "facts": stable_facts,
            "prompt_version": PROMPT_VERSION,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    def create_analysis(
        self,
        document_set_id: str,
        selection: ComparisonSelectionRequest,
        user_hash: str | None,
        *,
        force: bool,
    ) -> ComparisonAnalysis:
        comparison = self.save_pair(document_set_id, selection, user_hash)
        input_hash = self.analysis_input_hash(selection, comparison.facts)
        if not force:
            with self.connection() as connection, connection.cursor() as cursor:
                # In-process workers can be interrupted by a service restart.
                # Expire abandoned rows so the next request can start a new run.
                cursor.execute(
                    """
                    UPDATE sds_comparison_analyses
                    SET status='FAILED',
                        error_summary='比較処理が中断されました。再度分析してください',
                        completed_at=SYSTIMESTAMP,
                        updated_at=SYSTIMESTAMP
                    WHERE document_set_id=:document_set
                      AND status IN ('PENDING','RUNNING')
                      AND updated_at < SYSTIMESTAMP - NUMTODSINTERVAL(30, 'MINUTE')
                    """,
                    {"document_set": document_set_id},
                )
                connection.commit()
                cursor.execute(
                    """
                    SELECT * FROM sds_comparison_analyses
                    WHERE document_set_id=:document_set AND input_hash=:input_hash
                      AND prompt_version=:prompt_version
                      AND status IN ('COMPLETED','RUNNING','PENDING')
                    ORDER BY CASE status WHEN 'COMPLETED' THEN 1
                                         WHEN 'RUNNING' THEN 2 ELSE 3 END,
                             updated_at DESC
                    FETCH FIRST 1 ROWS ONLY
                    """,
                    {
                        "document_set": document_set_id,
                        "input_hash": input_hash,
                        "prompt_version": PROMPT_VERSION,
                    },
                )
                rows = self.rows(cursor)
            if rows:
                result = self._analysis_from_row(rows[0])
                result.cached = result.status == "COMPLETED"
                return result

        analysis_id = uuid4().hex
        cache_key = hashlib.sha256(f"{input_hash}:{analysis_id}".encode()).hexdigest()
        request_json = json.dumps(
            {"selection": selection.model_dump(mode="json")},
            ensure_ascii=False,
        )
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sds_comparison_analyses
                    (analysis_id, document_set_id, pair_id, input_hash, cache_key,
                     prompt_version, status, request_json, requested_by_hash)
                VALUES
                    (:analysis_id, :document_set, :pair_id, :input_hash, :cache_key,
                     :prompt_version, 'PENDING', :request_json, :user_hash)
                """,
                {
                    "analysis_id": analysis_id,
                    "document_set": document_set_id,
                    "pair_id": comparison.pair.pair_id,
                    "input_hash": input_hash,
                    "cache_key": cache_key,
                    "prompt_version": PROMPT_VERSION,
                    "request_json": request_json,
                    "user_hash": user_hash,
                },
            )
            connection.commit()
        return self.get_analysis(analysis_id, user_hash)

    @staticmethod
    def _analysis_from_row(row: dict[str, Any]) -> ComparisonAnalysis:
        return ComparisonAnalysis(
            analysis_id=str(row["analysis_id"]),
            document_set_id=str(row["document_set_id"]),
            pair_id=str(row["pair_id"]) if row.get("pair_id") else None,
            status=str(row["status"]),
            prompt_version=str(row["prompt_version"]),
            result=_json_value(row.get("result_json"), None),
            error_summary=str(row["error_summary"]) if row.get("error_summary") else None,
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    def get_analysis(self, analysis_id: str, user_hash: str | None) -> ComparisonAnalysis:
        with self.connection() as connection, connection.cursor() as cursor:
            # Do not leave a job permanently displayed as running after a backend
            # restart. The ACL predicate also prevents cross-user status mutation.
            cursor.execute(
                f"""
                UPDATE sds_comparison_analyses analysis
                SET status='FAILED',
                    error_summary='比較処理が中断されました。再度分析してください',
                    completed_at=SYSTIMESTAMP,
                    updated_at=SYSTIMESTAMP
                WHERE analysis.analysis_id=:analysis_id
                  AND analysis.status IN ('PENDING','RUNNING')
                  AND analysis.updated_at
                      < SYSTIMESTAMP - NUMTODSINTERVAL(30, 'MINUTE')
                  AND EXISTS (
                    SELECT 1 FROM sds_document_metadata dm
                    JOIN sds_documents d ON d.document_id=dm.document_id
                    WHERE dm.document_set_id=analysis.document_set_id
                      AND {self._access_sql('d')}
                  )
                """,
                {"analysis_id": analysis_id, "user_hash": user_hash},
            )
            connection.commit()
            cursor.execute(
                f"""
                SELECT analysis.*
                FROM sds_comparison_analyses analysis
                WHERE analysis.analysis_id=:analysis_id
                  AND EXISTS (
                    SELECT 1 FROM sds_document_metadata dm
                    JOIN sds_documents d ON d.document_id=dm.document_id
                    WHERE dm.document_set_id=analysis.document_set_id
                      AND {self._access_sql('d')}
                  )
                """,
                {"analysis_id": analysis_id, "user_hash": user_hash},
            )
            rows = self.rows(cursor)
        if not rows:
            raise LookupError("比較分析が見つからないか参照権限がありません")
        return self._analysis_from_row(rows[0])

    def claim_analysis(self, analysis_id: str) -> bool:
        """Atomically claim a pending analysis so it can only run once."""

        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE sds_comparison_analyses
                SET status='RUNNING', updated_at=SYSTIMESTAMP
                WHERE analysis_id=:analysis_id AND status='PENDING'
                """,
                {"analysis_id": analysis_id},
            )
            claimed = cursor.rowcount == 1
            connection.commit()
        return claimed

    def analysis_context(self, analysis_id: str) -> dict[str, Any]:
        """Resolve the immutable page selection captured when analysis was requested."""

        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT document_set_id, requested_by_hash, request_json
                FROM sds_comparison_analyses
                WHERE analysis_id=:analysis_id
                """,
                {"analysis_id": analysis_id},
            )
            rows = self.rows(cursor)
        if not rows:
            raise LookupError("比較分析が見つかりません")
        row = rows[0]
        user_hash = str(row["requested_by_hash"]) if row.get("requested_by_hash") else None
        comparison = self.comparison(str(row["document_set_id"]), user_hash)
        request_payload = _json_value(row.get("request_json"), {})
        selection_payload = request_payload.get("selection") if isinstance(request_payload, dict) else None
        if selection_payload:
            selection = ComparisonSelectionRequest.model_validate(selection_payload)
            before = next(
                (
                    page for page in comparison.all_pages
                    if self._same_reference(page, selection.before)
                ),
                None,
            )
            after = next(
                (
                    page for page in comparison.all_pages
                    if self._same_reference(page, selection.after)
                ),
                None,
            )
        else:
            # Compatibility for rows created before REQUEST_JSON was introduced.
            before = comparison.pair.before
            after = comparison.pair.after
        if not before or not after:
            raise RuntimeError("依頼時に選択された現況図または提案図を取得できません")
        return {
            "comparison": comparison,
            "before": before,
            "after": after,
            "user_hash": user_hash,
        }

    def set_analysis_status(
        self, analysis_id: str, status: str, *, result: dict | None = None,
        error_summary: str | None = None,
    ) -> None:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE sds_comparison_analyses
                SET status=:status, result_json=:result, error_summary=:error,
                    updated_at=SYSTIMESTAMP,
                    completed_at=CASE WHEN :status IN ('COMPLETED','FAILED')
                                      THEN SYSTIMESTAMP ELSE completed_at END
                WHERE analysis_id=:analysis_id
                """,
                {
                    "status": status,
                    "result": json.dumps(result, ensure_ascii=False) if result is not None else None,
                    "error": (error_summary or "")[:2000] or None,
                    "analysis_id": analysis_id,
                },
            )
            connection.commit()


case_comparison_repository = CaseComparisonRepository()
