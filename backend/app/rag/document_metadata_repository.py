from __future__ import annotations

import hashlib
import json
import math
import mimetypes
from contextlib import contextmanager
from pathlib import PurePosixPath
from typing import Any, Iterator, Sequence
from uuid import uuid4

from app.rag.classification_rules import clean_text, normalize_comparable, normalize_customer_name
from app.rag.document_metadata_models import (
    ActiveIngestBatch,
    ClassificationRuleSet,
    ClassificationRuleSetConfig,
    ClassificationRuleSetUpsert,
    CustomerSuggestion,
    ConceptExtractionItem,
    DocumentConcept,
    DocumentLibraryItem,
    DocumentLibraryResponse,
    DocumentMetadata,
    DocumentMetadataPatch,
    DocumentSet,
    DocumentSetCreate,
    DocumentSetSuggestion,
    DocumentSetUpdate,
    DocumentTagAssignment,
    FolderCreate,
    FolderDefaults,
    FolderNode,
    FolderRuleProfile,
    FolderRuleProfileUpsert,
    FolderUpdate,
    IngestBatch,
    IngestItemReview,
    ROOT_FOLDER_ID,
    RuleCandidate,
    SearchConcept,
    SearchConceptSettings,
    SearchConceptUpdate,
    TagDefinition,
    TagGroup,
    TagGroupUpsert,
    TagUpsert,
    UNCLASSIFIED_FOLDER_ID,
)
from app.rag.document_metadata_schema import (
    SYSTEM_TENANT_HASH,
    document_library_schema_status,
)
from app.services.database_service import database_service


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


class DocumentMetadataRepository:
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
    def _folder_exists(cursor: Any, folder_id: str) -> bool:
        cursor.execute(
            "SELECT COUNT(*) FROM SDS_FOLDERS WHERE FOLDER_ID=:folder_id",
            {"folder_id": folder_id},
        )
        return bool(cursor.fetchone()[0])

    def folder_tree(self) -> list[FolderNode]:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT f.folder_id, f.parent_folder_id, f.name, f.normalized_name,
                       NVL(self_link.depth, 0) tree_depth, f.is_system,
                       (SELECT COUNT(*)
                        FROM sds_document_metadata dm
                        JOIN sds_documents d ON d.document_id=dm.document_id
                        WHERE dm.folder_id=f.folder_id
                          AND d.is_current=1 AND d.status<>'DRAFT') document_count,
                       (SELECT COUNT(*)
                        FROM sds_folder_closure subtree
                        JOIN sds_document_metadata dm
                          ON dm.folder_id=subtree.descendant_folder_id
                        JOIN sds_documents d ON d.document_id=dm.document_id
                        WHERE subtree.ancestor_folder_id=f.folder_id
                          AND d.is_current=1 AND d.status<>'DRAFT') descendant_count
                FROM sds_folders f
                LEFT JOIN sds_folder_closure self_link
                  ON self_link.ancestor_folder_id=:root
                 AND self_link.descendant_folder_id=f.folder_id
                ORDER BY NVL(self_link.depth, 9999), f.sort_order, f.name, f.folder_id
                """,
                {"root": ROOT_FOLDER_ID},
            )
            rows = self.rows(cursor)
        nodes: dict[str, FolderNode] = {
            str(row["folder_id"]): FolderNode(
                folder_id=str(row["folder_id"]),
                parent_folder_id=(str(row["parent_folder_id"]) if row.get("parent_folder_id") else None),
                name=str(row["name"]),
                normalized_name=str(row["normalized_name"]),
                depth=int(row.get("tree_depth") or 0),
                is_system=bool(row.get("is_system")),
                document_count=int(row.get("document_count") or 0),
                descendant_document_count=int(row.get("descendant_count") or 0),
            )
            for row in rows
        }
        roots: list[FolderNode] = []
        for node in nodes.values():
            parent = nodes.get(node.parent_folder_id or "")
            if parent is None:
                roots.append(node)
            else:
                parent.children.append(node)
        return roots

    def create_folder(self, payload: FolderCreate) -> FolderNode:
        self.require_schema()
        folder_id = uuid4().hex
        normalized = normalize_comparable(payload.name)
        with self.connection() as connection, connection.cursor() as cursor:
            if not self._folder_exists(cursor, payload.parent_folder_id):
                raise LookupError("親フォルダが見つかりません")
            try:
                cursor.execute(
                    """
                    INSERT INTO SDS_FOLDERS
                        (FOLDER_ID, TENANT_ID_HASH, PARENT_FOLDER_ID, NAME, NORMALIZED_NAME)
                    VALUES (:folder_id, :tenant, :parent_id, :name, :normalized)
                    """,
                    {
                        "folder_id": folder_id,
                        "tenant": SYSTEM_TENANT_HASH,
                        "parent_id": payload.parent_folder_id,
                        "name": payload.name,
                        "normalized": normalized,
                    },
                )
                cursor.execute(
                    """
                    INSERT INTO SDS_FOLDER_CLOSURE
                        (ANCESTOR_FOLDER_ID, DESCENDANT_FOLDER_ID, DEPTH)
                    SELECT ANCESTOR_FOLDER_ID, :folder_id, DEPTH + 1
                    FROM SDS_FOLDER_CLOSURE
                    WHERE DESCENDANT_FOLDER_ID=:parent_id
                    """,
                    {"folder_id": folder_id, "parent_id": payload.parent_folder_id},
                )
                cursor.execute(
                    "INSERT INTO SDS_FOLDER_CLOSURE VALUES (:folder_id, :folder_id, 0)",
                    {"folder_id": folder_id},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return FolderNode(
            folder_id=folder_id,
            parent_folder_id=payload.parent_folder_id,
            name=payload.name,
            normalized_name=normalized,
        )

    def update_folder(self, folder_id: str, payload: FolderUpdate) -> FolderNode:
        self.require_schema()
        if folder_id == ROOT_FOLDER_ID:
            raise ValueError("ルートフォルダは変更できません")
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT parent_folder_id, name, normalized_name, is_system "
                "FROM sds_folders WHERE folder_id=:folder_id FOR UPDATE",
                {"folder_id": folder_id},
            )
            row = cursor.fetchone()
            if not row:
                raise LookupError("フォルダが見つかりません")
            current_parent = str(row[0]) if row[0] else None
            current_name = str(row[1])
            is_system = bool(row[3])
            if is_system and payload.name is not None:
                raise ValueError("システムフォルダの名前は変更できません")
            next_name = payload.name if payload.name is not None else current_name
            next_parent = (
                payload.parent_folder_id
                if "parent_folder_id" in payload.model_fields_set
                else current_parent
            )
            if next_parent is None:
                raise ValueError("ルート以外のフォルダには親が必要です")
            if not self._folder_exists(cursor, next_parent):
                raise LookupError("移動先フォルダが見つかりません")
            cursor.execute(
                """
                SELECT COUNT(*) FROM SDS_FOLDER_CLOSURE
                WHERE ANCESTOR_FOLDER_ID=:folder_id
                  AND DESCENDANT_FOLDER_ID=:parent_id
                """,
                {"folder_id": folder_id, "parent_id": next_parent},
            )
            if int(cursor.fetchone()[0]):
                raise ValueError("子孫フォルダの下へ移動できません")
            try:
                cursor.execute(
                    """
                    UPDATE SDS_FOLDERS
                    SET NAME=:name, NORMALIZED_NAME=:normalized,
                        PARENT_FOLDER_ID=:parent_id, UPDATED_AT=SYSTIMESTAMP
                    WHERE FOLDER_ID=:folder_id
                    """,
                    {
                        "name": next_name,
                        "normalized": normalize_comparable(next_name),
                        "parent_id": next_parent,
                        "folder_id": folder_id,
                    },
                )
                if next_parent != current_parent:
                    cursor.execute(
                        """
                        DELETE FROM SDS_FOLDER_CLOSURE c
                        WHERE c.DESCENDANT_FOLDER_ID IN (
                            SELECT DESCENDANT_FOLDER_ID FROM SDS_FOLDER_CLOSURE
                            WHERE ANCESTOR_FOLDER_ID=:folder_id
                        )
                        AND c.ANCESTOR_FOLDER_ID IN (
                            SELECT ANCESTOR_FOLDER_ID FROM SDS_FOLDER_CLOSURE
                            WHERE DESCENDANT_FOLDER_ID=:folder_id AND ANCESTOR_FOLDER_ID<>:folder_id
                        )
                        """,
                        {"folder_id": folder_id},
                    )
                    cursor.execute(
                        """
                        INSERT INTO SDS_FOLDER_CLOSURE
                            (ANCESTOR_FOLDER_ID, DESCENDANT_FOLDER_ID, DEPTH)
                        SELECT supertree.ANCESTOR_FOLDER_ID,
                               subtree.DESCENDANT_FOLDER_ID,
                               supertree.DEPTH + subtree.DEPTH + 1
                        FROM SDS_FOLDER_CLOSURE supertree
                        CROSS JOIN SDS_FOLDER_CLOSURE subtree
                        WHERE supertree.DESCENDANT_FOLDER_ID=:parent_id
                          AND subtree.ANCESTOR_FOLDER_ID=:folder_id
                        """,
                        {"parent_id": next_parent, "folder_id": folder_id},
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return FolderNode(
            folder_id=folder_id,
            parent_folder_id=next_parent,
            name=next_name,
            normalized_name=normalize_comparable(next_name),
            is_system=is_system,
        )

    def delete_folder(self, folder_id: str) -> None:
        self.require_schema()
        if folder_id in {ROOT_FOLDER_ID, UNCLASSIFIED_FOLDER_ID}:
            raise ValueError("システムフォルダは削除できません")
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM SDS_FOLDERS WHERE PARENT_FOLDER_ID=:folder_id),
                    (SELECT COUNT(*) FROM SDS_DOCUMENT_METADATA WHERE FOLDER_ID=:folder_id)
                FROM dual
                """,
                {"folder_id": folder_id},
            )
            child_count, document_count = cursor.fetchone()
            if int(child_count) or int(document_count):
                raise ValueError("文書または子フォルダがあるフォルダは削除できません")
            cursor.execute(
                "DELETE FROM SDS_FOLDERS WHERE FOLDER_ID=:folder_id AND IS_SYSTEM=0",
                {"folder_id": folder_id},
            )
            if cursor.rowcount != 1:
                raise LookupError("フォルダが見つかりません")
            connection.commit()

    @staticmethod
    def _document_set_from_row(row: dict[str, Any]) -> DocumentSet:
        return DocumentSet(
            document_set_id=str(row["document_set_id"]),
            label=str(row["label"]),
            normalized_label=str(row["normalized_label"]),
            description=str(row["description"]) if row.get("description") else None,
            status=str(row.get("status") or "ACTIVE"),
            document_count=int(row.get("document_count") or 0),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    def list_document_sets(
        self,
        *,
        query: str = "",
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[DocumentSet]:
        self.require_schema()
        clauses = ["1=1"]
        binds: dict[str, Any] = {}
        if not include_archived:
            clauses.append("ds.status='ACTIVE'")
        normalized_query = normalize_comparable(query)
        if normalized_query:
            clauses.append(
                "(ds.normalized_label LIKE '%' || :query || '%' "
                "OR LOWER(ds.description) LIKE '%' || :query || '%')"
            )
            binds["query"] = normalized_query
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT ds.*,
                       (SELECT COUNT(*) FROM sds_document_metadata m
                        JOIN sds_documents d ON d.document_id=m.document_id
                        WHERE m.document_set_id=ds.document_set_id
                          AND d.is_current=1 AND d.status<>'DRAFT') document_count
                FROM sds_document_sets ds
                WHERE {' AND '.join(clauses)}
                ORDER BY ds.updated_at DESC, ds.label
                FETCH FIRST {max(1, min(limit, 500))} ROWS ONLY
                """,
                binds,
            )
            return [self._document_set_from_row(row) for row in self.rows(cursor)]

    def create_document_set(
        self,
        payload: DocumentSetCreate,
        *,
        created_by_hash: str | None,
    ) -> DocumentSet:
        self.require_schema()
        label = clean_text(payload.label)
        normalized = normalize_comparable(label)
        document_set_id = uuid4().hex
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT document_set_id FROM sds_document_sets
                WHERE normalized_label=:normalized AND status='ACTIVE'
                FETCH FIRST 1 ROW ONLY
                """,
                {"normalized": normalized},
            )
            existing = cursor.fetchone()
            if existing:
                raise ValueError("同じ名前の案件グループが既にあります")
            cursor.execute(
                """
                INSERT INTO sds_document_sets
                    (document_set_id, label, normalized_label, description,
                     created_by_hash)
                VALUES (:document_set_id, :label, :normalized, :description,
                        :created_by)
                """,
                {
                    "document_set_id": document_set_id,
                    "label": label,
                    "normalized": normalized,
                    "description": payload.description,
                    "created_by": created_by_hash,
                },
            )
            connection.commit()
        return next(
            item
            for item in self.list_document_sets(
                query=label, include_archived=True, limit=20
            )
            if item.document_set_id == document_set_id
        )

    def update_document_set(
        self,
        document_set_id: str,
        payload: DocumentSetUpdate,
    ) -> DocumentSet:
        self.require_schema()
        fields = payload.model_fields_set
        assignments: list[str] = []
        binds: dict[str, Any] = {"document_set_id": document_set_id}
        if "label" in fields and payload.label is not None:
            label = clean_text(payload.label)
            binds.update(label=label, normalized=normalize_comparable(label))
            assignments.extend(["label=:label", "normalized_label=:normalized"])
        if "description" in fields:
            binds["description"] = payload.description
            assignments.append("description=:description")
        if "status" in fields and payload.status is not None:
            binds["status"] = payload.status
            assignments.append("status=:status")
        if not assignments:
            matches = [
                value
                for value in self.list_document_sets(include_archived=True, limit=500)
                if value.document_set_id == document_set_id
            ]
            if not matches:
                raise LookupError("案件グループが見つかりません")
            return matches[0]
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE sds_document_sets SET "
                + ", ".join(assignments)
                + ", updated_at=SYSTIMESTAMP WHERE document_set_id=:document_set_id",
                binds,
            )
            if cursor.rowcount != 1:
                raise LookupError("案件グループが見つかりません")
            connection.commit()
        matches = [
            value
            for value in self.list_document_sets(include_archived=True, limit=500)
            if value.document_set_id == document_set_id
        ]
        return matches[0]

    def suggest_document_sets(
        self, document_id: str, *, limit: int = 10
    ) -> list[DocumentSetSuggestion]:
        """Return weak candidates only. A surname/customer match never auto-assigns."""
        metadata = self.get_document_metadata(document_id)
        if metadata.document_set_id:
            return []
        candidates = self.list_document_sets(limit=500)
        if not candidates:
            return []
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.document_set_id, m.folder_id, m.document_year, m.document_month,
                       m.customer_name_search_key
                FROM sds_document_metadata m
                JOIN sds_documents d ON d.document_id=m.document_id
                WHERE m.document_set_id IS NOT NULL
                  AND d.is_current=1 AND d.status<>'DRAFT'
                """
            )
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in self.rows(cursor):
                grouped.setdefault(str(row["document_set_id"]), []).append(row)
        suggestions: list[DocumentSetSuggestion] = []
        for candidate in candidates:
            score = 0.0
            reasons: list[str] = []
            rows = grouped.get(candidate.document_set_id, [])
            if any(str(row.get("folder_id") or "") == metadata.folder_id for row in rows):
                score += 0.2
                reasons.append("同じフォルダ")
            if metadata.document_year and any(
                int(row.get("document_year") or 0) == metadata.document_year
                and int(row.get("document_month") or 0)
                == int(metadata.document_month or 0)
                for row in rows
            ):
                score += 0.4
                reasons.append("年月が一致")
            if metadata.customer_name_search_key and any(
                str(row.get("customer_name_search_key") or "")
                == metadata.customer_name_search_key
                for row in rows
            ):
                score += 0.25
                reasons.append("顧客名が一致（同姓の可能性あり）")
            if score:
                suggestions.append(
                    DocumentSetSuggestion(
                        **candidate.model_dump(),
                        score=min(score, 1),
                        reasons=reasons,
                        requires_confirmation=True,
                    )
                )
        return sorted(
            suggestions, key=lambda value: (-value.score, value.label)
        )[: max(1, min(limit, 50))]

    def get_concept_settings(self) -> SearchConceptSettings:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT enabled, auto_publish, auto_publish_confidence,
                       min_support_sets, max_concepts_per_document,
                       initial_display_limit, input_text_limit, prompt_text,
                       taxonomy_revision
                FROM sds_search_concept_settings WHERE settings_id='default'
                """
            )
            row = cursor.fetchone()
        if not row:
            raise LookupError("検索コンセプト設定が見つかりません")
        return SearchConceptSettings(
            enabled=bool(row[0]),
            auto_publish=bool(row[1]),
            auto_publish_confidence=float(row[2]),
            min_support_sets=int(row[3]),
            max_concepts_per_document=int(row[4]),
            initial_display_limit=int(row[5]),
            input_text_limit=int(row[6]),
            prompt_text=_lob_text(row[7]),
            taxonomy_revision=int(row[8]),
        )

    def update_concept_settings(
        self, payload: SearchConceptSettings
    ) -> SearchConceptSettings:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE sds_search_concept_settings SET
                    enabled=:enabled, auto_publish=:auto_publish,
                    auto_publish_confidence=:confidence,
                    min_support_sets=:min_support,
                    max_concepts_per_document=:max_concepts,
                    initial_display_limit=:display_limit,
                    input_text_limit=:input_limit, prompt_text=:prompt,
                    taxonomy_revision=:taxonomy_revision,
                    updated_at=SYSTIMESTAMP
                WHERE settings_id='default'
                """,
                {
                    "enabled": int(payload.enabled),
                    "auto_publish": int(payload.auto_publish),
                    "confidence": payload.auto_publish_confidence,
                    "min_support": payload.min_support_sets,
                    "max_concepts": payload.max_concepts_per_document,
                    "display_limit": payload.initial_display_limit,
                    "input_limit": payload.input_text_limit,
                    "prompt": payload.prompt_text,
                    "taxonomy_revision": payload.taxonomy_revision,
                },
            )
            if payload.auto_publish:
                cursor.execute(
                    """
                    UPDATE sds_search_concepts c
                    SET status='ACTIVE', updated_at=SYSTIMESTAMP
                    WHERE c.status='PENDING'
                      AND (
                          SELECT COUNT(DISTINCT m.document_set_id)
                          FROM sds_document_concepts dc
                          JOIN sds_document_metadata m
                            ON m.document_id=dc.document_id
                          JOIN sds_documents d
                            ON d.document_id=dc.document_id
                          JOIN sds_index_releases rel
                            ON rel.release_id=d.serving_release_id
                           AND rel.document_revision_id=dc.revision_id
                           AND rel.status='PUBLISHED'
                          WHERE dc.concept_id=c.concept_id
                            AND m.document_set_id IS NOT NULL
                            AND d.is_current=1 AND d.status<>'DRAFT'
                            AND dc.confidence>=:publish_confidence
                      )>=:publish_min_support
                    """,
                    {
                        "publish_confidence": payload.auto_publish_confidence,
                        "publish_min_support": payload.min_support_sets,
                    },
                )
            connection.commit()
        return self.get_concept_settings()

    @staticmethod
    def _concept_from_row(row: dict[str, Any]) -> SearchConcept:
        return SearchConcept(
            concept_id=str(row["concept_id"]),
            facet=str(row["facet"]),
            category_code=str(row["category_code"]),
            category_name=str(row["category_name"]),
            display_label=str(row["display_label"]),
            normalized_label=str(row["normalized_label"]),
            status=str(row["status"]),
            merged_into_id=(
                str(row["merged_into_id"]) if row.get("merged_into_id") else None
            ),
            support_document_count=int(row.get("support_document_count") or 0),
            support_set_count=int(row.get("support_set_count") or 0),
        )

    def list_search_concepts(
        self,
        *,
        status: str = "ACTIVE",
        facet: str | None = None,
        query: str = "",
        limit: int = 100,
        offset: int = 0,
        include_zero_support: bool = True,
    ) -> list[SearchConcept]:
        self.require_schema()
        clauses = ["c.status=:status"]
        if not include_zero_support:
            clauses.append("c.support_document_count>0")
        binds: dict[str, Any] = {"status": status}
        if facet:
            clauses.append("c.facet=:facet")
            binds["facet"] = facet
        normalized_query = normalize_comparable(query)
        if normalized_query:
            clauses.append(
                "(c.normalized_label LIKE '%' || :query || '%' "
                "OR EXISTS (SELECT 1 FROM sds_search_concept_aliases a "
                "WHERE a.concept_id=c.concept_id "
                "AND a.normalized_alias LIKE '%' || :query || '%'))"
            )
            binds["query"] = normalized_query
        safe_limit = max(1, min(limit, 500))
        safe_offset = max(0, offset)
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT c.* FROM sds_search_concepts c
                WHERE {' AND '.join(clauses)}
                ORDER BY c.facet, c.category_name, c.support_set_count DESC,
                         c.support_document_count DESC, c.display_label
                OFFSET {safe_offset} ROWS FETCH NEXT {safe_limit} ROWS ONLY
                """,
                binds,
            )
            return [self._concept_from_row(row) for row in self.rows(cursor)]

    def update_search_concept(
        self, concept_id: str, payload: SearchConceptUpdate
    ) -> SearchConcept:
        fields = payload.model_fields_set
        assignments: list[str] = []
        binds: dict[str, Any] = {"concept_id": concept_id}
        for field_name in (
            "category_code", "category_name", "facet", "status", "merged_into_id"
        ):
            if field_name in fields:
                binds[field_name] = getattr(payload, field_name)
                assignments.append(f"{field_name}=:{field_name}")
        if "display_label" in fields and payload.display_label is not None:
            label = clean_text(payload.display_label)
            binds.update(display_label=label, normalized_label=normalize_comparable(label))
            assignments.extend(
                ["display_label=:display_label", "normalized_label=:normalized_label"]
            )
        if payload.status == "MERGED" and not payload.merged_into_id:
            raise ValueError("統合先のコンセプトを指定してください")
        with self.connection() as connection, connection.cursor() as cursor:
            if assignments:
                cursor.execute(
                    "UPDATE sds_search_concepts SET "
                    + ", ".join(assignments)
                    + ", updated_at=SYSTIMESTAMP WHERE concept_id=:concept_id",
                    binds,
                )
                if cursor.rowcount != 1:
                    raise LookupError("検索コンセプトが見つかりません")
                connection.commit()
            cursor.execute(
                "SELECT c.* FROM sds_search_concepts c WHERE concept_id=:concept_id",
                {"concept_id": concept_id},
            )
            rows = self.rows(cursor)
        if not rows:
            raise LookupError("検索コンセプトが見つかりません")
        return self._concept_from_row(rows[0])

    def replace_document_concepts(
        self,
        *,
        document_id: str,
        revision_id: str,
        stage_run_id: str | None,
        concepts: Sequence[ConceptExtractionItem],
    ) -> list[DocumentConcept]:
        settings = self.get_concept_settings()
        selected = sorted(
            concepts, key=lambda value: value.confidence, reverse=True
        )[: settings.max_concepts_per_document]
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM sds_document_concepts "
                "WHERE revision_id=:revision_id AND user_locked=0",
                {"revision_id": revision_id},
            )
            for item in selected:
                normalized = normalize_comparable(item.label)
                concept_id = item.existing_concept_id
                if concept_id:
                    cursor.execute(
                        "SELECT concept_id FROM sds_search_concepts "
                        "WHERE concept_id=:concept_id AND status<>'MERGED'",
                        {"concept_id": concept_id},
                    )
                    if not cursor.fetchone():
                        concept_id = None
                if not concept_id:
                    cursor.execute(
                        """
                        SELECT concept_id FROM sds_search_concepts
                        WHERE facet=:facet AND category_code=:category_code
                          AND normalized_label=:normalized AND status<>'MERGED'
                        ORDER BY CASE status WHEN 'ACTIVE' THEN 0 ELSE 1 END
                        FETCH FIRST 1 ROW ONLY
                        """,
                        {
                            "facet": item.facet,
                            "category_code": item.category_code,
                            "normalized": normalized,
                        },
                    )
                    row = cursor.fetchone()
                    concept_id = str(row[0]) if row else uuid4().hex
                cursor.execute(
                    """
                    MERGE INTO sds_search_concepts c
                    USING (SELECT :concept_id concept_id FROM dual) s
                    ON (c.concept_id=s.concept_id)
                    WHEN NOT MATCHED THEN INSERT
                        (concept_id, facet, category_code, category_name,
                         display_label, normalized_label, status)
                    VALUES (:concept_id, :facet, :category_code, :category_name,
                            :display_label, :normalized_label, 'PENDING')
                    """,
                    {
                        "concept_id": concept_id,
                        "facet": item.facet,
                        "category_code": item.category_code,
                        "category_name": clean_text(item.category_name),
                        "display_label": clean_text(item.label),
                        "normalized_label": normalized,
                    },
                )
                cursor.execute(
                    """
                    MERGE INTO sds_document_concepts dc
                    USING (SELECT :revision_id revision_id,
                                  :concept_id concept_id FROM dual) s
                    ON (dc.revision_id=s.revision_id
                        AND dc.concept_id=s.concept_id)
                    WHEN MATCHED THEN UPDATE SET
                        dc.confidence=:confidence, dc.evidence_json=:evidence,
                        dc.source_kinds_json=:source_kinds,
                        dc.stage_run_id=:stage_run_id, dc.updated_at=SYSTIMESTAMP
                    WHEN NOT MATCHED THEN INSERT
                        (document_id, revision_id, concept_id, stage_run_id,
                         confidence, evidence_json, source_kinds_json)
                    VALUES (:document_id, :revision_id, :concept_id, :stage_run_id,
                            :confidence, :evidence, :source_kinds)
                    """,
                    {
                        "document_id": document_id,
                        "revision_id": revision_id,
                        "concept_id": concept_id,
                        "stage_run_id": stage_run_id,
                        "confidence": item.confidence,
                        "evidence": json.dumps(item.evidence, ensure_ascii=False),
                        "source_kinds": json.dumps(
                            list(dict.fromkeys(item.source_kinds)),
                            ensure_ascii=False,
                        ),
                    },
                )
            cursor.execute(
                """
                UPDATE sds_search_concepts c SET
                    support_document_count=(
                        SELECT COUNT(DISTINCT dc.document_id)
                        FROM sds_document_concepts dc
                        JOIN sds_documents d ON d.document_id=dc.document_id
                        JOIN sds_index_releases rel
                          ON rel.release_id=d.serving_release_id
                         AND rel.document_revision_id=dc.revision_id
                         AND rel.status='PUBLISHED'
                        WHERE dc.concept_id=c.concept_id AND d.is_current=1
                          AND d.status<>'DRAFT'
                    ),
                    support_set_count=(
                        SELECT COUNT(DISTINCT m.document_set_id)
                        FROM sds_document_concepts dc
                        JOIN sds_document_metadata m ON m.document_id=dc.document_id
                        JOIN sds_documents d ON d.document_id=dc.document_id
                        JOIN sds_index_releases rel
                          ON rel.release_id=d.serving_release_id
                         AND rel.document_revision_id=dc.revision_id
                         AND rel.status='PUBLISHED'
                        WHERE dc.concept_id=c.concept_id AND d.is_current=1
                          AND d.status<>'DRAFT'
                    ),
                    updated_at=SYSTIMESTAMP
                WHERE c.status<>'MERGED'
                """
            )
            if settings.auto_publish:
                cursor.execute(
                    """
                    UPDATE sds_search_concepts c
                    SET status='ACTIVE', updated_at=SYSTIMESTAMP
                    WHERE c.status='PENDING'
                      AND (
                          SELECT COUNT(DISTINCT m.document_set_id)
                          FROM sds_document_concepts dc
                          JOIN sds_document_metadata m
                            ON m.document_id=dc.document_id
                          JOIN sds_documents d
                            ON d.document_id=dc.document_id
                          JOIN sds_index_releases rel
                            ON rel.release_id=d.serving_release_id
                           AND rel.document_revision_id=dc.revision_id
                           AND rel.status='PUBLISHED'
                          WHERE dc.concept_id=c.concept_id
                            AND m.document_set_id IS NOT NULL
                            AND d.is_current=1 AND d.status<>'DRAFT'
                            AND dc.confidence>=:confidence
                      )>=:min_support
                    """,
                    {
                        "min_support": settings.min_support_sets,
                        "confidence": settings.auto_publish_confidence,
                    },
                )
            connection.commit()
        return self.list_document_concepts(document_id, revision_id=revision_id)

    def list_document_concepts(
        self, document_id: str, *, revision_id: str | None = None
    ) -> list[DocumentConcept]:
        self.require_schema()
        binds: dict[str, Any] = {"document_id": document_id}
        revision_clause = ""
        if revision_id:
            binds["revision_id"] = revision_id
            revision_clause = "AND dc.revision_id=:revision_id"
        else:
            revision_clause = (
                "AND dc.revision_id=(SELECT rel.document_revision_id "
                "FROM sds_documents d JOIN sds_index_releases rel "
                "ON rel.release_id=d.serving_release_id "
                "WHERE d.document_id=:document_id AND rel.status='PUBLISHED')"
            )
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT c.*, dc.confidence, dc.evidence_json,
                       dc.source_kinds_json, dc.user_locked
                FROM sds_document_concepts dc
                JOIN sds_search_concepts c ON c.concept_id=dc.concept_id
                WHERE dc.document_id=:document_id {revision_clause}
                ORDER BY c.facet, c.category_name, dc.confidence DESC
                """,
                binds,
            )
            rows = self.rows(cursor)
        return [
            DocumentConcept(
                concept=self._concept_from_row(row),
                confidence=float(row["confidence"]),
                evidence=_json_value(row.get("evidence_json"), []),
                source_kinds=_json_value(row.get("source_kinds_json"), []),
                user_locked=bool(row.get("user_locked")),
            )
            for row in rows
        ]

    @staticmethod
    def _metadata_from_row(
        row: dict[str, Any], tags: Sequence[DocumentTagAssignment] = ()
    ) -> DocumentMetadata:
        return DocumentMetadata(
            document_id=str(row["document_id"]),
            folder_id=str(row.get("folder_id") or UNCLASSIFIED_FOLDER_ID),
            folder_name=str(row.get("folder_name") or "未分類"),
            document_set_id=(
                str(row["document_set_id"]) if row.get("document_set_id") else None
            ),
            document_set_label=(
                str(row["document_set_label"])
                if row.get("document_set_label") else None
            ),
            document_year=(int(row["document_year"]) if row.get("document_year") else None),
            document_month=(int(row["document_month"]) if row.get("document_month") else None),
            date_precision=str(row.get("date_precision") or "UNKNOWN"),
            date_source=(str(row["date_source"]) if row.get("date_source") else None),
            date_confirmed=bool(row.get("date_confirmed")),
            customer_name_raw=(str(row["customer_name_raw"]) if row.get("customer_name_raw") else None),
            customer_name_normalized=(
                str(row["customer_name_normalized"])
                if row.get("customer_name_normalized") else None
            ),
            customer_name_search_key=(
                str(row["customer_name_search_key"])
                if row.get("customer_name_search_key") else None
            ),
            customer_source=(str(row["customer_source"]) if row.get("customer_source") else None),
            customer_confirmed=bool(row.get("customer_confirmed")),
            customer_confidence=(
                float(row["customer_confidence"])
                if row.get("customer_confidence") is not None else None
            ),
            customer_normalization_version=int(row.get("customer_normalization_version") or 1),
            row_version=int(row.get("row_version") or 1),
            tags=list(tags),
        )

    def _tags_for_documents(
        self, cursor: Any, document_ids: Sequence[str]
    ) -> dict[str, list[DocumentTagAssignment]]:
        if not document_ids:
            return {}
        binds = {f"doc_{index}": value for index, value in enumerate(document_ids)}
        placeholders = ",".join(f":{key}" for key in binds)
        cursor.execute(
            f"""
            SELECT dt.document_id, dt.tag_id, t.code, t.name, g.group_id, g.name,
                   dt.source, dt.confidence, dt.evidence_json,
                   dt.confirmed, dt.user_locked
            FROM sds_document_tags dt
            JOIN sds_tags t ON t.tag_id=dt.tag_id
            JOIN sds_tag_groups g ON g.group_id=t.group_id
            WHERE dt.document_id IN ({placeholders})
            ORDER BY g.sort_order, t.sort_order, t.name
            """,
            binds,
        )
        result: dict[str, list[DocumentTagAssignment]] = {}
        for row in cursor.fetchall():
            result.setdefault(str(row[0]), []).append(
                DocumentTagAssignment(
                    tag_id=str(row[1]),
                    code=str(row[2]),
                    name=str(row[3]),
                    group_id=str(row[4]),
                    group_name=str(row[5]),
                    source=str(row[6]),
                    confidence=float(row[7]) if row[7] is not None else None,
                    evidence=_json_value(row[8], None),
                    confirmed=bool(row[9]),
                    user_locked=bool(row[10]),
                )
            )
        return result

    def get_document_metadata(self, document_id: str) -> DocumentMetadata:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.*, f.name folder_name, ds.label document_set_label
                FROM sds_document_metadata m
                JOIN sds_folders f ON f.folder_id=m.folder_id
                LEFT JOIN sds_document_sets ds
                  ON ds.document_set_id=m.document_set_id
                WHERE m.document_id=:document_id
                """,
                {"document_id": document_id},
            )
            rows = self.rows(cursor)
            if not rows:
                raise LookupError("文書メタデータが見つかりません")
            tags = self._tags_for_documents(cursor, [document_id]).get(document_id, [])
            return self._metadata_from_row(rows[0], tags)

    def _active_ruleset_config(self, cursor: Any) -> ClassificationRuleSetConfig:
        cursor.execute(
            """
            SELECT rr.config_json
            FROM sds_class_rulesets rs
            JOIN sds_class_ruleset_revs rr ON rr.revision_id=rs.current_revision_id
            WHERE rs.enabled=1
            ORDER BY CASE WHEN rs.code='default' THEN 0 ELSE 1 END, rs.code
            FETCH FIRST 1 ROW ONLY
            """
        )
        row = cursor.fetchone()
        return (
            ClassificationRuleSetConfig.model_validate(_json_value(row[0], {}))
            if row else ClassificationRuleSetConfig()
        )

    def patch_document_metadata(
        self,
        document_id: str,
        payload: DocumentMetadataPatch,
        *,
        changed_by_hash: str | None,
    ) -> DocumentMetadata:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM SDS_DOCUMENT_METADATA WHERE DOCUMENT_ID=:document_id FOR UPDATE",
                {"document_id": document_id},
            )
            before_rows = self.rows(cursor)
            if not before_rows:
                raise LookupError("文書メタデータが見つかりません")
            before = before_rows[0]
            if (
                payload.expected_row_version is not None
                and int(before.get("row_version") or 1) != payload.expected_row_version
            ):
                raise RuntimeError("文書メタデータが他の操作で更新されました")
            fields = payload.model_fields_set
            assignments: list[str] = []
            binds: dict[str, Any] = {"document_id": document_id}
            direct_columns = {
                "folder_id": "FOLDER_ID",
                "document_set_id": "DOCUMENT_SET_ID",
                "document_year": "DOCUMENT_YEAR",
                "document_month": "DOCUMENT_MONTH",
                "date_precision": "DATE_PRECISION",
                "date_source": "DATE_SOURCE",
                "date_confirmed": "DATE_CONFIRMED",
                "customer_source": "CUSTOMER_SOURCE",
                "customer_confirmed": "CUSTOMER_CONFIRMED",
                "customer_confidence": "CUSTOMER_CONFIDENCE",
            }
            if "folder_id" in fields and payload.folder_id is not None:
                if not self._folder_exists(cursor, payload.folder_id):
                    raise LookupError("フォルダが見つかりません")
            if "document_set_id" in fields and payload.document_set_id is not None:
                cursor.execute(
                    "SELECT COUNT(*) FROM SDS_DOCUMENT_SETS "
                    "WHERE DOCUMENT_SET_ID=:document_set_id AND STATUS='ACTIVE'",
                    {"document_set_id": payload.document_set_id},
                )
                if not int(cursor.fetchone()[0]):
                    raise LookupError("案件グループが見つかりません")
            for field_name, column_name in direct_columns.items():
                if field_name not in fields:
                    continue
                value = getattr(payload, field_name)
                if isinstance(value, bool):
                    value = int(value)
                binds[field_name] = value
                assignments.append(f"{column_name}=:{field_name}")
            if "customer_name_raw" in fields:
                config = self._active_ruleset_config(cursor)
                normalized = normalize_customer_name(
                    payload.customer_name_raw or "",
                    suffixes=config.customer_suffixes,
                    version=config.normalization_version,
                )
                values = {
                    "customer_name_raw": normalized.raw or None,
                    "customer_name_normalized": normalized.normalized or None,
                    "customer_name_search_key": normalized.search_key or None,
                    "customer_normalization_version": normalized.version,
                }
                for key, value in values.items():
                    binds[key] = value
                    assignments.append(f"{key.upper()}=:{key}")
            if assignments:
                cursor.execute(
                    "UPDATE SDS_DOCUMENT_METADATA SET "
                    + ", ".join(assignments)
                    + ", ROW_VERSION=ROW_VERSION+1, UPDATED_AT=SYSTIMESTAMP "
                    "WHERE DOCUMENT_ID=:document_id",
                    binds,
                )
            if "tag_ids" in fields and payload.tag_ids is not None:
                self._replace_document_tags(cursor, document_id, payload.tag_ids)
                if not assignments:
                    cursor.execute(
                        "UPDATE SDS_DOCUMENT_METADATA SET ROW_VERSION=ROW_VERSION+1, "
                        "UPDATED_AT=SYSTIMESTAMP WHERE DOCUMENT_ID=:document_id",
                        {"document_id": document_id},
                    )
            cursor.execute(
                "SELECT * FROM SDS_DOCUMENT_METADATA WHERE DOCUMENT_ID=:document_id",
                {"document_id": document_id},
            )
            after = self.rows(cursor)[0]
            cursor.execute(
                """
                INSERT INTO SDS_DOC_METADATA_AUDIT
                    (DOCUMENT_ID, CHANGED_BY_HASH, CHANGE_SOURCE, BEFORE_JSON, AFTER_JSON)
                VALUES (:document_id, :changed_by, 'USER', :before_json, :after_json)
                """,
                {
                    "document_id": document_id,
                    "changed_by": changed_by_hash,
                    "before_json": json.dumps(before, ensure_ascii=False, default=str),
                    "after_json": json.dumps(after, ensure_ascii=False, default=str),
                },
            )
            connection.commit()
        return self.get_document_metadata(document_id)

    def _replace_document_tags(
        self, cursor: Any, document_id: str, tag_ids: Sequence[str]
    ) -> None:
        unique_ids = list(dict.fromkeys(str(value) for value in tag_ids))
        self._validate_tag_ids(cursor, unique_ids)
        cursor.execute(
            "DELETE FROM SDS_DOCUMENT_TAGS WHERE DOCUMENT_ID=:document_id",
            {"document_id": document_id},
        )
        for tag_id in unique_ids:
            cursor.execute(
                """
                INSERT INTO SDS_DOCUMENT_TAGS
                    (DOCUMENT_ID, TAG_ID, SOURCE, CONFIRMED, USER_LOCKED)
                VALUES (:document_id, :tag_id, 'USER', 1, 1)
                """,
                {"document_id": document_id, "tag_id": tag_id},
            )

    @staticmethod
    def _validate_tag_ids(cursor: Any, tag_ids: Sequence[str]) -> None:
        unique_ids = list(dict.fromkeys(str(value) for value in tag_ids))
        if not unique_ids:
            return
        binds = {f"validate_tag_{index}": value for index, value in enumerate(unique_ids)}
        placeholders = ",".join(f":{key}" for key in binds)
        cursor.execute(
            f"""
            SELECT t.tag_id, g.group_id, g.selection_mode
            FROM sds_tags t JOIN sds_tag_groups g ON g.group_id=t.group_id
            WHERE t.active=1 AND g.active=1 AND t.tag_id IN ({placeholders})
            """,
            binds,
        )
        found = cursor.fetchall()
        if len(found) != len(unique_ids):
            raise LookupError("指定されたタグが見つかりません")
        single_groups: dict[str, int] = {}
        for _, group_id, selection_mode in found:
            if str(selection_mode) == "SINGLE":
                single_groups[str(group_id)] = single_groups.get(str(group_id), 0) + 1
        if any(value > 1 for value in single_groups.values()):
            raise ValueError("排他タググループから複数のタグを選択できません")

    def list_documents(
        self,
        *,
        page: int,
        page_size: int,
        folder_id: str | None = None,
        include_descendants: bool = False,
        query: str | None = None,
        sort: str = "updated_desc",
    ) -> DocumentLibraryResponse:
        self.require_schema()
        order_by = {
            "updated_desc": "d.updated_at DESC NULLS LAST, d.document_id",
            "created_desc": "d.uploaded_at DESC NULLS LAST, d.document_id",
            "updated_asc": "d.updated_at ASC NULLS LAST, d.document_id",
            "filename_asc": "LOWER(d.file_name) ASC, d.file_name ASC, d.document_id",
        }.get(sort)
        if order_by is None:
            raise ValueError("未対応の文書並び順です")
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        clauses = ["d.is_current=1", "d.status<>'DRAFT'"]
        binds: dict[str, Any] = {}
        if folder_id:
            binds["folder_id"] = folder_id
            if include_descendants:
                clauses.append(
                    "EXISTS (SELECT 1 FROM sds_folder_closure fc "
                    "WHERE fc.ancestor_folder_id=:folder_id "
                    "AND fc.descendant_folder_id=m.folder_id)"
                )
            else:
                clauses.append("m.folder_id=:folder_id")
        if query and query.strip():
            binds["query"] = query.strip()
            clauses.append(
                "(LOWER(d.file_name) LIKE '%' || LOWER(:query) || '%' "
                "OR LOWER(m.customer_name_normalized) LIKE '%' || LOWER(:query) || '%')"
            )
        where = " AND ".join(clauses)
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*)
                FROM sds_documents d
                JOIN sds_document_metadata m ON m.document_id=d.document_id
                WHERE {where}
                """,
                binds,
            )
            total = int(cursor.fetchone()[0])
            offset = (page - 1) * page_size
            cursor.execute(
                f"""
                SELECT d.document_id, d.bucket, d.object_name, d.file_name, d.media_type,
                       d.file_size, d.status, d.uploaded_at, d.updated_at,
                       m.*, f.name folder_name, ds.label document_set_label
                FROM sds_documents d
                JOIN sds_document_metadata m ON m.document_id=d.document_id
                JOIN sds_folders f ON f.folder_id=m.folder_id
                LEFT JOIN sds_document_sets ds
                  ON ds.document_set_id=m.document_set_id
                WHERE {where}
                ORDER BY {order_by}
                OFFSET {offset} ROWS FETCH NEXT {page_size} ROWS ONLY
                """,
                binds,
            )
            rows = self.rows(cursor)
            document_ids = [str(row["document_id"]) for row in rows]
            tags = self._tags_for_documents(cursor, document_ids)
        items = [
            DocumentLibraryItem(
                document_id=str(row["document_id"]),
                bucket=str(row["bucket"]),
                object_name=str(row["object_name"]),
                file_name=str(row["file_name"]),
                media_type=(str(row["media_type"]) if row.get("media_type") else None),
                file_size=(int(row["file_size"]) if row.get("file_size") is not None else None),
                status=str(row["status"]),
                uploaded_at=row.get("uploaded_at"),
                updated_at=row.get("updated_at"),
                metadata=self._metadata_from_row(
                    row, tags.get(str(row["document_id"]), [])
                ),
            )
            for row in rows
        ]
        return DocumentLibraryResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=max(1, math.ceil(total / page_size)) if total else 1,
        )

    def documents_for_action(
        self, document_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        """Resolve current, registered documents without changing their order."""
        self.require_schema()
        values = list(
            dict.fromkeys(str(value).strip() for value in document_ids if value)
        )
        if not values:
            return []
        found: dict[str, dict[str, Any]] = {}
        with self.connection() as connection, connection.cursor() as cursor:
            for start in range(0, len(values), 900):
                batch = values[start : start + 900]
                binds = {
                    f"action_document_{index}": value
                    for index, value in enumerate(batch)
                }
                placeholders = ", ".join(f":{key}" for key in binds)
                cursor.execute(
                    f"""
                    SELECT document_id, bucket, object_name, file_name, status
                    FROM sds_documents
                    WHERE is_current=1 AND status<>'DRAFT'
                      AND document_id IN ({placeholders})
                    """,
                    binds,
                )
                for row in self.rows(cursor):
                    found[str(row["document_id"])] = row
        return [found[value] for value in values if value in found]

    def document_artifact_object_names(self, document_id: str) -> list[str]:
        """Return generated Object Storage objects for a registered document."""
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT a.object_name
                FROM sds_artifacts a
                JOIN sds_document_revisions r
                  ON r.revision_id=a.document_revision_id
                WHERE r.document_id=:document AND a.object_name IS NOT NULL
                ORDER BY a.object_name
                """,
                {"document": document_id},
            )
            return [str(row[0]) for row in cursor.fetchall()]

    def delete_registered_document(self, document_id: str) -> int:
        """Delete one document while retaining detached pipeline audit history."""
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE sds_documents
                SET current_revision_id=NULL, serving_release_id=NULL,
                    draft_release_id=NULL, updated_at=SYSTIMESTAMP
                WHERE document_id=:document AND is_current=1 AND status<>'DRAFT'
                """,
                {"document": document_id},
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return 0
            cursor.execute(
                """
                UPDATE sds_pipeline_job_steps
                SET document_id=NULL, document_revision_id=NULL,
                    release_id=NULL, stage_run_id=NULL
                WHERE document_id=:document
                   OR document_revision_id IN (
                        SELECT revision_id FROM sds_document_revisions
                        WHERE document_id=:document
                   )
                   OR release_id IN (
                        SELECT release_id FROM sds_index_releases
                        WHERE document_id=:document
                   )
                   OR stage_run_id IN (
                        SELECT sr.stage_run_id
                        FROM sds_stage_runs sr
                        JOIN sds_document_revisions r
                          ON r.revision_id=sr.document_revision_id
                        WHERE r.document_id=:document
                   )
                """,
                {"document": document_id},
            )
            # Ingest items deliberately keep a required reference to the
            # document so an unfinished upload cannot become detached from its
            # draft. Registered-document deletion is the other lifecycle:
            # remove those upload records explicitly before deleting the
            # document. The item delete also cascades to item-scoped class
            # candidates.
            cursor.execute(
                "DELETE FROM sds_ingest_items WHERE document_id=:document",
                {"document": document_id},
            )
            cursor.execute(
                "DELETE FROM sds_documents WHERE document_id=:document",
                {"document": document_id},
            )
            count = int(cursor.rowcount)
            connection.commit()
            return count

    def registered_object_names(self, object_names: Sequence[str]) -> set[str]:
        self.require_schema()
        values = list(dict.fromkeys(str(value) for value in object_names if value))
        registered: set[str] = set()
        with self.connection() as connection, connection.cursor() as cursor:
            for start in range(0, len(values), 900):
                batch = values[start : start + 900]
                binds = {
                    f"known_object_{index}": value
                    for index, value in enumerate(batch)
                }
                placeholders = ", ".join(f":{key}" for key in binds)
                cursor.execute(
                    f"SELECT object_name FROM sds_documents "
                    f"WHERE object_name IN ({placeholders})",
                    binds,
                )
                registered.update(str(row[0]) for row in cursor.fetchall())
        return registered

    def register_uploaded_document(
        self,
        *,
        document_id: str,
        bucket: str,
        object_name: str,
        original_filename: str,
        media_type: str | None,
        file_size: int,
        folder_id: str = UNCLASSIFIED_FOLDER_ID,
        storage_key_version: int = 2,
        status: str = "UNPROCESSED",
        metadata_source: str | None = None,
    ) -> None:
        self.require_schema()
        media_type = media_type or mimetypes.guess_type(original_filename)[0] or "application/octet-stream"
        document_type = PurePosixPath(original_filename).suffix.casefold().removeprefix(".") or None
        with self.connection() as connection, connection.cursor() as cursor:
            if not self._folder_exists(cursor, folder_id):
                raise LookupError("登録先フォルダが見つかりません")
            cursor.execute(
                """
                MERGE INTO SDS_DOCUMENTS d
                USING (SELECT :document_id document_id FROM dual) s
                ON (d.document_id=s.document_id)
                WHEN MATCHED THEN UPDATE SET
                    d.bucket=:bucket, d.object_name=:object_name, d.file_name=:file_name,
                    d.media_type=:media_type, d.document_type=:document_type,
                    d.file_size=:file_size, d.storage_key_version=:key_version,
                    d.updated_at=SYSTIMESTAMP
                WHEN NOT MATCHED THEN INSERT
                    (document_id, bucket, object_name, file_name, media_type,
                     document_type, file_size, storage_key_version, status)
                VALUES (:document_id, :bucket, :object_name, :file_name, :media_type,
                        :document_type, :file_size, :key_version, :status)
                """,
                {
                    "document_id": document_id,
                    "bucket": bucket,
                    "object_name": object_name,
                    "file_name": clean_text(original_filename),
                    "media_type": media_type,
                    "document_type": document_type,
                    "file_size": file_size,
                    "key_version": storage_key_version,
                    "status": status,
                },
            )
            cursor.execute(
                """
                MERGE INTO SDS_DOCUMENT_METADATA m
                USING (SELECT :document_id document_id FROM dual) s
                ON (m.document_id=s.document_id)
                WHEN MATCHED THEN UPDATE SET m.folder_id=:folder_id,
                    m.updated_at=SYSTIMESTAMP
                WHEN NOT MATCHED THEN INSERT (document_id, folder_id, date_source)
                    VALUES (:document_id, :folder_id, :metadata_source)
                """,
                {
                    "document_id": document_id,
                    "folder_id": folder_id,
                    "metadata_source": metadata_source,
                },
            )
            cursor.execute(
                """
                MERGE INTO SDS_DOCUMENT_ACL a
                USING (SELECT :document_id document_id FROM dual) s
                ON (a.document_id=s.document_id AND a.principal_type='public_authenticated'
                    AND a.principal_hash=:principal)
                WHEN NOT MATCHED THEN INSERT
                    (document_id, principal_type, principal_hash, permission)
                VALUES (:document_id, 'public_authenticated', :principal, 'read')
                """,
                {"document_id": document_id, "principal": "0" * 64},
            )
            connection.commit()

    def discard_draft_document(self, document_id: str) -> None:
        """Remove only an uncommitted draft created by the new ingest flow."""
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, serving_release_id FROM sds_documents "
                "WHERE document_id=:document_id FOR UPDATE",
                {"document_id": document_id},
            )
            row = cursor.fetchone()
            if not row:
                return
            if str(row[0]) != "DRAFT" or row[1] is not None:
                raise ValueError("登録済み文書はドラフト破棄できません")
            cursor.execute(
                "DELETE FROM sds_class_candidates WHERE document_id=:document_id",
                {"document_id": document_id},
            )
            cursor.execute(
                "DELETE FROM sds_ingest_items WHERE document_id=:document_id",
                {"document_id": document_id},
            )
            cursor.execute(
                "DELETE FROM sds_documents WHERE document_id=:document_id AND status='DRAFT'",
                {"document_id": document_id},
            )
            connection.commit()

    def customer_suggestions(
        self,
        *,
        query: str,
        user_hash: str | None,
        limit: int = 20,
    ) -> list[CustomerSuggestion]:
        self.require_schema()
        limit = max(1, min(limit, 100))
        normalized = normalize_comparable(query)
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT customer_name_raw, customer_name_normalized,
                       customer_name_search_key, COUNT(*) document_count,
                       MAX(m.updated_at) last_used_at
                FROM sds_document_metadata m
                JOIN sds_documents d ON d.document_id=m.document_id
                WHERE m.customer_confirmed=1
                  AND m.customer_name_normalized IS NOT NULL
                  AND (:query_value IS NULL OR m.customer_name_normalized LIKE :query_value || '%'
                       OR m.customer_name_normalized LIKE '%' || :query_value || '%')
                  AND EXISTS (
                      SELECT 1 FROM sds_document_acl acl
                      WHERE acl.document_id=d.document_id
                        AND :user_hash IS NOT NULL
                        AND (acl.principal_type='public_authenticated'
                             OR (acl.principal_type IN ('user', 'service')
                                 AND acl.principal_hash=:user_hash))
                  )
                GROUP BY customer_name_raw, customer_name_normalized,
                         customer_name_search_key
                ORDER BY CASE WHEN customer_name_normalized=:query_exact THEN 0
                              WHEN customer_name_normalized LIKE :query_prefix || '%' THEN 1
                              ELSE 2 END,
                         COUNT(*) DESC, MAX(m.updated_at) DESC
                FETCH FIRST {limit} ROWS ONLY
                """,
                {
                    "query_value": normalized or None,
                    "query_exact": normalized,
                    "query_prefix": normalized,
                    "user_hash": user_hash,
                },
            )
            return [
                CustomerSuggestion(
                    value=str(row[0]),
                    normalized=str(row[1]),
                    search_key=str(row[2] or row[1]),
                    document_count=int(row[3]),
                    last_used_at=row[4],
                    similarity_warning=(bool(normalized) and str(row[1]) != normalized),
                )
                for row in cursor.fetchall()
            ]

    def document_date_bounds(self) -> dict[str, int | None]:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT MIN(document_year), MAX(document_year) "
                "FROM sds_document_metadata WHERE date_confirmed=1"
            )
            row = cursor.fetchone() or (None, None)
        return {
            "min_year": int(row[0]) if row[0] is not None else None,
            "max_year": int(row[1]) if row[1] is not None else None,
        }

    def list_tag_groups(self) -> list[TagGroup]:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT group_id, code, name, selection_mode, active, sort_order "
                "FROM sds_tag_groups ORDER BY sort_order, name"
            )
            return [
                TagGroup(
                    group_id=str(row[0]), code=str(row[1]), name=str(row[2]),
                    selection_mode=str(row[3]), active=bool(row[4]), sort_order=int(row[5]),
                )
                for row in cursor.fetchall()
            ]

    def upsert_tag_group(self, payload: TagGroupUpsert, group_id: str | None = None) -> TagGroup:
        self.require_schema()
        group_id = group_id or uuid4().hex
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                MERGE INTO sds_tag_groups g USING (SELECT :group_id group_id FROM dual) s
                ON (g.group_id=s.group_id)
                WHEN MATCHED THEN UPDATE SET g.code=:code, g.name=:name,
                    g.selection_mode=:selection_mode_bind, g.active=:active,
                    g.sort_order=:sort_order,
                    g.updated_at=SYSTIMESTAMP
                WHEN NOT MATCHED THEN INSERT
                    (group_id, code, name, selection_mode, active, sort_order)
                VALUES (:group_id, :code, :name, :selection_mode_bind, :active, :sort_order)
                """,
                {
                    "group_id": group_id, "code": payload.code, "name": payload.name,
                    "selection_mode_bind": payload.selection_mode,
                    "active": int(payload.active),
                    "sort_order": payload.sort_order,
                },
            )
            connection.commit()
        return TagGroup(group_id=group_id, **payload.model_dump())

    def list_tags(self) -> list[TagDefinition]:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT t.tag_id, t.group_id, t.code, t.name, t.active, t.sort_order,
                       g.code, g.name, g.selection_mode
                FROM sds_tags t JOIN sds_tag_groups g ON g.group_id=t.group_id
                ORDER BY g.sort_order, t.sort_order, t.name
                """
            )
            return [
                TagDefinition(
                    tag_id=str(row[0]), group_id=str(row[1]), code=str(row[2]),
                    name=str(row[3]), active=bool(row[4]), sort_order=int(row[5]),
                    group_code=str(row[6]), group_name=str(row[7]),
                    selection_mode=str(row[8]),
                )
                for row in cursor.fetchall()
            ]

    def upsert_tag(self, payload: TagUpsert, tag_id: str | None = None) -> TagDefinition:
        self.require_schema()
        tag_id = tag_id or uuid4().hex
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT code, name, selection_mode FROM sds_tag_groups "
                "WHERE group_id=:group_id",
                {"group_id": payload.group_id},
            )
            group = cursor.fetchone()
            if not group:
                raise LookupError("タググループが見つかりません")
            cursor.execute(
                """
                MERGE INTO sds_tags t USING (SELECT :tag_id tag_id FROM dual) s
                ON (t.tag_id=s.tag_id)
                WHEN MATCHED THEN UPDATE SET t.group_id=:group_id, t.code=:code,
                    t.name=:name, t.active=:active, t.sort_order=:sort_order,
                    t.updated_at=SYSTIMESTAMP
                WHEN NOT MATCHED THEN INSERT
                    (tag_id, group_id, code, name, active, sort_order)
                VALUES (:tag_id, :group_id, :code, :name, :active, :sort_order)
                """,
                {
                    "tag_id": tag_id, "group_id": payload.group_id, "code": payload.code,
                    "name": payload.name, "active": int(payload.active),
                    "sort_order": payload.sort_order,
                },
            )
            connection.commit()
        return TagDefinition(
            tag_id=tag_id, **payload.model_dump(), group_code=str(group[0]),
            group_name=str(group[1]), selection_mode=str(group[2]),
        )

    def list_rulesets(self) -> list[ClassificationRuleSet]:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT rs.ruleset_id, rs.code, rs.name, rs.enabled,
                       rr.revision_id, rr.revision_no, rr.config_hash, rr.config_json
                FROM sds_class_rulesets rs
                JOIN sds_class_ruleset_revs rr ON rr.revision_id=rs.current_revision_id
                ORDER BY rs.code
                """
            )
            return [
                ClassificationRuleSet(
                    ruleset_id=str(row[0]), code=str(row[1]), name=str(row[2]),
                    enabled=bool(row[3]), revision_id=str(row[4]), revision_no=int(row[5]),
                    config_hash=str(row[6]),
                    config=ClassificationRuleSetConfig.model_validate(_json_value(row[7], {})),
                )
                for row in cursor.fetchall()
            ]

    def upsert_ruleset(
        self, payload: ClassificationRuleSetUpsert, ruleset_id: str | None = None
    ) -> ClassificationRuleSet:
        self.require_schema()
        ruleset_id = ruleset_id or uuid4().hex
        config_json = payload.config.model_dump_json()
        config_hash = hashlib.sha256(config_json.encode()).hexdigest()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_revision_id FROM sds_class_rulesets "
                "WHERE ruleset_id=:ruleset_id FOR UPDATE",
                {"ruleset_id": ruleset_id},
            )
            existing = cursor.fetchone()
            if existing:
                cursor.execute(
                    "SELECT config_hash, revision_no FROM sds_class_ruleset_revs "
                    "WHERE revision_id=:revision_id",
                    {"revision_id": existing[0]},
                )
                revision = cursor.fetchone()
                revision_no = int(revision[1])
                revision_id = str(existing[0])
                if str(revision[0]) != config_hash:
                    revision_no += 1
                    revision_id = uuid4().hex
                    cursor.execute(
                        """
                        INSERT INTO sds_class_ruleset_revs
                            (revision_id, ruleset_id, revision_no, config_hash, config_json)
                        VALUES (:revision_id, :ruleset_id, :revision_no, :hash, :config)
                        """,
                        {
                            "revision_id": revision_id, "ruleset_id": ruleset_id,
                            "revision_no": revision_no, "hash": config_hash,
                            "config": config_json,
                        },
                    )
                cursor.execute(
                    """
                    UPDATE sds_class_rulesets SET code=:code, name=:name, enabled=:enabled,
                        current_revision_id=:revision_id, updated_at=SYSTIMESTAMP
                    WHERE ruleset_id=:ruleset_id
                    """,
                    {
                        "code": payload.code, "name": payload.name,
                        "enabled": int(payload.enabled), "revision_id": revision_id,
                        "ruleset_id": ruleset_id,
                    },
                )
            else:
                revision_no = 1
                revision_id = uuid4().hex
                cursor.execute(
                    "INSERT INTO sds_class_rulesets (ruleset_id, code, name, enabled) "
                    "VALUES (:ruleset_id, :code, :name, :enabled)",
                    {
                        "ruleset_id": ruleset_id, "code": payload.code,
                        "name": payload.name, "enabled": int(payload.enabled),
                    },
                )
                cursor.execute(
                    """
                    INSERT INTO sds_class_ruleset_revs
                        (revision_id, ruleset_id, revision_no, config_hash, config_json)
                    VALUES (:revision_id, :ruleset_id, 1, :hash, :config)
                    """,
                    {
                        "revision_id": revision_id, "ruleset_id": ruleset_id,
                        "hash": config_hash, "config": config_json,
                    },
                )
                cursor.execute(
                    "UPDATE sds_class_rulesets SET current_revision_id=:revision_id "
                    "WHERE ruleset_id=:ruleset_id",
                    {"revision_id": revision_id, "ruleset_id": ruleset_id},
                )
            connection.commit()
        return ClassificationRuleSet(
            ruleset_id=ruleset_id, revision_id=revision_id, revision_no=revision_no,
            config_hash=config_hash, **payload.model_dump(),
        )

    def resolve_ruleset(
        self, *, ruleset_id: str | None = None, folder_id: str | None = None
    ) -> ClassificationRuleSet:
        rulesets = self.list_rulesets()
        if ruleset_id:
            for item in rulesets:
                if item.ruleset_id == ruleset_id:
                    return item
            raise LookupError("分類ルールセットが見つかりません")
        if folder_id:
            with self.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT fr.ruleset_id
                    FROM sds_folder_closure path
                    JOIN sds_folder_rulesets fr
                      ON fr.folder_id=path.ancestor_folder_id
                    WHERE path.descendant_folder_id=:folder_id
                      AND (path.depth=0 OR fr.inherit_to_descendants=1)
                    ORDER BY path.depth
                    FETCH FIRST 1 ROW ONLY
                    """,
                    {"folder_id": folder_id},
                )
                row = cursor.fetchone()
                if row:
                    return self.resolve_ruleset(ruleset_id=str(row[0]))
        for item in rulesets:
            if item.code == "default" and item.enabled:
                return item
        for item in rulesets:
            if item.enabled:
                return item
        raise LookupError("有効な分類ルールセットがありません")

    def list_folder_rule_profiles(self) -> list[FolderRuleProfile]:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT fr.folder_id, fr.ruleset_id, fr.inherit_to_descendants,
                       fr.defaults_json
                FROM sds_folder_rulesets fr
                JOIN sds_folders f ON f.folder_id=fr.folder_id
                ORDER BY f.name, fr.folder_id
                """
            )
            return [
                FolderRuleProfile(
                    folder_id=str(row[0]),
                    ruleset_id=str(row[1]),
                    inherit_to_descendants=bool(row[2]),
                    defaults=FolderDefaults.model_validate(_json_value(row[3], {})),
                )
                for row in cursor.fetchall()
            ]

    def upsert_folder_rule_profile(
        self, folder_id: str, payload: FolderRuleProfileUpsert
    ) -> FolderRuleProfile:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            if not self._folder_exists(cursor, folder_id):
                raise LookupError("フォルダが見つかりません")
            cursor.execute(
                "SELECT COUNT(*) FROM sds_class_rulesets WHERE ruleset_id=:ruleset_id",
                {"ruleset_id": payload.ruleset_id},
            )
            if not int(cursor.fetchone()[0]):
                raise LookupError("分類ルールセットが見つかりません")
            if payload.defaults.tag_ids:
                self._validate_tag_ids(cursor, payload.defaults.tag_ids)
            cursor.execute(
                """
                MERGE INTO sds_folder_rulesets f
                USING (SELECT :folder_id folder_id FROM dual) s
                ON (f.folder_id=s.folder_id)
                WHEN MATCHED THEN UPDATE SET f.ruleset_id=:ruleset_id,
                    f.inherit_to_descendants=:inherit, f.defaults_json=:defaults,
                    f.updated_at=SYSTIMESTAMP
                WHEN NOT MATCHED THEN INSERT
                    (folder_id, ruleset_id, inherit_to_descendants, defaults_json)
                VALUES (:folder_id, :ruleset_id, :inherit, :defaults)
                """,
                {
                    "folder_id": folder_id,
                    "ruleset_id": payload.ruleset_id,
                    "inherit": int(payload.inherit_to_descendants),
                    "defaults": payload.defaults.model_dump_json(),
                },
            )
            connection.commit()
        return FolderRuleProfile(folder_id=folder_id, **payload.model_dump())

    def resolve_folder_defaults(self, folder_id: str) -> FolderDefaults:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT fr.defaults_json
                FROM sds_folder_closure path
                JOIN sds_folder_rulesets fr
                  ON fr.folder_id=path.ancestor_folder_id
                WHERE path.descendant_folder_id=:folder_id
                  AND (path.depth=0 OR fr.inherit_to_descendants=1)
                ORDER BY path.depth
                FETCH FIRST 1 ROW ONLY
                """,
                {"folder_id": folder_id},
            )
            row = cursor.fetchone()
        return FolderDefaults.model_validate(_json_value(row[0], {})) if row else FolderDefaults()

    def create_ingest_batch(
        self,
        *,
        target_folder_id: str,
        ruleset_id: str | None,
        created_by_hash: str | None,
    ) -> IngestBatch:
        self.require_schema()
        batch_id = uuid4().hex
        ruleset = self.resolve_ruleset(ruleset_id=ruleset_id, folder_id=target_folder_id)
        with self.connection() as connection, connection.cursor() as cursor:
            if not self._folder_exists(cursor, target_folder_id):
                raise LookupError("登録先フォルダが見つかりません")
            cursor.execute(
                """
                INSERT INTO SDS_INGEST_BATCHES
                    (BATCH_ID, STATUS, TARGET_FOLDER_ID, RULESET_ID, CREATED_BY_HASH)
                VALUES (:batch_id, 'DRAFT', :folder_id, :ruleset_id, :created_by)
                """,
                {
                    "batch_id": batch_id,
                    "folder_id": target_folder_id,
                    "ruleset_id": ruleset.ruleset_id,
                    "created_by": created_by_hash,
                },
            )
            connection.commit()
        return IngestBatch(
            batch_id=batch_id,
            status="DRAFT",
            target_folder_id=target_folder_id,
            ruleset_id=ruleset.ruleset_id,
        )

    def require_ingest_batch_owner(
        self, batch_id: str, created_by_hash: str | None
    ) -> None:
        """Hide ingest batches from users other than their creator."""
        if not created_by_hash:
            raise LookupError("取込バッチが見つかりません")
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT created_by_hash FROM sds_ingest_batches "
                "WHERE batch_id=:batch_id",
                {"batch_id": batch_id},
            )
            row = cursor.fetchone()
        if not row or str(row[0] or "") != created_by_hash:
            raise LookupError("取込バッチが見つかりません")

    def list_active_ingest_batches(
        self, created_by_hash: str | None
    ) -> list[ActiveIngestBatch]:
        if not created_by_hash:
            return []
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT b.batch_id, b.status, b.target_folder_id, b.ruleset_id,
                       b.pipeline_job_id, b.total_items, b.confirmed_items,
                       b.failed_items, b.created_at, b.updated_at, f.name,
                       (SELECT COUNT(*) FROM sds_ingest_items i
                        WHERE i.batch_id=b.batch_id
                          AND (i.llm_result_json IS NOT NULL OR i.state IN
                              ('LLM_SKIPPED', 'REVIEW_REQUIRED', 'CONFIRMED',
                               'COMMITTING', 'REGISTERED', 'INDEX_QUEUED', 'INDEXED'))
                       ) analysis_completed_items,
                       (SELECT COUNT(*) FROM sds_ingest_items i
                        WHERE i.batch_id=b.batch_id
                          AND i.llm_result_json IS NULL
                          AND i.state NOT IN
                              ('LLM_SKIPPED', 'REVIEW_REQUIRED', 'CONFIRMED',
                               'COMMITTING', 'REGISTERED', 'INDEX_QUEUED', 'INDEXED')
                       ) analysis_pending_items,
                       (SELECT COUNT(*) FROM sds_ingest_items i
                        WHERE i.batch_id=b.batch_id
                          AND i.state IN ('REGISTERED', 'INDEX_QUEUED', 'INDEXED')
                       ) registered_items,
                       CASE WHEN NOT EXISTS (
                           SELECT 1
                           FROM sds_ingest_items i
                           JOIN sds_documents d ON d.document_id=i.document_id
                           WHERE i.batch_id=b.batch_id
                             AND (d.status<>'DRAFT' OR d.serving_release_id IS NOT NULL)
                       ) THEN 1 ELSE 0 END discardable
                FROM sds_ingest_batches b
                JOIN sds_folders f ON f.folder_id=b.target_folder_id
                WHERE b.created_by_hash=:created_by_hash
                  AND b.status IN ('DRAFT', 'REVIEW_REQUIRED')
                  AND b.total_items > 0
                ORDER BY b.updated_at DESC, b.created_at DESC, b.batch_id DESC
                """,
                {"created_by_hash": created_by_hash},
            )
            rows = cursor.fetchall()
        return [
            ActiveIngestBatch(
                batch_id=str(row[0]),
                status=str(row[1]),
                target_folder_id=str(row[2]),
                ruleset_id=str(row[3]) if row[3] else None,
                pipeline_job_id=str(row[4]) if row[4] else None,
                total_items=int(row[5] or 0),
                confirmed_items=int(row[6] or 0),
                failed_items=int(row[7] or 0),
                created_at=row[8],
                updated_at=row[9],
                target_folder_name=str(row[10]),
                analysis_completed_items=int(row[11] or 0),
                analysis_pending_items=int(row[12] or 0),
                registered_items=int(row[13] or 0),
                discardable=bool(row[14]),
            )
            for row in rows
        ]

    def ensure_ingest_batch_discardable(self, batch_id: str) -> None:
        """Reject cancellation before any Object Storage deletion is attempted."""
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT b.status,
                       (SELECT COUNT(*)
                        FROM sds_ingest_items i
                        JOIN sds_documents d ON d.document_id=i.document_id
                        WHERE i.batch_id=b.batch_id
                          AND (d.status<>'DRAFT' OR d.serving_release_id IS NOT NULL))
                FROM sds_ingest_batches b
                WHERE b.batch_id=:batch_id
                """,
                {"batch_id": batch_id},
            )
            row = cursor.fetchone()
        if not row:
            raise LookupError("取込バッチが見つかりません")
        if str(row[0]) not in {"DRAFT", "REVIEW_REQUIRED"} or int(row[1] or 0):
            raise ValueError("登録処理が開始されたバッチはキャンセルできません")

    def add_ingest_item(
        self,
        *,
        batch_id: str,
        document_id: str,
        original_filename: str,
        object_name: str,
        media_type: str | None,
        file_size: int,
        content_sha256: str,
        folder_id: str,
        rule_result: Any,
        ruleset_config: ClassificationRuleSetConfig,
    ) -> str:
        self.require_schema()
        item_id = uuid4().hex
        candidates = list(rule_result.candidates)
        defaults = self.resolve_folder_defaults(folder_id)
        definitions = {item.tag_id: item for item in self.list_tags()}
        resolved_tag_ids = {
            str(item.target_key)
            for item in candidates
            if item.field_kind == "TAG"
            and item.target_key in definitions
            and item.confirmed
            and not item.ambiguous
        }
        resolved_single_groups = {
            definitions[item.target_key].group_id
            for item in candidates
            if item.field_kind == "TAG"
            and item.target_key in definitions
            and item.confirmed
            and not item.ambiguous
            and definitions[item.target_key].selection_mode == "SINGLE"
        }
        for tag_id in defaults.tag_ids:
            tag = definitions.get(tag_id)
            if (
                tag is None
                or tag.tag_id in resolved_tag_ids
                or (
                    tag.selection_mode == "SINGLE"
                    and tag.group_id in resolved_single_groups
                )
            ):
                continue
            candidates.append(
                RuleCandidate(
                    field_kind="TAG",
                    target_key=tag.tag_id,
                    value_raw=tag.name,
                    value_normalized=tag.code,
                    source="FOLDER_DEFAULT",
                    confidence=1,
                    evidence={"folder_id": folder_id},
                    confirmed=True,
                )
            )
            resolved_tag_ids.add(tag.tag_id)
            if tag.selection_mode == "SINGLE":
                resolved_single_groups.add(tag.group_id)
        if defaults.customer_name_raw and not any(
            item.field_kind == "CUSTOMER" for item in candidates
        ):
            normalized = normalize_customer_name(
                defaults.customer_name_raw,
                suffixes=ruleset_config.customer_suffixes,
                version=ruleset_config.normalization_version,
            )
            candidates.append(
                RuleCandidate(
                    field_kind="CUSTOMER",
                    target_key="customer_name",
                    value_raw=normalized.raw,
                    value_normalized=normalized.normalized,
                    source="FOLDER_DEFAULT",
                    confidence=1,
                    evidence={"folder_id": folder_id},
                    confirmed=True,
                )
            )
        if defaults.document_year and not any(
            item.field_kind == "DATE" for item in candidates
        ):
            date_value = (
                f"{defaults.document_year:04d}-{defaults.document_month:02d}"
                if defaults.document_month
                else f"{defaults.document_year:04d}"
            )
            candidates.append(
                RuleCandidate(
                    field_kind="DATE",
                    target_key="document_year_month",
                    value_raw=date_value,
                    value_normalized=date_value,
                    source="FOLDER_DEFAULT",
                    confidence=1,
                    evidence={
                        "folder_id": folder_id,
                        "precision": defaults.date_precision,
                    },
                    confirmed=True,
                )
            )
        rule_json = (
            rule_result.model_dump_json()
            if hasattr(rule_result, "model_dump_json")
            else json.dumps(rule_result, ensure_ascii=False)
        )
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO SDS_INGEST_ITEMS
                    (ITEM_ID, BATCH_ID, DOCUMENT_ID, ORIGINAL_FILENAME, OBJECT_NAME,
                     MEDIA_TYPE, FILE_SIZE, CONTENT_SHA256, STATE, FOLDER_ID,
                     RULE_RESULT_JSON)
                VALUES (:item_id, :batch_id, :document_id, :filename, :object_name,
                        :media_type, :file_size, :content_sha, 'RULE_CLASSIFIED',
                        :folder_id, :rule_result)
                """,
                {
                    "item_id": item_id,
                    "batch_id": batch_id,
                    "document_id": document_id,
                    "filename": original_filename,
                    "object_name": object_name,
                    "media_type": media_type,
                    "file_size": file_size,
                    "content_sha": content_sha256,
                    "folder_id": folder_id,
                    "rule_result": rule_json,
                },
            )
            for candidate in candidates:
                self._insert_candidate(cursor, item_id, document_id, candidate)
            cursor.execute(
                """
                UPDATE SDS_INGEST_BATCHES
                SET TOTAL_ITEMS=TOTAL_ITEMS+1, STATUS='REVIEW_REQUIRED',
                    UPDATED_AT=SYSTIMESTAMP
                WHERE BATCH_ID=:batch_id
                """,
                {"batch_id": batch_id},
            )
            connection.commit()
        return item_id

    @staticmethod
    def _insert_candidate(
        cursor: Any,
        item_id: str,
        document_id: str,
        candidate: RuleCandidate,
    ) -> None:
        status = "CONFIRMED" if candidate.confirmed and not candidate.ambiguous else "PENDING"
        cursor.execute(
            """
            INSERT INTO SDS_CLASS_CANDIDATES
                (CANDIDATE_ID, ITEM_ID, DOCUMENT_ID, FIELD_KIND, TARGET_KEY,
                 VALUE_RAW, VALUE_NORMALIZED, SOURCE, CONFIDENCE, EVIDENCE_JSON,
                 STATUS, AMBIGUOUS)
            VALUES (:candidate_id, :item_id, :document_id, :field_kind, :target_key,
                    :value_raw, :value_normalized, :source, :confidence, :evidence,
                    :status, :ambiguous)
            """,
            {
                "candidate_id": uuid4().hex,
                "item_id": item_id,
                "document_id": document_id,
                "field_kind": candidate.field_kind,
                "target_key": candidate.target_key,
                "value_raw": candidate.value_raw,
                "value_normalized": candidate.value_normalized,
                "source": candidate.source,
                "confidence": candidate.confidence,
                "evidence": json.dumps(candidate.evidence, ensure_ascii=False),
                "status": status,
                "ambiguous": int(candidate.ambiguous),
            },
        )

    def set_ingest_llm_result(
        self,
        item_id: str,
        *,
        candidates: Sequence[RuleCandidate],
        raw_result: dict[str, Any],
        error_summary: str | None = None,
    ) -> None:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT document_id FROM sds_ingest_items WHERE item_id=:item_id FOR UPDATE",
                {"item_id": item_id},
            )
            row = cursor.fetchone()
            if not row:
                raise LookupError("取込項目が見つかりません")
            document_id = str(row[0])
            cursor.execute(
                "DELETE FROM sds_class_candidates WHERE item_id=:item_id AND source='LLM'",
                {"item_id": item_id},
            )
            for candidate in candidates:
                self._insert_candidate(cursor, item_id, document_id, candidate)
            cursor.execute(
                """
                UPDATE sds_ingest_items
                SET llm_result_json=:result, error_summary=:error,
                    state='REVIEW_REQUIRED', row_version=row_version+1,
                    updated_at=SYSTIMESTAMP
                WHERE item_id=:item_id
                """,
                {
                    "result": json.dumps(raw_result, ensure_ascii=False),
                    "error": error_summary[:2000] if error_summary else None,
                    "item_id": item_id,
                },
            )
            connection.commit()

    def _candidate_rows(self, cursor: Any, item_id: str) -> list[RuleCandidate]:
        cursor.execute(
            """
            SELECT field_kind, target_key, value_raw, value_normalized, source,
                   confidence, evidence_json, status, ambiguous
            FROM sds_class_candidates WHERE item_id=:item_id
            ORDER BY created_at, candidate_id
            """,
            {"item_id": item_id},
        )
        return [
            RuleCandidate(
                field_kind=str(row[0]),
                target_key=str(row[1]) if row[1] else None,
                value_raw=str(row[2]),
                value_normalized=str(row[3]) if row[3] else None,
                source=str(row[4]),
                confidence=float(row[5]) if row[5] is not None else None,
                evidence=_json_value(row[6], {}),
                confirmed=str(row[7]) == "CONFIRMED",
                ambiguous=bool(row[8]),
            )
            for row in cursor.fetchall()
        ]

    def get_ingest_item(self, item_id: str) -> IngestItemReview:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT item_id, batch_id, document_id, original_filename, object_name,
                       media_type, file_size, state, folder_id, rule_result_json,
                       llm_result_json, review_json, error_summary, row_version
                FROM sds_ingest_items WHERE item_id=:item_id
                """,
                {"item_id": item_id},
            )
            row = cursor.fetchone()
            if not row:
                raise LookupError("取込項目が見つかりません")
            candidates = self._candidate_rows(cursor, item_id)
        review = _json_value(row[11], {})
        metadata = None
        try:
            metadata = self.get_document_metadata(str(row[2]))
        except LookupError:
            pass
        if review and metadata:
            metadata = metadata.model_copy(update=review)
        return IngestItemReview(
            item_id=str(row[0]), batch_id=str(row[1]), document_id=str(row[2]),
            original_filename=str(row[3]), object_name=str(row[4]),
            media_type=str(row[5]) if row[5] else None, file_size=int(row[6]),
            state=str(row[7]), folder_id=str(row[8]),
            rule_result=_json_value(row[9], None), llm_result=_json_value(row[10], None),
            review=review,
            candidates=candidates, metadata=metadata,
            error_summary=str(row[12]) if row[12] else None, row_version=int(row[13]),
        )

    def list_ingest_batch(self, batch_id: str) -> tuple[IngestBatch, list[IngestItemReview]]:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT batch_id, status, target_folder_id, ruleset_id, pipeline_job_id,
                       total_items, confirmed_items, failed_items, created_at, updated_at
                FROM sds_ingest_batches WHERE batch_id=:batch_id
                """,
                {"batch_id": batch_id},
            )
            row = cursor.fetchone()
            if not row:
                raise LookupError("取込バッチが見つかりません")
            cursor.execute(
                "SELECT item_id FROM sds_ingest_items WHERE batch_id=:batch_id "
                "ORDER BY created_at, item_id",
                {"batch_id": batch_id},
            )
            item_ids = [str(item[0]) for item in cursor.fetchall()]
        batch = IngestBatch(
            batch_id=str(row[0]), status=str(row[1]), target_folder_id=str(row[2]),
            ruleset_id=str(row[3]) if row[3] else None,
            pipeline_job_id=str(row[4]) if row[4] else None,
            total_items=int(row[5]), confirmed_items=int(row[6]), failed_items=int(row[7]),
            created_at=row[8], updated_at=row[9],
        )
        return batch, [self.get_ingest_item(item_id) for item_id in item_ids]

    def save_ingest_review(
        self,
        item_id: str,
        *,
        review: dict[str, Any],
        expected_row_version: int,
    ) -> IngestItemReview:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE sds_ingest_items
                SET review_json=:review, state='CONFIRMED', row_version=row_version+1,
                    updated_at=SYSTIMESTAMP
                WHERE item_id=:item_id AND row_version=:expected
                """,
                {
                    "review": json.dumps(review, ensure_ascii=False),
                    "item_id": item_id,
                    "expected": expected_row_version,
                },
            )
            if cursor.rowcount != 1:
                raise RuntimeError("取込項目が他の操作で更新されました")
            cursor.execute(
                """
                UPDATE sds_ingest_batches b
                SET confirmed_items=(
                    SELECT COUNT(*) FROM sds_ingest_items i
                    WHERE i.batch_id=b.batch_id AND i.state='CONFIRMED'
                ), updated_at=SYSTIMESTAMP
                WHERE batch_id=(SELECT batch_id FROM sds_ingest_items WHERE item_id=:item_id)
                """,
                {"item_id": item_id},
            )
            connection.commit()
        return self.get_ingest_item(item_id)

    def commit_ingest_item(
        self,
        item_id: str,
        *,
        changed_by_hash: str | None,
    ) -> tuple[str, str]:
        item = self.get_ingest_item(item_id)
        if item.state != "CONFIRMED":
            raise ValueError("確認済みの取込項目だけを登録できます")
        review = dict(item.review)
        confirmed_candidates = [
            candidate for candidate in item.candidates
            if candidate.confirmed and not candidate.ambiguous
        ]
        tag_ids = list(review.get("tag_ids") or [])
        if "tag_ids" not in review:
            tag_ids = [
                str(candidate.target_key) for candidate in confirmed_candidates
                if candidate.field_kind == "TAG" and candidate.target_key
            ]
        customer_was_reviewed = "customer_name_raw" in review
        customer = review.get("customer_name_raw")
        customer_source = review.get("customer_source")
        customer_confidence = review.get("customer_confidence")
        customer_candidate = next(
            (
                value for value in item.candidates
                if value.field_kind == "CUSTOMER"
                and normalize_comparable(value.value_raw)
                == normalize_comparable(str(customer or value.value_raw))
            ),
            None,
        )
        if (
            not customer_was_reviewed
            and not customer
            and customer_candidate
            and customer_candidate.confirmed
        ):
            customer = customer_candidate.value_raw
        if customer and customer_candidate:
            customer_source = customer_source or customer_candidate.source
            customer_confidence = (
                customer_confidence
                if customer_confidence is not None
                else customer_candidate.confidence
            )
        date_was_reviewed = "document_year" in review
        year = review.get("document_year")
        month = review.get("document_month")
        precision = review.get("date_precision")
        date_source = review.get("date_source")
        date_candidate = next(
            (value for value in item.candidates if value.field_kind == "DATE"),
            None,
        )
        if not date_was_reviewed and not year:
            candidate = next(
                (value for value in confirmed_candidates if value.field_kind == "DATE"),
                None,
            )
            if candidate:
                try:
                    parts = [int(value) for value in candidate.value_raw.split("-", 1)]
                    year = parts[0]
                    month = parts[1] if len(parts) > 1 else None
                    precision = str(
                        candidate.evidence.get("precision")
                        or ("YEAR_MONTH" if month else "YEAR")
                    )
                    date_source = candidate.source
                except (TypeError, ValueError):
                    pass
        elif date_candidate:
            date_source = date_source or date_candidate.source
            precision = precision or str(
                date_candidate.evidence.get("precision")
                or ("YEAR_MONTH" if month else "YEAR")
            )
        patch = DocumentMetadataPatch(
            folder_id=review.get("folder_id") or item.folder_id,
            document_set_id=review.get("document_set_id"),
            document_year=year,
            document_month=month,
            date_precision=precision or ("YEAR_MONTH" if year and month else "UNKNOWN"),
            date_source=date_source or ("USER" if year else None),
            date_confirmed=bool(year),
            customer_name_raw=customer,
            customer_source=customer_source or ("USER" if customer else None),
            customer_confirmed=bool(customer),
            customer_confidence=customer_confidence,
            tag_ids=tag_ids,
        )
        self.patch_document_metadata(
            item.document_id, patch, changed_by_hash=changed_by_hash
        )
        with self.connection() as connection, connection.cursor() as cursor:
            for tag_id in tag_ids:
                candidate = next(
                    (
                        value for value in item.candidates
                        if value.field_kind == "TAG" and value.target_key == tag_id
                    ),
                    None,
                )
                if candidate:
                    cursor.execute(
                        """
                        UPDATE sds_document_tags
                        SET source=:source, confidence=:confidence,
                            evidence_json=:evidence, confirmed=1, user_locked=1,
                            updated_at=SYSTIMESTAMP
                        WHERE document_id=:document_id AND tag_id=:tag_id
                        """,
                        {
                            "source": candidate.source,
                            "confidence": candidate.confidence,
                            "evidence": json.dumps(
                                candidate.evidence, ensure_ascii=False
                            ),
                            "document_id": item.document_id,
                            "tag_id": tag_id,
                        },
                    )
            cursor.execute(
                "UPDATE sds_documents SET status='UNPROCESSED', updated_at=SYSTIMESTAMP "
                "WHERE document_id=:document_id",
                {"document_id": item.document_id},
            )
            cursor.execute(
                "UPDATE sds_ingest_items SET state='REGISTERED', row_version=row_version+1, "
                "updated_at=SYSTIMESTAMP WHERE item_id=:item_id",
                {"item_id": item_id},
            )
            cursor.execute(
                """
                UPDATE sds_ingest_batches b SET
                    status=CASE WHEN NOT EXISTS (
                        SELECT 1 FROM sds_ingest_items i
                        WHERE i.batch_id=b.batch_id AND i.state<>'REGISTERED'
                    ) THEN 'COMMITTED' ELSE status END,
                    updated_at=SYSTIMESTAMP
                WHERE b.batch_id=:batch_id
                """,
                {"batch_id": item.batch_id},
            )
            connection.commit()
        return item.document_id, item.object_name

    def mark_ingest_index_queued(self, batch_id: str, job_id: str) -> None:
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE sds_ingest_items SET state='INDEX_QUEUED', updated_at=SYSTIMESTAMP "
                "WHERE batch_id=:batch_id AND state='REGISTERED'",
                {"batch_id": batch_id},
            )
            cursor.execute(
                "UPDATE sds_ingest_batches SET status='COMMITTED', pipeline_job_id=:job_id, "
                "updated_at=SYSTIMESTAMP WHERE batch_id=:batch_id",
                {"batch_id": batch_id, "job_id": job_id},
            )
            connection.commit()

    def cancel_ingest_batch(self, batch_id: str) -> list[str]:
        """Cancel an uncommitted batch and remove only its DRAFT DB records."""
        self.require_schema()
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM sds_ingest_batches "
                "WHERE batch_id=:batch_id FOR UPDATE",
                {"batch_id": batch_id},
            )
            row = cursor.fetchone()
            if not row:
                raise LookupError("取込バッチが見つかりません")
            if str(row[0]) in {"COMMITTED", "CONFIRMED"}:
                raise ValueError("登録済みの取込バッチはキャンセルできません")
            cursor.execute(
                """
                SELECT i.document_id, i.object_name, d.status, d.serving_release_id
                FROM sds_ingest_items i
                JOIN sds_documents d ON d.document_id=i.document_id
                WHERE i.batch_id=:batch_id
                """,
                {"batch_id": batch_id},
            )
            documents = cursor.fetchall()
            if any(str(item[2]) != "DRAFT" or item[3] is not None for item in documents):
                raise ValueError("登録処理が開始されたバッチはキャンセルできません")
            object_names = [str(item[1]) for item in documents]
            document_ids = [str(item[0]) for item in documents]
            cursor.execute(
                "DELETE FROM sds_ingest_items WHERE batch_id=:batch_id",
                {"batch_id": batch_id},
            )
            for document_id in document_ids:
                cursor.execute(
                    "DELETE FROM sds_documents "
                    "WHERE document_id=:document_id AND status='DRAFT'",
                    {"document_id": document_id},
                )
            cursor.execute(
                "UPDATE sds_ingest_batches SET status='CANCELLED', "
                "updated_at=SYSTIMESTAMP WHERE batch_id=:batch_id",
                {"batch_id": batch_id},
            )
            connection.commit()
        return object_names


document_metadata_repository = DocumentMetadataRepository()
