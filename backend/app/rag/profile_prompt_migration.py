from __future__ import annotations

from typing import Any

from app.rag.profile_prompts import (
    CONSTRUCTION_PHOTO_PROFILE_PROMPT,
    PRESENTATION_DESIGN_PROFILE_PROMPT,
    PROPOSAL_PLAN_PROFILE_PROMPT,
)
from app.rag.profile_repository import profile_repository


RECOMMENDED_PROFILE_UPDATES = {
    1: ("住宅施工写真", CONSTRUCTION_PHOTO_PROFILE_PROMPT),
    2: ("住宅提案・図面", PROPOSAL_PLAN_PROFILE_PROMPT),
}

DESIGN_PROFILE_UPDATES = {
    1: ("住宅施工写真・デザイン", CONSTRUCTION_PHOTO_PROFILE_PROMPT),
    3: ("住宅提案プレゼン・デザイン", PRESENTATION_DESIGN_PROFILE_PROMPT),
}


def snapshot_profiles() -> list[dict[str, Any]]:
    """Capture the exact revisions so rollback can reuse already indexed releases."""
    return [
        profile_repository.get_profile(slot_no).model_dump(mode="json")
        for slot_no in sorted(RECOMMENDED_PROFILE_UPDATES)
    ]


def apply_recommended_profiles() -> list[dict[str, Any]]:
    """Update prompts without queuing VLM jobs. Existing documents remain pending."""
    results: list[dict[str, Any]] = []
    for slot_no, (name, prompt) in RECOMMENDED_PROFILE_UPDATES.items():
        current = profile_repository.get_profile(slot_no)
        saved = profile_repository.apply_profile(
            current.model_copy(update={"name": name, "extraction_prompt": prompt})
        )
        results.append(
            {
                "slot_no": saved.slot_no,
                "name": saved.name,
                "enabled": saved.enabled,
                "apply_status": saved.apply_status,
                "pending_document_count": saved.pending_document_count,
                "current_revision_id": saved.current_revision_id,
            }
        )
    return results


def restore_profiles(snapshot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Restore the exact prior revision IDs instead of creating duplicate revisions."""
    expected_slots = set(RECOMMENDED_PROFILE_UPDATES)
    by_slot = {int(item["slot_no"]): item for item in snapshot}
    if set(by_slot) != expected_slots:
        raise ValueError("profile snapshot must contain exactly slots 1 and 2")
    with profile_repository._connection() as connection, connection.cursor() as cursor:
        for slot_no in sorted(expected_slots):
            item = by_slot[slot_no]
            revision_id = str(item["current_revision_id"])
            cursor.execute(
                "SELECT COUNT(*) FROM SDS_VLM_PROFILE_REVISIONS "
                "WHERE REVISION_ID=:revision AND SLOT_NO=:slot",
                {"revision": revision_id, "slot": slot_no},
            )
            if int(cursor.fetchone()[0]) != 1:
                raise ValueError(f"profile revision is missing for slot {slot_no}")
            cursor.execute(
                """
                UPDATE SDS_VLM_PROFILES
                SET NAME=:name, ENABLED=:enabled, CURRENT_REVISION_ID=:revision,
                    APPLY_STATUS='PENDING', UPDATED_AT=SYSTIMESTAMP
                WHERE SLOT_NO=:slot
                """,
                {
                    "name": str(item["name"]),
                    "enabled": int(bool(item["enabled"])),
                    "revision": revision_id,
                    "slot": slot_no,
                },
            )
        connection.commit()
    for slot_no in sorted(expected_slots):
        profile_repository.refresh_apply_status(slot_no)
    return [
        {
            "slot_no": profile.slot_no,
            "name": profile.name,
            "enabled": profile.enabled,
            "apply_status": profile.apply_status,
            "pending_document_count": profile.pending_document_count,
            "current_revision_id": profile.current_revision_id,
        }
        for profile in (profile_repository.get_profile(1), profile_repository.get_profile(2))
    ]


def snapshot_design_profiles() -> list[dict[str, Any]]:
    """Capture slots 1 and 3 before applying lifestyle/design prompt revisions."""
    return [
        profile_repository.get_profile(slot_no).model_dump(mode="json")
        for slot_no in sorted(DESIGN_PROFILE_UPDATES)
    ]


def apply_design_profiles() -> list[dict[str, Any]]:
    """Apply lifestyle/design prompts without automatically queuing existing documents."""
    results: list[dict[str, Any]] = []
    for slot_no, (name, prompt) in DESIGN_PROFILE_UPDATES.items():
        current = profile_repository.get_profile(slot_no)
        saved = profile_repository.apply_profile(
            current.model_copy(update={"name": name, "extraction_prompt": prompt})
        )
        results.append(
            {
                "slot_no": saved.slot_no,
                "name": saved.name,
                "enabled": saved.enabled,
                "apply_status": saved.apply_status,
                "pending_document_count": saved.pending_document_count,
                "current_revision_id": saved.current_revision_id,
            }
        )
    return results


def restore_design_profiles(snapshot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Restore the exact prior revisions for slots 1 and 3."""
    expected_slots = set(DESIGN_PROFILE_UPDATES)
    by_slot = {int(item["slot_no"]): item for item in snapshot}
    if set(by_slot) != expected_slots:
        raise ValueError("profile snapshot must contain exactly slots 1 and 3")
    with profile_repository._connection() as connection, connection.cursor() as cursor:
        for slot_no in sorted(expected_slots):
            item = by_slot[slot_no]
            revision_id = str(item["current_revision_id"])
            cursor.execute(
                "SELECT COUNT(*) FROM SDS_VLM_PROFILE_REVISIONS "
                "WHERE REVISION_ID=:revision AND SLOT_NO=:slot",
                {"revision": revision_id, "slot": slot_no},
            )
            if int(cursor.fetchone()[0]) != 1:
                raise ValueError(f"profile revision is missing for slot {slot_no}")
            cursor.execute(
                """
                UPDATE SDS_VLM_PROFILES
                SET NAME=:name, ENABLED=:enabled, CURRENT_REVISION_ID=:revision,
                    APPLY_STATUS='PENDING', UPDATED_AT=SYSTIMESTAMP
                WHERE SLOT_NO=:slot
                """,
                {
                    "name": str(item["name"]),
                    "enabled": int(bool(item["enabled"])),
                    "revision": revision_id,
                    "slot": slot_no,
                },
            )
        connection.commit()
    for slot_no in sorted(expected_slots):
        profile_repository.refresh_apply_status(slot_no)
    profiles = [profile_repository.get_profile(slot_no) for slot_no in sorted(expected_slots)]
    return [
        {
            "slot_no": profile.slot_no,
            "name": profile.name,
            "enabled": profile.enabled,
            "apply_status": profile.apply_status,
            "pending_document_count": profile.pending_document_count,
            "current_revision_id": profile.current_revision_id,
        }
        for profile in profiles
    ]
