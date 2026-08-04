from __future__ import annotations

from typing import Any

from app.rag.document_metadata_repository import document_metadata_repository
from app.rag.profile_prompts import SEARCH_CONCEPT_EXTRACTION_PROMPT


def snapshot_concept_prompt() -> dict[str, Any]:
    settings = document_metadata_repository.get_concept_settings()
    return {
        "prompt_text": settings.prompt_text,
        "taxonomy_revision": settings.taxonomy_revision,
    }


def apply_recommended_concept_prompt() -> dict[str, Any]:
    """Replace only extraction semantics; keep thresholds and publication policy."""
    document_metadata_repository.require_schema()
    current = document_metadata_repository.get_concept_settings()
    if current.prompt_text == SEARCH_CONCEPT_EXTRACTION_PROMPT:
        return current.model_dump(mode="json")
    with (
        document_metadata_repository.connection() as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            """
            UPDATE sds_search_concept_settings
            SET prompt_text=:prompt,
                taxonomy_revision=:revision,
                updated_at=SYSTIMESTAMP
            WHERE settings_id='default'
            """,
            {
                "prompt": SEARCH_CONCEPT_EXTRACTION_PROMPT,
                "revision": current.taxonomy_revision + 1,
            },
        )
        if cursor.rowcount != 1:
            raise LookupError("検索コンセプト設定が見つかりません")
        connection.commit()
    return document_metadata_repository.get_concept_settings().model_dump(mode="json")


def restore_concept_prompt(snapshot: dict[str, Any]) -> dict[str, Any]:
    prompt_text = str(snapshot.get("prompt_text") or "").strip()
    taxonomy_revision = int(snapshot.get("taxonomy_revision") or 0)
    if not prompt_text or taxonomy_revision < 1:
        raise ValueError("検索コンセプトプロンプトのバックアップが不正です")
    document_metadata_repository.require_schema()
    with (
        document_metadata_repository.connection() as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            """
            UPDATE sds_search_concept_settings
            SET prompt_text=:prompt,
                taxonomy_revision=:revision,
                updated_at=SYSTIMESTAMP
            WHERE settings_id='default'
            """,
            {"prompt": prompt_text, "revision": taxonomy_revision},
        )
        if cursor.rowcount != 1:
            raise LookupError("検索コンセプト設定が見つかりません")
        connection.commit()
    return document_metadata_repository.get_concept_settings().model_dump(mode="json")
