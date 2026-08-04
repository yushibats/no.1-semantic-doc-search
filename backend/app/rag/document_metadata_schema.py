from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import Any, Iterator

from app.rag.document_metadata_models import (
    ClassificationRule,
    ClassificationRuleSetConfig,
    ROOT_FOLDER_ID,
    RuleCondition,
    UNCLASSIFIED_FOLDER_ID,
)
from app.rag.profile_prompts import SEARCH_CONCEPT_EXTRACTION_PROMPT

from app.services.database_service import database_service


FEATURE_CODE = "document_library"
FEATURE_VERSION = "20260803_003"
SYSTEM_TENANT_HASH = "0" * 64


TABLE_STATEMENTS: dict[str, str] = {
    "SDS_FEATURE_MIGRATIONS": """
        CREATE TABLE SDS_FEATURE_MIGRATIONS (
            FEATURE_CODE VARCHAR2(64) NOT NULL,
            VERSION_ID VARCHAR2(32) NOT NULL,
            DDL_SHA256 CHAR(64) NOT NULL,
            DETAILS_JSON CLOB CHECK (DETAILS_JSON IS JSON),
            APPLIED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            PRIMARY KEY (FEATURE_CODE, VERSION_ID)
        )
    """,
    "SDS_FOLDERS": """
        CREATE TABLE SDS_FOLDERS (
            FOLDER_ID VARCHAR2(64) PRIMARY KEY,
            TENANT_ID_HASH CHAR(64) NOT NULL,
            PARENT_FOLDER_ID VARCHAR2(64) REFERENCES SDS_FOLDERS(FOLDER_ID),
            NAME VARCHAR2(400) NOT NULL,
            NORMALIZED_NAME VARCHAR2(400) NOT NULL,
            IS_SYSTEM NUMBER(1) DEFAULT 0 NOT NULL CHECK (IS_SYSTEM IN (0, 1)),
            SORT_ORDER NUMBER DEFAULT 0 NOT NULL,
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "SDS_FOLDER_CLOSURE": """
        CREATE TABLE SDS_FOLDER_CLOSURE (
            ANCESTOR_FOLDER_ID VARCHAR2(64) NOT NULL
                REFERENCES SDS_FOLDERS(FOLDER_ID) ON DELETE CASCADE,
            DESCENDANT_FOLDER_ID VARCHAR2(64) NOT NULL
                REFERENCES SDS_FOLDERS(FOLDER_ID) ON DELETE CASCADE,
            DEPTH NUMBER NOT NULL CHECK (DEPTH >= 0),
            PRIMARY KEY (ANCESTOR_FOLDER_ID, DESCENDANT_FOLDER_ID)
        )
    """,
    "SDS_DOCUMENT_SETS": """
        CREATE TABLE SDS_DOCUMENT_SETS (
            DOCUMENT_SET_ID VARCHAR2(64) PRIMARY KEY,
            LABEL VARCHAR2(400) NOT NULL,
            NORMALIZED_LABEL VARCHAR2(400) NOT NULL,
            DESCRIPTION VARCHAR2(2000),
            STATUS VARCHAR2(16) DEFAULT 'ACTIVE' NOT NULL
                CHECK (STATUS IN ('ACTIVE', 'ARCHIVED')),
            CREATED_BY_HASH CHAR(64),
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "SDS_DOCUMENT_METADATA": f"""
        CREATE TABLE SDS_DOCUMENT_METADATA (
            DOCUMENT_ID VARCHAR2(64) PRIMARY KEY
                REFERENCES SDS_DOCUMENTS(DOCUMENT_ID) ON DELETE CASCADE,
            FOLDER_ID VARCHAR2(64) DEFAULT '{UNCLASSIFIED_FOLDER_ID}' NOT NULL
                REFERENCES SDS_FOLDERS(FOLDER_ID),
            DOCUMENT_SET_ID VARCHAR2(64) REFERENCES SDS_DOCUMENT_SETS(DOCUMENT_SET_ID),
            DOCUMENT_YEAR NUMBER(4) CHECK (DOCUMENT_YEAR BETWEEN 1000 AND 9999),
            DOCUMENT_MONTH NUMBER(2) CHECK (DOCUMENT_MONTH BETWEEN 1 AND 12),
            DATE_PRECISION VARCHAR2(16) DEFAULT 'UNKNOWN' NOT NULL
                CHECK (DATE_PRECISION IN ('UNKNOWN', 'YEAR', 'YEAR_MONTH', 'DAY')),
            DATE_SOURCE VARCHAR2(24)
                CHECK (DATE_SOURCE IN ('AUTO_FILENAME', 'RULE', 'LLM', 'USER',
                                       'FOLDER_DEFAULT', 'MIGRATION')),
            DATE_CONFIRMED NUMBER(1) DEFAULT 0 NOT NULL CHECK (DATE_CONFIRMED IN (0, 1)),
            CUSTOMER_NAME_RAW VARCHAR2(400),
            CUSTOMER_NAME_NORMALIZED VARCHAR2(400),
            CUSTOMER_NAME_SEARCH_KEY VARCHAR2(400),
            CUSTOMER_SOURCE VARCHAR2(24)
                CHECK (CUSTOMER_SOURCE IN ('AUTO_FILENAME', 'RULE', 'LLM', 'USER',
                                           'FOLDER_DEFAULT', 'MIGRATION')),
            CUSTOMER_CONFIRMED NUMBER(1) DEFAULT 0 NOT NULL
                CHECK (CUSTOMER_CONFIRMED IN (0, 1)),
            CUSTOMER_CONFIDENCE NUMBER CHECK (CUSTOMER_CONFIDENCE BETWEEN 0 AND 1),
            CUSTOMER_NORMALIZATION_VERSION NUMBER DEFAULT 1 NOT NULL,
            ROW_VERSION NUMBER DEFAULT 1 NOT NULL,
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            CHECK (DOCUMENT_MONTH IS NULL OR DOCUMENT_YEAR IS NOT NULL)
        )
    """,
    "SDS_TAG_GROUPS": """
        CREATE TABLE SDS_TAG_GROUPS (
            GROUP_ID VARCHAR2(64) PRIMARY KEY,
            CODE VARCHAR2(64) NOT NULL UNIQUE,
            NAME VARCHAR2(200) NOT NULL,
            SELECTION_MODE VARCHAR2(8) DEFAULT 'MULTI' NOT NULL
                CHECK (SELECTION_MODE IN ('SINGLE', 'MULTI')),
            ACTIVE NUMBER(1) DEFAULT 1 NOT NULL CHECK (ACTIVE IN (0, 1)),
            SORT_ORDER NUMBER DEFAULT 0 NOT NULL,
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "SDS_TAGS": """
        CREATE TABLE SDS_TAGS (
            TAG_ID VARCHAR2(64) PRIMARY KEY,
            GROUP_ID VARCHAR2(64) NOT NULL REFERENCES SDS_TAG_GROUPS(GROUP_ID),
            CODE VARCHAR2(64) NOT NULL UNIQUE,
            NAME VARCHAR2(200) NOT NULL,
            ACTIVE NUMBER(1) DEFAULT 1 NOT NULL CHECK (ACTIVE IN (0, 1)),
            SORT_ORDER NUMBER DEFAULT 0 NOT NULL,
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "SDS_DOCUMENT_TAGS": """
        CREATE TABLE SDS_DOCUMENT_TAGS (
            DOCUMENT_ID VARCHAR2(64) NOT NULL
                REFERENCES SDS_DOCUMENTS(DOCUMENT_ID) ON DELETE CASCADE,
            TAG_ID VARCHAR2(64) NOT NULL REFERENCES SDS_TAGS(TAG_ID),
            SOURCE VARCHAR2(24) NOT NULL
                CHECK (SOURCE IN ('AUTO_FILENAME', 'RULE', 'LLM', 'USER',
                                  'FOLDER_DEFAULT', 'MIGRATION')),
            CONFIDENCE NUMBER CHECK (CONFIDENCE BETWEEN 0 AND 1),
            EVIDENCE_JSON CLOB CHECK (EVIDENCE_JSON IS JSON),
            CONFIRMED NUMBER(1) DEFAULT 0 NOT NULL CHECK (CONFIRMED IN (0, 1)),
            USER_LOCKED NUMBER(1) DEFAULT 0 NOT NULL CHECK (USER_LOCKED IN (0, 1)),
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            PRIMARY KEY (DOCUMENT_ID, TAG_ID)
        )
    """,
    "SDS_CLASS_RULESETS": """
        CREATE TABLE SDS_CLASS_RULESETS (
            RULESET_ID VARCHAR2(64) PRIMARY KEY,
            CODE VARCHAR2(64) NOT NULL UNIQUE,
            NAME VARCHAR2(200) NOT NULL,
            ENABLED NUMBER(1) DEFAULT 1 NOT NULL CHECK (ENABLED IN (0, 1)),
            CURRENT_REVISION_ID VARCHAR2(64),
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "SDS_CLASS_RULESET_REVS": """
        CREATE TABLE SDS_CLASS_RULESET_REVS (
            REVISION_ID VARCHAR2(64) PRIMARY KEY,
            RULESET_ID VARCHAR2(64) NOT NULL
                REFERENCES SDS_CLASS_RULESETS(RULESET_ID) ON DELETE CASCADE,
            REVISION_NO NUMBER NOT NULL,
            CONFIG_HASH CHAR(64) NOT NULL,
            CONFIG_JSON CLOB NOT NULL CHECK (CONFIG_JSON IS JSON),
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            UNIQUE (RULESET_ID, REVISION_NO)
        )
    """,
    "SDS_FOLDER_RULESETS": """
        CREATE TABLE SDS_FOLDER_RULESETS (
            FOLDER_ID VARCHAR2(64) PRIMARY KEY
                REFERENCES SDS_FOLDERS(FOLDER_ID) ON DELETE CASCADE,
            RULESET_ID VARCHAR2(64) NOT NULL REFERENCES SDS_CLASS_RULESETS(RULESET_ID),
            INHERIT_TO_DESCENDANTS NUMBER(1) DEFAULT 1 NOT NULL
                CHECK (INHERIT_TO_DESCENDANTS IN (0, 1)),
            DEFAULTS_JSON CLOB CHECK (DEFAULTS_JSON IS JSON),
            UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "SDS_INGEST_BATCHES": """
        CREATE TABLE SDS_INGEST_BATCHES (
            BATCH_ID VARCHAR2(64) PRIMARY KEY,
            STATUS VARCHAR2(24) DEFAULT 'DRAFT' NOT NULL
                CHECK (STATUS IN ('DRAFT', 'PROCESSING', 'REVIEW_REQUIRED', 'CONFIRMED',
                                  'COMMITTED', 'PARTIAL_FAILED', 'FAILED', 'CANCELLED')),
            TARGET_FOLDER_ID VARCHAR2(64) NOT NULL REFERENCES SDS_FOLDERS(FOLDER_ID),
            RULESET_ID VARCHAR2(64) REFERENCES SDS_CLASS_RULESETS(RULESET_ID),
            PIPELINE_JOB_ID VARCHAR2(64) REFERENCES SDS_PIPELINE_JOBS(JOB_ID),
            CREATED_BY_HASH CHAR(64),
            TOTAL_ITEMS NUMBER DEFAULT 0 NOT NULL,
            CONFIRMED_ITEMS NUMBER DEFAULT 0 NOT NULL,
            FAILED_ITEMS NUMBER DEFAULT 0 NOT NULL,
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "SDS_INGEST_ITEMS": """
        CREATE TABLE SDS_INGEST_ITEMS (
            ITEM_ID VARCHAR2(64) PRIMARY KEY,
            BATCH_ID VARCHAR2(64) NOT NULL
                REFERENCES SDS_INGEST_BATCHES(BATCH_ID) ON DELETE CASCADE,
            DOCUMENT_ID VARCHAR2(64) NOT NULL REFERENCES SDS_DOCUMENTS(DOCUMENT_ID),
            ORIGINAL_FILENAME VARCHAR2(1024) NOT NULL,
            OBJECT_NAME VARCHAR2(1024) NOT NULL,
            MEDIA_TYPE VARCHAR2(128),
            FILE_SIZE NUMBER NOT NULL,
            CONTENT_SHA256 CHAR(64) NOT NULL,
            STATE VARCHAR2(32) DEFAULT 'TEMP_STORED' NOT NULL,
            FOLDER_ID VARCHAR2(64) NOT NULL REFERENCES SDS_FOLDERS(FOLDER_ID),
            RULE_RESULT_JSON CLOB CHECK (RULE_RESULT_JSON IS JSON),
            LLM_RESULT_JSON CLOB CHECK (LLM_RESULT_JSON IS JSON),
            REVIEW_JSON CLOB CHECK (REVIEW_JSON IS JSON),
            ERROR_SUMMARY VARCHAR2(2000),
            ROW_VERSION NUMBER DEFAULT 1 NOT NULL,
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            UNIQUE (BATCH_ID, DOCUMENT_ID)
        )
    """,
    "SDS_CLASS_CANDIDATES": """
        CREATE TABLE SDS_CLASS_CANDIDATES (
            CANDIDATE_ID VARCHAR2(64) PRIMARY KEY,
            ITEM_ID VARCHAR2(64) REFERENCES SDS_INGEST_ITEMS(ITEM_ID) ON DELETE CASCADE,
            DOCUMENT_ID VARCHAR2(64) REFERENCES SDS_DOCUMENTS(DOCUMENT_ID) ON DELETE CASCADE,
            FIELD_KIND VARCHAR2(16) NOT NULL CHECK (FIELD_KIND IN ('TAG', 'CUSTOMER', 'DATE')),
            TARGET_KEY VARCHAR2(128),
            VALUE_RAW VARCHAR2(2000) NOT NULL,
            VALUE_NORMALIZED VARCHAR2(2000),
            SOURCE VARCHAR2(24) NOT NULL,
            CONFIDENCE NUMBER CHECK (CONFIDENCE BETWEEN 0 AND 1),
            EVIDENCE_JSON CLOB CHECK (EVIDENCE_JSON IS JSON),
            STATUS VARCHAR2(16) DEFAULT 'PENDING' NOT NULL
                CHECK (STATUS IN ('PENDING', 'CONFIRMED', 'REJECTED')),
            AMBIGUOUS NUMBER(1) DEFAULT 0 NOT NULL CHECK (AMBIGUOUS IN (0, 1)),
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            CHECK (ITEM_ID IS NOT NULL OR DOCUMENT_ID IS NOT NULL)
        )
    """,
    "SDS_DOC_METADATA_AUDIT": """
        CREATE TABLE SDS_DOC_METADATA_AUDIT (
            AUDIT_ID NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            DOCUMENT_ID VARCHAR2(64) NOT NULL
                REFERENCES SDS_DOCUMENTS(DOCUMENT_ID) ON DELETE CASCADE,
            CHANGED_BY_HASH CHAR(64),
            CHANGE_SOURCE VARCHAR2(24) NOT NULL,
            BEFORE_JSON CLOB CHECK (BEFORE_JSON IS JSON),
            AFTER_JSON CLOB CHECK (AFTER_JSON IS JSON),
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "SDS_SEARCH_CONCEPT_SETTINGS": """
        CREATE TABLE SDS_SEARCH_CONCEPT_SETTINGS (
            SETTINGS_ID VARCHAR2(64) PRIMARY KEY,
            ENABLED NUMBER(1) DEFAULT 1 NOT NULL CHECK (ENABLED IN (0, 1)),
            AUTO_PUBLISH NUMBER(1) DEFAULT 1 NOT NULL CHECK (AUTO_PUBLISH IN (0, 1)),
            AUTO_PUBLISH_CONFIDENCE NUMBER DEFAULT 0.85 NOT NULL
                CHECK (AUTO_PUBLISH_CONFIDENCE BETWEEN 0 AND 1),
            MIN_SUPPORT_SETS NUMBER DEFAULT 2 NOT NULL CHECK (MIN_SUPPORT_SETS >= 1),
            MAX_CONCEPTS_PER_DOCUMENT NUMBER DEFAULT 16 NOT NULL
                CHECK (MAX_CONCEPTS_PER_DOCUMENT BETWEEN 1 AND 100),
            INITIAL_DISPLAY_LIMIT NUMBER DEFAULT 8 NOT NULL
                CHECK (INITIAL_DISPLAY_LIMIT BETWEEN 1 AND 100),
            INPUT_TEXT_LIMIT NUMBER DEFAULT 24000 NOT NULL
                CHECK (INPUT_TEXT_LIMIT BETWEEN 1000 AND 100000),
            PROMPT_TEXT CLOB NOT NULL,
            TAXONOMY_REVISION NUMBER DEFAULT 1 NOT NULL,
            UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "SDS_SEARCH_CONCEPTS": """
        CREATE TABLE SDS_SEARCH_CONCEPTS (
            CONCEPT_ID VARCHAR2(64) PRIMARY KEY,
            FACET VARCHAR2(16) NOT NULL CHECK (FACET IN ('BEFORE', 'AFTER', 'OTHER')),
            CATEGORY_CODE VARCHAR2(64) NOT NULL,
            CATEGORY_NAME VARCHAR2(200) NOT NULL,
            DISPLAY_LABEL VARCHAR2(400) NOT NULL,
            NORMALIZED_LABEL VARCHAR2(400) NOT NULL,
            STATUS VARCHAR2(16) DEFAULT 'PENDING' NOT NULL
                CHECK (STATUS IN ('PENDING', 'ACTIVE', 'HIDDEN', 'MERGED')),
            MERGED_INTO_ID VARCHAR2(64) REFERENCES SDS_SEARCH_CONCEPTS(CONCEPT_ID),
            SUPPORT_DOCUMENT_COUNT NUMBER DEFAULT 0 NOT NULL,
            SUPPORT_SET_COUNT NUMBER DEFAULT 0 NOT NULL,
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "SDS_SEARCH_CONCEPT_ALIASES": """
        CREATE TABLE SDS_SEARCH_CONCEPT_ALIASES (
            ALIAS_ID VARCHAR2(64) PRIMARY KEY,
            CONCEPT_ID VARCHAR2(64) NOT NULL
                REFERENCES SDS_SEARCH_CONCEPTS(CONCEPT_ID) ON DELETE CASCADE,
            ALIAS_LABEL VARCHAR2(400) NOT NULL,
            NORMALIZED_ALIAS VARCHAR2(400) NOT NULL,
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "SDS_DOCUMENT_CONCEPTS": """
        CREATE TABLE SDS_DOCUMENT_CONCEPTS (
            DOCUMENT_ID VARCHAR2(64) NOT NULL
                REFERENCES SDS_DOCUMENTS(DOCUMENT_ID) ON DELETE CASCADE,
            REVISION_ID VARCHAR2(64) NOT NULL
                REFERENCES SDS_DOCUMENT_REVISIONS(REVISION_ID) ON DELETE CASCADE,
            CONCEPT_ID VARCHAR2(64) NOT NULL
                REFERENCES SDS_SEARCH_CONCEPTS(CONCEPT_ID),
            STAGE_RUN_ID VARCHAR2(64) REFERENCES SDS_STAGE_RUNS(STAGE_RUN_ID),
            CONFIDENCE NUMBER NOT NULL CHECK (CONFIDENCE BETWEEN 0 AND 1),
            EVIDENCE_JSON CLOB CHECK (EVIDENCE_JSON IS JSON),
            SOURCE_KINDS_JSON CLOB CHECK (SOURCE_KINDS_JSON IS JSON),
            USER_LOCKED NUMBER(1) DEFAULT 0 NOT NULL CHECK (USER_LOCKED IN (0, 1)),
            CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            PRIMARY KEY (REVISION_ID, CONCEPT_ID)
        )
    """,
}

INDEX_STATEMENTS: dict[str, str] = {
    "SDS_FOLDER_PARENT_UQ": """
        CREATE UNIQUE INDEX SDS_FOLDER_PARENT_UQ ON SDS_FOLDERS (
            TENANT_ID_HASH,
            NVL(PARENT_FOLDER_ID, '#ROOT#'),
            NORMALIZED_NAME
        )
    """,
    "SDS_FOLDER_DESC_IDX": """
        CREATE INDEX SDS_FOLDER_DESC_IDX ON SDS_FOLDER_CLOSURE (
            DESCENDANT_FOLDER_ID, ANCESTOR_FOLDER_ID, DEPTH
        )
    """,
    "SDS_DOCMETA_FOLDER_IDX": """
        CREATE INDEX SDS_DOCMETA_FOLDER_IDX ON SDS_DOCUMENT_METADATA (
            FOLDER_ID, DOCUMENT_ID
        )
    """,
    "SDS_DOCMETA_DATE_IDX": """
        CREATE INDEX SDS_DOCMETA_DATE_IDX ON SDS_DOCUMENT_METADATA (
            DOCUMENT_YEAR, DOCUMENT_MONTH, DOCUMENT_ID
        )
    """,
    "SDS_DOCMETA_CUSTOMER_IDX": """
        CREATE INDEX SDS_DOCMETA_CUSTOMER_IDX ON SDS_DOCUMENT_METADATA (
            CUSTOMER_NAME_NORMALIZED, DOCUMENT_ID
        )
    """,
    "SDS_DOCMETA_SET_IDX": """
        CREATE INDEX SDS_DOCMETA_SET_IDX ON SDS_DOCUMENT_METADATA (
            DOCUMENT_SET_ID, DOCUMENT_ID
        )
    """,
    "SDS_DOCSET_LABEL_IDX": """
        CREATE INDEX SDS_DOCSET_LABEL_IDX ON SDS_DOCUMENT_SETS (
            NORMALIZED_LABEL, STATUS
        )
    """,
    "SDS_DOCTAG_TAG_IDX": """
        CREATE INDEX SDS_DOCTAG_TAG_IDX ON SDS_DOCUMENT_TAGS (
            TAG_ID, DOCUMENT_ID, CONFIRMED
        )
    """,
    "SDS_CANDIDATE_ITEM_IDX": """
        CREATE INDEX SDS_CANDIDATE_ITEM_IDX ON SDS_CLASS_CANDIDATES (
            ITEM_ID, STATUS, FIELD_KIND
        )
    """,
    "SDS_CONCEPT_LOOKUP_IDX": """
        CREATE INDEX SDS_CONCEPT_LOOKUP_IDX ON SDS_SEARCH_CONCEPTS (
            STATUS, FACET, CATEGORY_CODE, NORMALIZED_LABEL
        )
    """,
    "SDS_CONCEPT_ALIAS_UQ": """
        CREATE UNIQUE INDEX SDS_CONCEPT_ALIAS_UQ ON SDS_SEARCH_CONCEPT_ALIASES (
            NORMALIZED_ALIAS, CONCEPT_ID
        )
    """,
    "SDS_DOCCONCEPT_DOC_IDX": """
        CREATE INDEX SDS_DOCCONCEPT_DOC_IDX ON SDS_DOCUMENT_CONCEPTS (
            DOCUMENT_ID, CONCEPT_ID, REVISION_ID
        )
    """,
    "SDS_DOCCONCEPT_CONCEPT_IDX": """
        CREATE INDEX SDS_DOCCONCEPT_CONCEPT_IDX ON SDS_DOCUMENT_CONCEPTS (
            CONCEPT_ID, DOCUMENT_ID, REVISION_ID
        )
    """,
}

ALTER_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "SDS_DOCUMENTS",
        "STORAGE_KEY_VERSION",
        "ALTER TABLE SDS_DOCUMENTS ADD STORAGE_KEY_VERSION NUMBER DEFAULT 1 NOT NULL",
    ),
    (
        "SDS_DOCUMENT_REVISIONS",
        "SOURCE_OBJECT_NAME",
        "ALTER TABLE SDS_DOCUMENT_REVISIONS ADD SOURCE_OBJECT_NAME VARCHAR2(1024)",
    ),
    (
        "SDS_DOCUMENT_METADATA",
        "DOCUMENT_SET_ID",
        "ALTER TABLE SDS_DOCUMENT_METADATA ADD DOCUMENT_SET_ID VARCHAR2(64) "
        "REFERENCES SDS_DOCUMENT_SETS(DOCUMENT_SET_ID)",
    ),
)


DEFAULT_CONCEPT_PROMPT = SEARCH_CONCEPT_EXTRACTION_PROMPT


def _default_ruleset() -> ClassificationRuleSetConfig:
    return ClassificationRuleSetConfig(
        rules=[
            ClassificationRule(
                rule_id="document_floor_plan",
                name="間取り図",
                priority=100,
                condition=RuleCondition(any_terms=["平面図", "現況図", "計画図"]),
                tag_ids=["tag_document_floor_plan"],
            ),
            ClassificationRule(
                rule_id="document_proposal_material",
                name="提案資料",
                priority=110,
                condition=RuleCondition(
                    any_terms=["提案書", "プレゼン", "プレゼン資料"],
                    extensions=["pdf", "ppt", "pptx"],
                ),
                tag_ids=["tag_document_proposal_material"],
            ),
            ClassificationRule(
                rule_id="state_existing",
                name="現況",
                priority=100,
                condition=RuleCondition(any_terms=["現況", "現状"]),
                tag_ids=["tag_state_existing"],
            ),
            ClassificationRule(
                rule_id="state_plan",
                name="提案・計画",
                priority=100,
                condition=RuleCondition(any_terms=["plan", "計画", "提案"]),
                tag_ids=["tag_state_plan"],
            ),
            ClassificationRule(
                rule_id="floor_1f",
                name="1F",
                priority=100,
                condition=RuleCondition(any_terms=["1f", "1階"]),
                tag_ids=["tag_floor_1f"],
            ),
            ClassificationRule(
                rule_id="floor_2f",
                name="2F",
                priority=100,
                condition=RuleCondition(any_terms=["2f", "2階"]),
                tag_ids=["tag_floor_2f"],
            ),
            ClassificationRule(
                rule_id="perspective_image",
                name="パース画像",
                priority=100,
                condition=RuleCondition(
                    any_terms=["パース"],
                    extensions=["jpg", "jpeg", "png"],
                ),
                tag_ids=["tag_document_perspective"],
            ),
            ClassificationRule(
                rule_id="customer_japanese_house",
                name="顧客名候補（様邸・邸）",
                priority=10,
                condition=RuleCondition(any_terms=["様邸", "邸"]),
                customer_pattern=(
                    r"(?P<customer>[^_\-\s.]{1,80}?(?:様邸|邸))"
                ),
            ),
        ],
    )


TAG_GROUP_SEEDS = (
    ("tag_group_document", "document_kind", "文書種別", "SINGLE", 10),
    ("tag_group_state", "state", "現況／提案・計画", "SINGLE", 20),
    ("tag_group_floor", "floor", "階数", "SINGLE", 30),
)
TAG_SEEDS = (
    ("tag_document_floor_plan", "tag_group_document", "floor_plan", "間取り図", 10),
    ("tag_document_perspective", "tag_group_document", "perspective", "パース画像", 20),
    ("tag_document_photo", "tag_group_document", "photo", "写真", 30),
    (
        "tag_document_proposal_material",
        "tag_group_document",
        "proposal_material",
        "提案資料",
        40,
    ),
    ("tag_state_existing", "tag_group_state", "existing", "現況", 10),
    ("tag_state_plan", "tag_group_state", "plan", "提案・計画", 20),
    ("tag_floor_1f", "tag_group_floor", "floor_1f", "1F", 10),
    ("tag_floor_2f", "tag_group_floor", "floor_2f", "2F", 20),
)


@contextmanager
def _connection() -> Iterator[Any]:
    if not database_service._ensure_pool_initialized():
        raise RuntimeError("database connection is not configured")
    with database_service.pool_manager.acquire_connection() as connection:
        yield connection


def _names(cursor: Any, query: str, binds: dict[str, Any] | None = None) -> set[str]:
    cursor.execute(query, binds or {})
    return {str(row[0]).upper() for row in cursor.fetchall()}


def _ddl_digest() -> str:
    statements = [*TABLE_STATEMENTS.values(), *INDEX_STATEMENTS.values()]
    statements.extend(item[2] for item in ALTER_COLUMNS)
    return hashlib.sha256("\n\n".join(value.strip() for value in statements).encode()).hexdigest()


def document_library_schema_status() -> dict[str, Any]:
    with _connection() as connection, connection.cursor() as cursor:
        existing_tables = _names(cursor, "SELECT TABLE_NAME FROM USER_TABLES")
        existing_indexes = _names(cursor, "SELECT INDEX_NAME FROM USER_INDEXES")
        missing_tables = [name for name in TABLE_STATEMENTS if name not in existing_tables]
        missing_indexes = [name for name in INDEX_STATEMENTS if name not in existing_indexes]
        missing_columns: list[str] = []
        for table, column, _ in ALTER_COLUMNS:
            cursor.execute(
                "SELECT COUNT(*) FROM USER_TAB_COLUMNS WHERE TABLE_NAME=:table_name "
                "AND COLUMN_NAME=:column_name",
                {"table_name": table, "column_name": column},
            )
            if not int(cursor.fetchone()[0]):
                missing_columns.append(f"{table}.{column}")
        version_current = False
        if "SDS_FEATURE_MIGRATIONS" in existing_tables:
            cursor.execute(
                "SELECT COUNT(*) FROM SDS_FEATURE_MIGRATIONS "
                "WHERE FEATURE_CODE=:feature AND VERSION_ID=:version AND DDL_SHA256=:digest",
                {
                    "feature": FEATURE_CODE,
                    "version": FEATURE_VERSION,
                    "digest": _ddl_digest(),
                },
            )
            version_current = bool(cursor.fetchone()[0])
    return {
        "ready": not missing_tables and not missing_indexes and not missing_columns and version_current,
        "feature": FEATURE_CODE,
        "version": FEATURE_VERSION,
        "version_current": version_current,
        "missing_tables": missing_tables,
        "missing_indexes": missing_indexes,
        "missing_columns": missing_columns,
    }


def apply_document_library_schema() -> dict[str, Any]:
    created_tables: list[str] = []
    created_indexes: list[str] = []
    added_columns: list[str] = []
    with _connection() as connection, connection.cursor() as cursor:
        existing_tables = _names(cursor, "SELECT TABLE_NAME FROM USER_TABLES")
        if "SDS_DOCUMENTS" not in existing_tables:
            raise RuntimeError("SDS base schema is not initialized")
        for table_name, statement in TABLE_STATEMENTS.items():
            if table_name in existing_tables:
                continue
            cursor.execute(statement)
            existing_tables.add(table_name)
            created_tables.append(table_name)
        for table, column, statement in ALTER_COLUMNS:
            cursor.execute(
                "SELECT COUNT(*) FROM USER_TAB_COLUMNS WHERE TABLE_NAME=:table_name "
                "AND COLUMN_NAME=:column_name",
                {"table_name": table, "column_name": column},
            )
            if int(cursor.fetchone()[0]):
                continue
            cursor.execute(statement)
            added_columns.append(f"{table}.{column}")
        cursor.execute(
            """
            UPDATE SDS_DOCUMENT_REVISIONS r
            SET r.SOURCE_OBJECT_NAME=(
                SELECT d.OBJECT_NAME FROM SDS_DOCUMENTS d WHERE d.DOCUMENT_ID=r.DOCUMENT_ID
            )
            WHERE r.SOURCE_OBJECT_NAME IS NULL
            """
        )
        existing_indexes = _names(cursor, "SELECT INDEX_NAME FROM USER_INDEXES")
        for index_name, statement in INDEX_STATEMENTS.items():
            if index_name in existing_indexes:
                continue
            cursor.execute(statement)
            existing_indexes.add(index_name)
            created_indexes.append(index_name)

        for folder_id, parent_id, name, is_system, sort_order in (
            (ROOT_FOLDER_ID, None, "ルート", 1, 0),
            (UNCLASSIFIED_FOLDER_ID, ROOT_FOLDER_ID, "未分類", 1, 0),
        ):
            cursor.execute(
                """
                MERGE INTO SDS_FOLDERS f
                USING (SELECT :folder_id folder_id FROM dual) s
                ON (f.folder_id=s.folder_id)
                WHEN NOT MATCHED THEN INSERT
                    (folder_id, tenant_id_hash, parent_folder_id, name, normalized_name,
                     is_system, sort_order)
                VALUES (:folder_id, :tenant, :parent_id, :name, :normalized_name,
                        :is_system, :sort_order)
                """,
                {
                    "folder_id": folder_id,
                    "tenant": SYSTEM_TENANT_HASH,
                    "parent_id": parent_id,
                    "name": name,
                    "normalized_name": name.casefold(),
                    "is_system": is_system,
                    "sort_order": sort_order,
                },
            )
        for ancestor, descendant, depth in (
            (ROOT_FOLDER_ID, ROOT_FOLDER_ID, 0),
            (ROOT_FOLDER_ID, UNCLASSIFIED_FOLDER_ID, 1),
            (UNCLASSIFIED_FOLDER_ID, UNCLASSIFIED_FOLDER_ID, 0),
        ):
            cursor.execute(
                """
                MERGE INTO SDS_FOLDER_CLOSURE c
                USING (SELECT :ancestor ancestor_folder_id,
                              :descendant descendant_folder_id FROM dual) s
                ON (c.ancestor_folder_id=s.ancestor_folder_id
                    AND c.descendant_folder_id=s.descendant_folder_id)
                WHEN NOT MATCHED THEN INSERT
                    (ancestor_folder_id, descendant_folder_id, depth)
                VALUES (:ancestor, :descendant, :depth)
                """,
                {"ancestor": ancestor, "descendant": descendant, "depth": depth},
            )
        cursor.execute(
            f"""
            MERGE INTO SDS_DOCUMENT_METADATA m
            USING (SELECT d.DOCUMENT_ID FROM SDS_DOCUMENTS d) s
            ON (m.DOCUMENT_ID=s.DOCUMENT_ID)
            WHEN NOT MATCHED THEN INSERT
                (DOCUMENT_ID, FOLDER_ID, DATE_PRECISION, DATE_SOURCE, DATE_CONFIRMED,
                 CUSTOMER_CONFIRMED, ROW_VERSION)
            VALUES (s.DOCUMENT_ID, '{UNCLASSIFIED_FOLDER_ID}', 'UNKNOWN', 'MIGRATION', 0, 0, 1)
            """
        )

        for group_id, code, name, mode, sort_order in TAG_GROUP_SEEDS:
            cursor.execute(
                """
                MERGE INTO SDS_TAG_GROUPS g
                USING (SELECT :group_id group_id FROM dual) s
                ON (g.group_id=s.group_id)
                WHEN NOT MATCHED THEN INSERT
                    (group_id, code, name, selection_mode, sort_order)
                VALUES (:group_id, :code, :name, :selection_mode_bind, :sort_order)
                """,
                {
                    "group_id": group_id,
                    "code": code,
                    "name": name,
                    "selection_mode_bind": mode,
                    "sort_order": sort_order,
                },
            )
        for tag_id, group_id, code, name, sort_order in TAG_SEEDS:
            cursor.execute(
                """
                MERGE INTO SDS_TAGS t
                USING (SELECT :tag_id tag_id, :code code FROM dual) s
                ON (t.tag_id=s.tag_id OR t.code=s.code)
                WHEN NOT MATCHED THEN INSERT
                    (tag_id, group_id, code, name, sort_order)
                VALUES (:tag_id, :group_id, :code, :name, :sort_order)
                """,
                {
                    "tag_id": tag_id,
                    "group_id": group_id,
                    "code": code,
                    "name": name,
                    "sort_order": sort_order,
                },
            )

        ruleset_id = "ruleset_default"
        config = _default_ruleset()
        config_json = config.model_dump_json()
        config_hash = hashlib.sha256(config_json.encode()).hexdigest()
        revision_id = f"{ruleset_id}_{FEATURE_VERSION}"
        cursor.execute(
            """
            MERGE INTO SDS_CLASS_RULESETS r
            USING (SELECT :ruleset_id ruleset_id FROM dual) s
            ON (r.ruleset_id=s.ruleset_id)
            WHEN NOT MATCHED THEN INSERT
                (ruleset_id, code, name, enabled)
            VALUES (:ruleset_id, 'default', '既定の分類ルール', 1)
            """,
            {"ruleset_id": ruleset_id},
        )
        cursor.execute(
            "SELECT CURRENT_REVISION_ID FROM SDS_CLASS_RULESETS "
            "WHERE RULESET_ID=:ruleset_id",
            {"ruleset_id": ruleset_id},
        )
        current_revision_id = cursor.fetchone()[0]
        if current_revision_id is None or str(current_revision_id) == "ruleset_default_v1":
            cursor.execute(
                "SELECT COALESCE(MAX(REVISION_NO), 0) + 1 "
                "FROM SDS_CLASS_RULESET_REVS WHERE RULESET_ID=:ruleset_id",
                {"ruleset_id": ruleset_id},
            )
            revision_no = int(cursor.fetchone()[0])
            cursor.execute(
                """
                MERGE INTO SDS_CLASS_RULESET_REVS r
                USING (SELECT :revision_id revision_id FROM dual) s
                ON (r.revision_id=s.revision_id)
                WHEN NOT MATCHED THEN INSERT
                    (revision_id, ruleset_id, revision_no, config_hash, config_json)
                VALUES (:revision_id, :ruleset_id, :revision_no,
                        :config_hash, :config_json)
                """,
                {
                    "revision_id": revision_id,
                    "ruleset_id": ruleset_id,
                    "revision_no": revision_no,
                    "config_hash": config_hash,
                    "config_json": config_json,
                },
            )
            cursor.execute(
                "UPDATE SDS_CLASS_RULESETS SET CURRENT_REVISION_ID=:revision_id "
                "WHERE RULESET_ID=:ruleset_id "
                "AND (CURRENT_REVISION_ID IS NULL "
                "OR CURRENT_REVISION_ID='ruleset_default_v1')",
                {"revision_id": revision_id, "ruleset_id": ruleset_id},
            )
        cursor.execute(
            """
            MERGE INTO SDS_FOLDER_RULESETS f
            USING (SELECT :folder_id folder_id FROM dual) s
            ON (f.folder_id=s.folder_id)
            WHEN NOT MATCHED THEN INSERT
                (folder_id, ruleset_id, inherit_to_descendants, defaults_json)
            VALUES (:folder_id, :ruleset_id, 1, '{}')
            """,
            {"folder_id": ROOT_FOLDER_ID, "ruleset_id": ruleset_id},
        )
        cursor.execute(
            """
            MERGE INTO SDS_SEARCH_CONCEPT_SETTINGS s
            USING (SELECT 'default' settings_id FROM dual) d
            ON (s.settings_id=d.settings_id)
            WHEN NOT MATCHED THEN INSERT
                (settings_id, enabled, auto_publish, auto_publish_confidence,
                 min_support_sets, max_concepts_per_document,
                 initial_display_limit, input_text_limit, prompt_text,
                 taxonomy_revision)
            VALUES ('default', 1, 1, 0.85, 2, 16, 8, 24000,
                    :prompt_text, 1)
            """,
            {"prompt_text": DEFAULT_CONCEPT_PROMPT},
        )
        cursor.execute(
            """
            MERGE INTO SDS_FEATURE_MIGRATIONS m
            USING (SELECT :feature feature_code, :version version_id FROM dual) s
            ON (m.feature_code=s.feature_code AND m.version_id=s.version_id)
            WHEN MATCHED THEN UPDATE SET m.ddl_sha256=:digest, m.details_json=:details,
                m.applied_at=SYSTIMESTAMP
            WHEN NOT MATCHED THEN INSERT
                (feature_code, version_id, ddl_sha256, details_json)
            VALUES (:feature, :version, :digest, :details)
            """,
            {
                "feature": FEATURE_CODE,
                "version": FEATURE_VERSION,
                "digest": _ddl_digest(),
                "details": json.dumps(
                    {
                        "non_destructive": True,
                        "created_tables": created_tables,
                        "created_indexes": created_indexes,
                        "added_columns": added_columns,
                    },
                    ensure_ascii=False,
                ),
            },
        )
        connection.commit()
    return {
        **document_library_schema_status(),
        "created_tables": created_tables,
        "created_indexes": created_indexes,
        "added_columns": added_columns,
        "destructive": False,
    }


def drop_document_library_schema_for_recreate() -> dict[str, Any]:
    """Drop only feature-owned tables before an explicitly confirmed base rebuild."""
    dropped: list[str] = []
    with _connection() as connection, connection.cursor() as cursor:
        existing = _names(cursor, "SELECT TABLE_NAME FROM USER_TABLES")
        for table_name in reversed(tuple(TABLE_STATEMENTS)):
            if table_name not in existing:
                continue
            cursor.execute(f"DROP TABLE {table_name} CASCADE CONSTRAINTS PURGE")
            dropped.append(table_name)
        connection.commit()
    return {"dropped_tables": dropped, "destructive": bool(dropped)}
