from __future__ import annotations

from contextlib import contextmanager

from app.rag.classification_rules import (
    evaluate_filename_rules,
    normalize_customer_name,
)
from app.rag.document_metadata_api import filter_existing_source_objects
from app.rag.document_metadata_schema import TAG_SEEDS, _default_ruleset
from app.rag.draft_classifier import (
    _photo_tag_guidance,
    _proposal_material_tag_guidance,
)
from app.rag.document_metadata_models import (
    BulkDocumentSelection,
    ClassificationRule,
    ClassificationRuleSetConfig,
    IngestItemReview,
    RuleCandidate,
    RuleCondition,
    SearchConceptSettings,
    TagDefinition,
)
from app.rag.document_metadata_repository import DocumentMetadataRepository
from app.rag.models import (
    BuildingConditionSearchFilter,
    CustomerSearchFilter,
    FolderSearchScope,
    MetadataSearchFilters,
    TagSearchFilter,
)
from app.rag.oracle_repository import OracleRagRepository


def _tag(
    tag_id: str,
    *,
    group_id: str,
    code: str,
    name: str,
    mode: str = "SINGLE",
) -> TagDefinition:
    return TagDefinition(
        tag_id=tag_id,
        group_id=group_id,
        group_code=group_id,
        group_name=group_id,
        selection_mode=mode,
        code=code,
        name=name,
        active=True,
        sort_order=0,
    )


def _example_config() -> ClassificationRuleSetConfig:
    return ClassificationRuleSetConfig(
        customer_suffixes=["様邸", "邸"],
        rules=[
            ClassificationRule(
                rule_id="floorplan",
                name="間取り図",
                priority=100,
                condition=RuleCondition(any_terms=["平面図", "計画図", "現況図"]),
                tag_ids=["tag_floorplan"],
                customer_pattern=r"(?:^|_)(?P<customer>[^_]+様邸)(?:_|現況|計画)",
            ),
            ClassificationRule(
                rule_id="state_plan",
                name="提案・計画",
                priority=100,
                condition=RuleCondition(any_terms=["plan", "計画"]),
                tag_ids=["tag_plan"],
            ),
            ClassificationRule(
                rule_id="state_existing",
                name="現況",
                priority=100,
                condition=RuleCondition(any_terms=["現況"]),
                tag_ids=["tag_existing"],
            ),
            ClassificationRule(
                rule_id="floor_1f",
                name="1F",
                priority=100,
                condition=RuleCondition(any_terms=["1f"]),
                tag_ids=["tag_1f"],
            ),
            ClassificationRule(
                rule_id="perspective",
                name="パース画像",
                priority=100,
                condition=RuleCondition(all_terms=["パース"], extensions=["jpg", "jpeg"]),
                tag_ids=["tag_perspective"],
            ),
        ],
    )


TAGS = [
    _tag("tag_floorplan", group_id="document_kind", code="floorplan", name="間取り図"),
    _tag("tag_perspective", group_id="document_kind", code="perspective", name="パース画像"),
    _tag("tag_photo", group_id="document_kind", code="photo", name="写真"),
    _tag(
        "tag_document_proposal_material",
        group_id="document_kind",
        code="proposal_material",
        name="提案資料",
    ),
    _tag("tag_plan", group_id="state", code="plan", name="提案・計画"),
    _tag("tag_existing", group_id="state", code="existing", name="現況"),
    _tag("tag_1f", group_id="floor", code="floor_1f", name="1F"),
]


def _candidate_values(filename: str) -> dict[str, list[RuleCandidate]]:
    result = evaluate_filename_rules(filename, _example_config(), TAGS)
    values: dict[str, list[RuleCandidate]] = {}
    for candidate in result.candidates:
        values.setdefault(candidate.field_kind, []).append(candidate)
    return values


def test_strong_filename_rules_cover_examples_without_guessing_yearless_date() -> None:
    plan = _candidate_values("20240203_森様邸_計画図.pdf")
    assert {item.target_key for item in plan["TAG"]} == {
        "tag_floorplan",
        "tag_plan",
    }
    assert plan["DATE"][0].value_raw == "2024-02"
    assert plan["DATE"][0].confirmed is True
    assert plan["CUSTOMER"][0].value_raw == "森様邸"
    assert plan["CUSTOMER"][0].confirmed is False

    current = _candidate_values("01.現況-1F 平面図.pdf")
    assert {item.target_key for item in current["TAG"]} == {
        "tag_floorplan",
        "tag_existing",
        "tag_1f",
    }

    image = _candidate_values("IMG_1760 2024-06-06 05_33_46.JPG")
    assert image["DATE"][0].value_raw == "2024-06"
    assert "TAG" not in image

    yearless = _candidate_values("中村様邸現況図0704.pdf")
    assert "DATE" not in yearless
    assert {item.target_key for item in yearless["TAG"]} == {
        "tag_floorplan",
        "tag_existing",
    }


def test_photo_is_seeded_for_vlm_review_without_filename_extension_guessing() -> None:
    assert (
        "tag_document_photo",
        "tag_group_document",
        "photo",
        "写真",
        30,
    ) in TAG_SEEDS
    guidance = _photo_tag_guidance(TAGS)
    assert "実世界をカメラ" in guidance
    assert "パース画像" in guidance
    assert "判別できなければ候補を返さない" in guidance


def test_proposal_material_is_seeded_and_strong_filename_rule_is_scoped() -> None:
    assert (
        "tag_document_proposal_material",
        "tag_group_document",
        "proposal_material",
        "提案資料",
        40,
    ) in TAG_SEEDS
    tags = [
        _tag(
            "tag_document_floor_plan",
            group_id="tag_group_document",
            code="floor_plan",
            name="間取り図",
        ),
        _tag(
            "tag_document_proposal_material",
            group_id="tag_group_document",
            code="proposal_material",
            name="提案資料",
        ),
    ]
    proposal = evaluate_filename_rules(
        "0314-ﾌﾟﾚｾﾞﾝ.pdf",
        _default_ruleset(),
        tags,
    )
    assert [
        item.target_key
        for item in proposal.candidates
        if item.field_kind == "TAG"
    ] == ["tag_document_proposal_material"]
    assert not [
        item
        for item in evaluate_filename_rules(
            "プレゼン用写真.jpg",
            _default_ruleset(),
            tags,
        ).candidates
        if item.field_kind == "TAG"
    ]
    guidance = _proposal_material_tag_guidance(tags)
    assert "複数ページ" in guidance
    assert "個別の平面図" in guidance
    assert "根拠がなければ" in guidance


def test_invalid_calendar_date_is_not_automatically_confirmed() -> None:
    values = _candidate_values("20240231_森様邸_計画図.pdf")
    assert "DATE" not in values


def test_same_priority_exclusive_tag_conflict_stays_ambiguous() -> None:
    config = ClassificationRuleSetConfig(
        rules=[
            ClassificationRule(
                rule_id="one",
                name="one",
                priority=10,
                condition=RuleCondition(all_terms=["対象"]),
                tag_ids=["tag_plan"],
            ),
            ClassificationRule(
                rule_id="two",
                name="two",
                priority=10,
                condition=RuleCondition(all_terms=["対象"]),
                tag_ids=["tag_existing"],
            ),
        ]
    )
    result = evaluate_filename_rules("対象.pdf", config, TAGS)
    candidates = [item for item in result.candidates if item.field_kind == "TAG"]
    assert {item.target_key for item in candidates} == {"tag_plan", "tag_existing"}
    assert all(item.ambiguous and not item.confirmed for item in candidates)
    assert any("競合" in warning for warning in result.warnings)


def test_customer_normalization_preserves_display_and_builds_search_key() -> None:
    value = normalize_customer_name("  森　様邸  ", suffixes=["様邸", "邸"])
    assert value.raw == "森 様邸"
    assert value.normalized == "森 様邸"
    assert value.search_key == "森"


def test_existing_object_filter_protects_generated_and_stable_objects() -> None:
    stable_id = "a" * 32
    objects = [
        {"name": "legacy/plan.pdf", "size": 100},
        {"name": "legacy/plan/page_001.png", "size": 10},
        {"name": "standalone/page_001.png", "size": 20},
        {"name": "_pipeline/jobs/output.json", "size": 5},
        {"name": f"documents/{stable_id}/source.pdf", "size": 100},
        {"name": "folder/", "size": 0},
    ]
    sources, protected_count = filter_existing_source_objects(objects)
    assert {item["name"] for item in sources} == {
        "legacy/plan.pdf",
        "standalone/page_001.png",
    }
    assert protected_count == 2


class _Cursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, binds: dict | None = None) -> None:
        self.calls.append((sql, binds or {}))

    def fetchall(self) -> list:
        return []


class _Connection:
    def __init__(self) -> None:
        self.cursor_value = _Cursor()

    def cursor(self) -> _Cursor:
        return self.cursor_value

    def commit(self) -> None:
        pass


class _MetadataCaptureRepository(DocumentMetadataRepository):
    def __init__(self, cursor: _Cursor | None = None) -> None:
        self.connection_value = _Connection()
        if cursor is not None:
            self.connection_value.cursor_value = cursor

    def require_schema(self) -> None:
        pass

    @contextmanager
    def connection(self):
        yield self.connection_value


class _ResultCursor(_Cursor):
    def __init__(self, *, rows: list | None = None, one: tuple | None = None) -> None:
        super().__init__()
        self.result_rows = rows or []
        self.result_one = one

    def fetchall(self) -> list:
        return self.result_rows

    def fetchone(self):
        return self.result_one


def test_bulk_document_selection_is_bounded_trimmed_and_deduplicated() -> None:
    selection = BulkDocumentSelection(
        document_ids=[" document-a ", "document-b", "document-a"]
    )
    assert selection.document_ids == ["document-a", "document-b"]


def test_document_action_queries_only_current_registered_documents() -> None:
    repository = _MetadataCaptureRepository()
    assert repository.documents_for_action(["document-a", "document-a"]) == []
    sql, binds = repository.connection_value.cursor_value.calls[0]
    normalized = " ".join(sql.casefold().split())
    assert "is_current=1" in normalized
    assert "status<>'draft'" in normalized
    assert binds == {"action_document_0": "document-a"}


def test_artifact_cleanup_query_follows_all_document_revisions() -> None:
    repository = _MetadataCaptureRepository()
    assert repository.document_artifact_object_names("document-a") == []
    sql, binds = repository.connection_value.cursor_value.calls[0]
    assert "JOIN sds_document_revisions" in sql
    assert "r.document_id=:document" in sql
    assert binds == {"document": "document-a"}


def test_registered_document_delete_detaches_audit_pointers_before_cascade() -> None:
    class DeleteCursor(_Cursor):
        rowcount = 0

        def execute(self, sql: str, binds: dict | None = None) -> None:
            super().execute(sql, binds)
            normalized = " ".join(sql.casefold().split())
            self.rowcount = 1 if (
                normalized.startswith("update sds_documents")
                or normalized.startswith("delete from sds_documents")
            ) else 4

    class DeleteConnection(_Connection):
        def __init__(self) -> None:
            self.cursor_value = DeleteCursor()
            self.committed = False

        def commit(self) -> None:
            self.committed = True

    class DeleteRepository(_MetadataCaptureRepository):
        def __init__(self) -> None:
            self.connection_value = DeleteConnection()

    repository = DeleteRepository()
    assert repository.delete_registered_document("document-a") == 1
    calls = repository.connection_value.cursor_value.calls
    sql = "\n".join(statement for statement, _ in calls).casefold()
    assert "current_revision_id=null" in sql
    assert "update sds_pipeline_job_steps" in sql
    assert "document_revision_id=null" in sql
    assert "delete from sds_ingest_items" in sql
    assert "delete from sds_documents" in sql
    assert sql.index("delete from sds_ingest_items") < sql.index(
        "delete from sds_documents"
    )
    assert repository.connection_value.committed is True


def test_folder_counts_exclude_drafts_and_non_current_documents() -> None:
    repository = _MetadataCaptureRepository()
    repository.folder_tree()
    sql, _ = repository.connection_value.cursor_value.calls[0]
    normalized = " ".join(sql.casefold().split())
    assert normalized.count("join sds_documents d on d.document_id=dm.document_id") == 2
    assert normalized.count("d.is_current=1 and d.status<>'draft'") == 2


def test_document_list_supports_only_the_declared_sort_orders() -> None:
    expected = {
        "updated_desc": "order by d.updated_at desc nulls last, d.document_id",
        "created_desc": "order by d.uploaded_at desc nulls last, d.document_id",
        "updated_asc": "order by d.updated_at asc nulls last, d.document_id",
        "filename_asc": (
            "order by lower(d.file_name) asc, d.file_name asc, d.document_id"
        ),
    }
    for sort, order_by in expected.items():
        cursor = _ResultCursor(rows=[], one=(0,))
        repository = _MetadataCaptureRepository(cursor)
        result = repository.list_documents(page=1, page_size=20, sort=sort)
        assert result.total == 0
        sql, _ = cursor.calls[1]
        assert order_by in " ".join(sql.casefold().split())

    repository = _MetadataCaptureRepository(_ResultCursor(rows=[], one=(0,)))
    try:
        repository.list_documents(page=1, page_size=20, sort="sql injection")
    except ValueError as error:
        assert "未対応" in str(error)
    else:
        raise AssertionError("未対応の並び順が受理されました")


def test_active_batches_are_scoped_to_owner_and_expose_recovery_counts() -> None:
    cursor = _ResultCursor(rows=[(
        "batch-a", "REVIEW_REQUIRED", "folder-a", "rules-a", None,
        10, 0, 0, "2026-08-03T00:00:00Z", "2026-08-03T00:07:00Z",
        "新築", 7, 3, 0, 1,
    )])
    repository = _MetadataCaptureRepository(cursor)
    batches = repository.list_active_ingest_batches("owner-hash")
    sql, binds = cursor.calls[0]
    assert "b.created_by_hash=:created_by_hash" in sql
    assert "b.status IN ('DRAFT', 'REVIEW_REQUIRED')" in sql
    assert "b.total_items > 0" in sql
    assert binds == {"created_by_hash": "owner-hash"}
    assert len(batches) == 1
    assert batches[0].target_folder_name == "新築"
    assert batches[0].analysis_completed_items == 7
    assert batches[0].analysis_pending_items == 3
    assert batches[0].discardable is True


def test_ingest_batch_owner_mismatch_is_hidden_as_not_found() -> None:
    repository = _MetadataCaptureRepository(
        _ResultCursor(one=("another-owner-hash",))
    )
    try:
        repository.require_ingest_batch_owner("batch-a", "current-owner-hash")
    except LookupError as error:
        assert "見つかりません" in str(error)
    else:
        raise AssertionError("他ユーザーの取込バッチへアクセスできてしまいました")


def test_registered_batch_is_rejected_before_object_deletion() -> None:
    repository = _MetadataCaptureRepository(
        _ResultCursor(one=("REVIEW_REQUIRED", 1))
    )
    try:
        repository.ensure_ingest_batch_discardable("batch-a")
    except ValueError as error:
        assert "登録処理が開始" in str(error)
    else:
        raise AssertionError("登録開始済みバッチを破棄可能と判定しました")


class _CaptureSearchRepository(OracleRagRepository):
    def __init__(self) -> None:
        self.connection_value = _Connection()

    @contextmanager
    def connection(self):
        yield self.connection_value


def test_companion_documents_select_the_first_published_page_thumbnail() -> None:
    repository = _CaptureSearchRepository()
    repository.companion_documents(
        ["set-a"], exclude_document_ids=[], user_hash="u" * 64
    )
    sql, binds = repository.connection_value.cursor_value.calls[0]
    normalized = " ".join(sql.casefold().split())
    assert "c.release_id=d.serving_release_id" in normalized
    assert "c.component_key='render'" in normalized
    assert "a.artifact_kind='page_image'" in normalized
    assert "dense_rank first order by a.page_number nulls last" in normalized
    assert "thumbnail_object_name" in normalized
    assert "thumbnail_page_number" in normalized
    assert binds["document_set_id"] == "set-a"


def _search_filters() -> MetadataSearchFilters:
    return MetadataSearchFilters(
        folder=FolderSearchScope(folder_id="folder_a", include_descendants=True),
        tags=TagSearchFilter(
            all_of=["tag_floorplan"],
            any_of=["tag_plan", "tag_existing"],
            none_of=["tag_archived"],
        ),
        customer=CustomerSearchFilter(values=["森様邸"], match="search_key_exact"),
        document_year_from=2023,
        document_year_to=2025,
        document_months=[2, 6],
    )


def test_metadata_filters_are_in_keyword_candidate_sql_before_row_limit() -> None:
    repository = _CaptureSearchRepository()
    repository.keyword_search(
        query="平面図",
        top_k=20,
        user_hash="u" * 64,
        current_version_only=True,
        document_types=[],
        metadata_filters=_search_filters(),
    )
    sql, binds = repository.connection_value.cursor_value.calls[-1]
    assert sql.index("sds_folder_closure") < sql.index("ROWNUM<=:top_k")
    assert "sds_document_tags" in sql
    assert "customer_name_search_key" in sql
    assert binds["metadata_folder_id"] == "folder_a"
    assert binds["document_month_0"] == 2


def test_metadata_filters_are_in_vector_candidate_sql_before_fetch() -> None:
    repository = _CaptureSearchRepository()
    repository.recipe_vector_search(
        recipe_code="chunk_text",
        embedding=[0.1, 0.2],
        channel="vector:test",
        top_k=20,
        user_hash="u" * 64,
        current_version_only=True,
        document_types=[],
        metadata_filters=_search_filters(),
    )
    sql, _ = repository.connection_value.cursor_value.calls[-1]
    assert sql.index("sds_folder_closure") < sql.index("FETCH APPROX FIRST")
    assert "sds_document_tags" in sql
    assert "customer_name_search_key" in sql


def test_building_conditions_are_in_candidates_before_limits() -> None:
    repository = _CaptureSearchRepository()
    filters = MetadataSearchFilters(
        building=BuildingConditionSearchFilter(
            building_types=["マンション"],
            structures=["RC造"],
            area_types=["専有面積"],
            area_min=70,
            area_max=90,
            area_unit="㎡",
            existing_layouts=["3LDK"],
            proposed_layouts=["2LDK"],
        )
    )
    repository.keyword_search(
        query="回遊動線",
        top_k=20,
        user_hash="u" * 64,
        current_version_only=True,
        document_types=[],
        metadata_filters=filters,
    )
    keyword_sql, binds = repository.connection_value.cursor_value.calls[-1]
    assert keyword_sql.index("sds_document_set_facts") < keyword_sql.index("ROWNUM<=:top_k")
    assert keyword_sql.index("sds_document_set_areas") < keyword_sql.index("ROWNUM<=:top_k")
    assert binds["building_type_0"] == "マンション"
    assert binds["structure_0"] == "RC造"
    assert binds["area_type_0"] == "専有面積"
    assert binds["area_value_min"] == 70
    assert binds["area_value_max"] == 90
    assert binds["existing_layout_0"] == "3LDK"
    assert binds["proposed_layout_0"] == "2LDK"

    repository.recipe_vector_search(
        recipe_code="chunk_text",
        embedding=[0.1, 0.2],
        channel="vector:test",
        top_k=20,
        user_hash="u" * 64,
        current_version_only=True,
        document_types=[],
        metadata_filters=filters,
    )
    vector_sql, _ = repository.connection_value.cursor_value.calls[-1]
    assert vector_sql.index("sds_document_set_facts") < vector_sql.index("FETCH APPROX FIRST")
    assert vector_sql.index("sds_document_set_areas") < vector_sql.index("FETCH APPROX FIRST")


def test_typed_metadata_search_returns_one_representative_page_per_document() -> None:
    repository = _CaptureSearchRepository()
    filters = MetadataSearchFilters(
        building=BuildingConditionSearchFilter(
            area_types=["専有面積"],
            area_min=72,
            area_max=88,
            area_unit="㎡",
        )
    )

    assert repository.metadata_search(
        top_k=20,
        user_hash="u" * 64,
        current_version_only=True,
        document_types=[],
        metadata_filters=filters,
    ) == []

    sql, binds = repository.connection_value.cursor_value.calls[-1]
    normalized = " ".join(sql.casefold().split())
    assert "sds_document_set_areas" in normalized
    assert normalized.index("sds_document_set_areas") < normalized.index(
        "rownum<=:top_k"
    )
    assert "row_number() over" in normalized
    assert "partition by metadata_rows.document_id" in normalized
    assert binds["area_type_0"] == "専有面積"
    assert binds["area_value_min"] == 72
    assert binds["area_value_max"] == 88


def test_required_concepts_are_in_keyword_candidate_sql_before_row_limit() -> None:
    repository = _CaptureSearchRepository()
    filters = MetadataSearchFilters(
        concept_ids=["concept-before", "concept-after"],
        concept_mode="REQUIRE_ALL",
    )
    repository.keyword_search(
        query="収納",
        top_k=20,
        user_hash="u" * 64,
        current_version_only=True,
        document_types=[],
        metadata_filters=filters,
    )
    sql, binds = repository.connection_value.cursor_value.calls[-1]
    assert sql.index("sds_document_concepts") < sql.index("ROWNUM<=:top_k")
    assert sql.count("sds_document_concepts") == 2
    assert "member_m.document_set_id=target_m.document_set_id" in sql
    assert sql.count("sds_index_releases member_rel") == 2
    assert "serving_revision_id" not in sql
    assert binds["required_concept_0"] == "concept-before"
    assert binds["required_concept_1"] == "concept-after"


def test_boost_concepts_do_not_exclude_keyword_candidates() -> None:
    repository = _CaptureSearchRepository()
    repository.keyword_search(
        query="収納",
        top_k=20,
        user_hash="u" * 64,
        current_version_only=True,
        document_types=[],
        metadata_filters=MetadataSearchFilters(
            concept_ids=["concept-before"], concept_mode="BOOST"
        ),
    )
    sql, _ = repository.connection_value.cursor_value.calls[-1]
    assert "sds_document_concepts" not in sql


def test_result_context_collects_selected_concepts_from_accessible_set_members() -> None:
    repository = _CaptureSearchRepository()
    repository.search_document_context(
        ["document-a"],
        selected_concept_ids=["concept-before"],
        user_hash="u" * 64,
    )
    sql, binds = repository.connection_value.cursor_value.calls[-1]
    assert "member_m.document_set_id=m.document_set_id" in sql
    assert "member_acl.document_id=member_d.document_id" in sql
    assert "member_rel.release_id=member_d.serving_release_id" in sql
    assert "dc.revision_id=member_rel.document_revision_id" in sql
    assert "c.release_id=d.serving_release_id" in sql
    assert "a.artifact_kind='PAGE_IMAGE'" in sql
    assert "thumbnail_object_name" in sql
    assert "thumbnail_page_number" in sql
    assert "serving_revision_id" not in sql
    assert binds["context_concept_0"] == "concept-before"


class _ConceptSqlCaptureRepository(_MetadataCaptureRepository):
    def get_concept_settings(self) -> SearchConceptSettings:
        return SearchConceptSettings(
            enabled=True,
            auto_publish=False,
            prompt_text="test",
        )


def test_search_candidate_list_excludes_zero_support_when_requested() -> None:
    repository = _MetadataCaptureRepository()
    repository.list_search_concepts(include_zero_support=False)
    sql, _ = repository.connection_value.cursor_value.calls[0]
    normalized = " ".join(sql.casefold().split())
    assert "c.support_document_count>0" in normalized


def test_document_concept_aggregation_uses_serving_release_revision() -> None:
    repository = _ConceptSqlCaptureRepository()
    repository.replace_document_concepts(
        document_id="document-a",
        revision_id="revision-a",
        stage_run_id="run-a",
        concepts=[],
    )
    sql = "\n".join(
        statement
        for statement, _ in repository.connection_value.cursor_value.calls
    ).casefold()
    assert "serving_revision_id" not in sql
    assert "sds_index_releases" in sql
    assert "rel.document_revision_id=dc.revision_id" in sql
    assert "where c.status<>'merged'" in sql
    assert "where exists (" not in sql


def test_concept_publish_and_default_lookup_use_serving_release_revision() -> None:
    repository = _ConceptSqlCaptureRepository()
    repository.update_concept_settings(
        SearchConceptSettings(
            enabled=True,
            auto_publish=True,
            prompt_text="test",
        )
    )
    repository.list_document_concepts("document-a")
    sql = "\n".join(
        statement
        for statement, _ in repository.connection_value.cursor_value.calls
    ).casefold()
    assert "serving_revision_id" not in sql
    assert "rel.release_id=d.serving_release_id" in sql
    assert "select rel.document_revision_id" in sql


class _CommitRepository(DocumentMetadataRepository):
    def __init__(self, item: IngestItemReview) -> None:
        self.item = item
        self.patch = None
        self.connection_value = _Connection()

    def get_ingest_item(self, _item_id: str) -> IngestItemReview:
        return self.item

    def patch_document_metadata(self, _document_id, payload, *, changed_by_hash):
        self.patch = payload

    @contextmanager
    def connection(self):
        yield self.connection_value


def test_explicitly_cleared_review_values_do_not_restore_confirmed_candidates() -> None:
    item = IngestItemReview(
        item_id="item",
        batch_id="batch",
        document_id="document",
        original_filename="example.pdf",
        object_name="documents/document/source.pdf",
        file_size=1,
        state="CONFIRMED",
        folder_id="folder_unclassified",
        review={
            "tag_ids": [],
            "customer_name_raw": "",
            "document_year": None,
            "document_month": None,
            "date_precision": "UNKNOWN",
        },
        candidates=[
            RuleCandidate(
                field_kind="TAG",
                target_key="tag_floorplan",
                value_raw="間取り図",
                source="RULE",
                confirmed=True,
            ),
            RuleCandidate(
                field_kind="CUSTOMER",
                target_key="customer_name",
                value_raw="森様邸",
                source="FOLDER_DEFAULT",
                confirmed=True,
            ),
            RuleCandidate(
                field_kind="DATE",
                target_key="document_year_month",
                value_raw="2024-02",
                source="AUTO_FILENAME",
                confirmed=True,
            ),
        ],
    )
    repository = _CommitRepository(item)
    repository.commit_ingest_item("item", changed_by_hash=None)
    assert repository.patch.tag_ids == []
    assert repository.patch.customer_name_raw == ""
    assert repository.patch.document_year is None
    assert repository.patch.document_month is None
