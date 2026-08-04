from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from openai import APITimeoutError

from app.rag import concept_prompt_migration
from app.rag.models import ProfileConfig
from app.rag.pipeline_dispatcher import PipelineDispatcher
from app.rag.pipeline_engine import (
    ObjectContext,
    PipelineEngine,
    concept_only_job_objects,
    SEARCH_CONCEPT_TAXONOMY_INSTRUCTION,
    pipeline_max_concurrent_files,
)
from app.rag.pipeline_models import EmbeddingRecipe, EmbeddingRecipeInput
from app.rag.pipeline_repository import (
    LeaseLostError,
    OraclePipelineRepository,
    RevisionRecord,
    release_validation_error_message,
)
from app.rag.profile_repository import profile_repository
from app.rag.profile_prompts import SEARCH_CONCEPT_EXTRACTION_PROMPT
from app.rag.service_settings import retrieval_service_settings
from app.rag.pipeline_repository_types import embedding_input_fingerprint


async def _inline_to_thread(
    function: object, *args: object, **kwargs: object
) -> object:
    assert callable(function)
    return function(*args, **kwargs)


def test_concept_prompt_migration_preserves_thresholds_and_bumps_taxonomy() -> None:
    current = SimpleNamespace(prompt_text="old prompt", taxonomy_revision=7)
    updated = SimpleNamespace(
        model_dump=MagicMock(
            return_value={
                "prompt_text": SEARCH_CONCEPT_EXTRACTION_PROMPT,
                "taxonomy_revision": 8,
            }
        )
    )
    connection_context = MagicMock()
    connection = connection_context.__enter__.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.rowcount = 1

    with (
        patch.object(concept_prompt_migration.document_metadata_repository, "require_schema"),
        patch.object(
            concept_prompt_migration.document_metadata_repository,
            "get_concept_settings",
            side_effect=[current, updated],
        ),
        patch.object(
            concept_prompt_migration.document_metadata_repository,
            "connection",
            return_value=connection_context,
        ),
    ):
        result = concept_prompt_migration.apply_recommended_concept_prompt()

    sql, binds = cursor.execute.call_args.args
    assert "UPDATE sds_search_concept_settings" in sql
    assert binds == {
        "prompt": SEARCH_CONCEPT_EXTRACTION_PROMPT,
        "revision": 8,
    }
    assert result["taxonomy_revision"] == 8
    connection.commit.assert_called_once()


def test_search_concept_taxonomy_has_a_lifestyle_category() -> None:
    assert "LIFESTYLE(暮らし・過ごし方)" in SEARCH_CONCEPT_TAXONOMY_INSTRUCTION
    assert "DESIGN(デザイン・テイスト)" in SEARCH_CONCEPT_TAXONOMY_INSTRUCTION
    assert "KITCHEN(キッチン・LDK)" in SEARCH_CONCEPT_TAXONOMY_INSTRUCTION


def _object_context(object_name: str) -> ObjectContext:
    revision = RevisionRecord(
        document_id=f"document-{object_name}",
        revision_id=f"revision-{object_name}",
        content_sha256="a" * 64,
        bucket="documents",
        object_name=object_name,
        file_name=object_name,
        media_type="application/pdf",
        document_type="pdf",
        content_changed=True,
    )
    return ObjectContext(b"pdf", revision, f"release-{object_name}")


class _RuntimeRepository:
    def __init__(self) -> None:
        self.steps = [
            {
                "step_id": "step-1",
                "object_name": "catalog.pdf",
                "stage_kind": "RENDER",
                "component_key": "render",
                "attempt_count": 0,
            }
        ]
        self.calls: list[tuple[object, ...]] = []

    def get_job(self, job_id: str) -> dict[str, object]:
        return {
            "job_id": job_id,
            "status": "RUNNING",
            "cancel_requested": False,
            "lease_owner": "worker-1",
            "lease_generation": 1,
        }

    def next_step(
        self,
        job_id: str,
        exclude_object_names: tuple[str, ...] = (),
    ) -> dict[str, object] | None:
        del job_id
        excluded = set(exclude_object_names)
        for index, step in enumerate(self.steps):
            if str(step["object_name"]) not in excluded:
                return self.steps.pop(index)
        return None

    def start_step(self, step_id: str, **kwargs: object) -> None:
        self.calls.append(("start", step_id, kwargs))

    def attach_step_context(self, step_id: str, **kwargs: object) -> None:
        self.calls.append(("attach", step_id, kwargs))

    def complete_step(
        self, step_id: str, run_id: str | None, **kwargs: object
    ) -> None:
        self.calls.append(("complete", step_id, run_id, kwargs))

    def fail_step(self, step_id: str, error: str, **kwargs: object) -> None:
        self.calls.append(("fail", step_id, error, kwargs))

    def requeue_step(
        self,
        step_id: str,
        error: str,
        *,
        owner: str,
        generation: int,
        attempt: int,
    ) -> None:
        self.calls.append(
            ("requeue", step_id, error, owner, generation, attempt)
        )
        self.steps.insert(
            0,
            {
                "step_id": step_id,
                "object_name": "catalog.pdf",
                "stage_kind": "RENDER",
                "component_key": "render",
                "attempt_count": attempt,
            },
        )

    def heartbeat(self, job_id: str, owner: str, generation: int) -> bool:
        self.calls.append(("heartbeat", job_id, owner, generation))
        return True

    def finish_job(self, job_id: str, owner: str, generation: int) -> str:
        self.calls.append(("finish", job_id, owner, generation))
        return "FAILED" if any(call[0] == "fail" for call in self.calls) else "SUCCEEDED"


class _MultiJobRuntimeRepository(_RuntimeRepository):
    def __init__(self, steps_by_job: dict[str, list[dict[str, object]]]) -> None:
        super().__init__()
        self.steps_by_job = steps_by_job

    def next_step(
        self,
        job_id: str,
        exclude_object_names: tuple[str, ...] = (),
    ) -> dict[str, object] | None:
        excluded = set(exclude_object_names)
        steps = self.steps_by_job[job_id]
        for index, step in enumerate(steps):
            if str(step["object_name"]) not in excluded:
                return steps.pop(index)
        return None


def test_concept_only_job_objects_detects_read_only_retry_targets() -> None:
    assert concept_only_job_objects(
        {
            "steps": [
                {
                    "object_name": "photo.jpg",
                    "stage_kind": "CONCEPT",
                },
                {
                    "object_name": "plan.pdf",
                    "stage_kind": "CONCEPT",
                },
                {
                    "object_name": "plan.pdf",
                    "stage_kind": "PUBLISH",
                },
            ]
        }
    ) == {"photo.jpg"}


@pytest.mark.asyncio
async def test_concept_only_context_reuses_release_without_creating_draft() -> None:
    repository = MagicMock()
    revision = _object_context("photo.jpg").revision
    repository.register_revision.return_value = revision
    repository.release_for_enrichment.return_value = "serving-release"
    engine = PipelineEngine(repository)

    with (
        patch("app.rag.pipeline_engine.oci_service.download_object", return_value=b"jpg"),
        patch("app.rag.pipeline_engine.asyncio.to_thread", side_effect=_inline_to_thread),
    ):
        context = await engine._context(
            "retry-job",
            "worker-1",
            1,
            "photo.jpg",
            {},
            read_only_enrichment=True,
        )

    assert context.release_id == "serving-release"
    repository.release_for_enrichment.assert_called_once_with(revision)
    repository.ensure_draft_release.assert_not_called()


def test_release_for_enrichment_prefers_current_serving_release() -> None:
    repository = OraclePipelineRepository()
    connection_context = MagicMock()
    connection = connection_context.__enter__.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (
        "serving-1",
        "revision-1",
        "draft-stranded",
        "revision-1",
    )
    revision = SimpleNamespace(document_id="doc-1", revision_id="revision-1")

    with patch.object(repository, "connection", return_value=connection_context):
        release_id = repository.release_for_enrichment(revision)

    assert release_id == "serving-1"
    sql, params = cursor.execute.call_args.args
    assert "LEFT JOIN sds_index_releases serving" in sql
    assert params == {"document": "doc-1"}


def test_repair_stranded_concept_only_draft_restores_indexed_document() -> None:
    repository = OraclePipelineRepository()
    connection_context = MagicMock()
    connection = connection_context.__enter__.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.side_effect = [
        [("doc-1", "photo.jpg", "draft-1", "serving-1")],
        [("render", "run-render"), ("embedding:image", "run-image")],
        [("render", "run-render"), ("embedding:image", "run-image")],
    ]
    cursor.rowcount = 1

    with patch.object(repository, "connection", return_value=connection_context):
        repaired = repository.repair_stranded_concept_only_drafts()

    assert repaired == ["photo.jpg"]
    update_calls = [
        call
        for call in cursor.execute.call_args_list
        if "UPDATE sds_" in str(call.args[0])
    ]
    assert "UPDATE sds_documents" in update_calls[0].args[0]
    assert "UPDATE sds_index_releases" in update_calls[1].args[0]
    connection.commit.assert_called_once_with()


def test_job_lineage_separates_original_and_additional_jobs() -> None:
    repository = OraclePipelineRepository()
    connection_context = MagicMock()
    connection = connection_context.__enter__.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [
        (
            "job-original",
            "PARTIAL_FAILED",
            "FULL",
            "AUTO",
            10,
            9,
            1,
            "concept failed",
            json.dumps({"mode": "FULL"}),
            "2026-08-03T00:00:00Z",
            "2026-08-03T00:01:00Z",
            1,
        ),
        (
            "job-retry",
            "SUCCEEDED",
            "CUSTOM",
            "AUTO",
            1,
            1,
            0,
            None,
            json.dumps({"retry_of_job_id": "job-original"}),
            "2026-08-03T00:02:00Z",
            "2026-08-03T00:03:00Z",
            1,
        ),
    ]

    with patch.object(repository, "connection", return_value=connection_context):
        lineage = repository.job_lineage_for_object("photo.jpg")

    assert [job["tab_label"] for job in lineage] == ["全体工程", "追加Job 1"]
    assert lineage[0]["is_additional"] is False
    assert lineage[1]["is_additional"] is True
    assert lineage[1]["retry_of_job_id"] == "job-original"


def test_pipeline_global_concurrency_defaults_to_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PIPELINE_MAX_CONCURRENT_FILES", raising=False)

    assert pipeline_max_concurrent_files() == 3


def test_pipeline_specific_limits_cannot_exceed_the_global_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIPELINE_MAX_CONCURRENT_FILES", "2")
    monkeypatch.setenv("PIPELINE_MAX_CONCURRENT_JOBS", "5")
    monkeypatch.setenv("PIPELINE_MAX_CONCURRENT_FILES_PER_JOB", "5")
    monkeypatch.setenv("PIPELINE_MAX_CONCURRENT_VLM_STEPS", "5")

    engine = PipelineEngine(_RuntimeRepository())  # type: ignore[arg-type]
    dispatcher = PipelineDispatcher()

    assert engine._max_concurrent_files == 2
    assert engine._max_concurrent_files_per_job() == 2
    assert engine._max_concurrent_vlm_steps() == 2
    assert dispatcher._max_concurrent_jobs == 2


@pytest.mark.asyncio
async def test_dispatcher_stop_cancels_a_long_running_job_task() -> None:
    dispatcher = PipelineDispatcher()
    entered = asyncio.Event()

    async def long_running_dispatch() -> None:
        entered.set()
        await asyncio.Event().wait()

    dispatcher._run = long_running_dispatch  # type: ignore[method-assign]
    await dispatcher.start()
    await entered.wait()

    await asyncio.wait_for(dispatcher.stop(), timeout=0.5)

    assert dispatcher._task is None


@pytest.mark.asyncio
async def test_dispatcher_runs_two_durable_jobs_without_head_of_line_blocking() -> None:
    dispatcher = PipelineDispatcher(max_concurrent_jobs=2)
    both_started = asyncio.Event()
    started: set[str] = set()

    async def process_job(job_id: str, owner: str, generation: int) -> str:
        del owner, generation
        started.add(job_id)
        if len(started) == 2:
            both_started.set()
        await asyncio.Event().wait()
        return "SUCCEEDED"

    with (
        patch(
            "app.rag.pipeline_dispatcher.pipeline_repository.schema_ready",
            return_value=True,
        ),
        patch(
            "app.rag.pipeline_dispatcher.pipeline_repository.claim_next_job",
            side_effect=[("job-1", 1), ("job-2", 1), None],
        ),
        patch(
            "app.rag.pipeline_dispatcher.pipeline_engine.process_job",
            side_effect=process_job,
        ),
        patch(
            "app.rag.pipeline_dispatcher.asyncio.to_thread",
            side_effect=_inline_to_thread,
        ),
    ):
        await dispatcher.start()
        try:
            await asyncio.wait_for(both_started.wait(), timeout=1)
        finally:
            await dispatcher.stop()

    assert started == {"job-1", "job-2"}


@pytest.mark.asyncio
async def test_four_files_run_three_at_a_time_within_one_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIPELINE_MAX_CONCURRENT_FILES", "3")
    monkeypatch.setenv("PIPELINE_MAX_CONCURRENT_FILES_PER_JOB", "3")
    repository = _RuntimeRepository()
    repository.steps = [
        {
            "step_id": f"step-{index}",
            "object_name": f"file-{index}.pdf",
            "stage_kind": "RENDER",
            "component_key": "render",
            "attempt_count": 0,
        }
        for index in range(1, 5)
    ]
    engine = PipelineEngine(repository)  # type: ignore[arg-type]
    releases = {
        f"file-{index}.pdf": asyncio.Event() for index in range(1, 5)
    }
    three_started = asyncio.Event()
    fourth_started = asyncio.Event()
    started: list[str] = []
    active = 0
    peak = 0

    async def context(
        job_id: str,
        owner: str,
        generation: int,
        object_name: str,
        cache: dict[str, ObjectContext],
    ) -> ObjectContext:
        del job_id, owner, generation, cache
        return _object_context(object_name)

    async def execute(**kwargs: object) -> tuple[str, bool]:
        nonlocal active, peak
        step = kwargs["step"]
        assert isinstance(step, dict)
        object_name = str(step["object_name"])
        active += 1
        peak = max(peak, active)
        started.append(object_name)
        if len(started) == 3:
            three_started.set()
        if len(started) == 4:
            fourth_started.set()
        try:
            await releases[object_name].wait()
        finally:
            active -= 1
        return f"run-{object_name}", False

    engine._context = AsyncMock(side_effect=context)
    engine._execute = AsyncMock(side_effect=execute)

    with patch(
        "app.rag.pipeline_engine.asyncio.to_thread",
        side_effect=_inline_to_thread,
    ):
        job_task = asyncio.create_task(
            engine.process_job("job-1", "worker-1", 1)
        )
        try:
            await asyncio.wait_for(three_started.wait(), timeout=1)
            await asyncio.sleep(0)
            assert len(started) == 3
            assert not fourth_started.is_set()

            releases[started[0]].set()
            await asyncio.wait_for(fourth_started.wait(), timeout=1)
            assert peak == 3

            for release in releases.values():
                release.set()
            assert await asyncio.wait_for(job_task, timeout=1) == "SUCCEEDED"
        finally:
            for release in releases.values():
                release.set()
            if not job_task.done():
                job_task.cancel()
                await asyncio.gather(job_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_global_file_limit_is_shared_across_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIPELINE_MAX_CONCURRENT_FILES", "3")
    monkeypatch.setenv("PIPELINE_MAX_CONCURRENT_FILES_PER_JOB", "3")
    repository = _MultiJobRuntimeRepository(
        {
            job_id: [
                {
                    "step_id": f"{job_id}-step-{index}",
                    "object_name": f"{job_id}-file-{index}.pdf",
                    "stage_kind": "RENDER",
                    "component_key": "render",
                    "attempt_count": 0,
                }
                for index in range(1, 3)
            ]
            for job_id in ("job-1", "job-2")
        }
    )
    engine = PipelineEngine(repository)  # type: ignore[arg-type]
    object_names = {
        f"{job_id}-file-{index}.pdf"
        for job_id in ("job-1", "job-2")
        for index in range(1, 3)
    }
    releases = {name: asyncio.Event() for name in object_names}
    three_started = asyncio.Event()
    fourth_started = asyncio.Event()
    started: list[str] = []
    active = 0
    peak = 0

    async def context(
        job_id: str,
        owner: str,
        generation: int,
        object_name: str,
        cache: dict[str, ObjectContext],
    ) -> ObjectContext:
        del job_id, owner, generation, cache
        return _object_context(object_name)

    async def execute(**kwargs: object) -> tuple[str, bool]:
        nonlocal active, peak
        step = kwargs["step"]
        assert isinstance(step, dict)
        object_name = str(step["object_name"])
        active += 1
        peak = max(peak, active)
        started.append(object_name)
        if len(started) == 3:
            three_started.set()
        if len(started) == 4:
            fourth_started.set()
        try:
            await releases[object_name].wait()
        finally:
            active -= 1
        return f"run-{object_name}", False

    engine._context = AsyncMock(side_effect=context)
    engine._execute = AsyncMock(side_effect=execute)

    with patch(
        "app.rag.pipeline_engine.asyncio.to_thread",
        side_effect=_inline_to_thread,
    ):
        job_tasks = [
            asyncio.create_task(engine.process_job(job_id, "worker-1", 1))
            for job_id in ("job-1", "job-2")
        ]
        try:
            await asyncio.wait_for(three_started.wait(), timeout=1)
            await asyncio.sleep(0)
            assert len(started) == 3
            assert not fourth_started.is_set()

            releases[started[0]].set()
            await asyncio.wait_for(fourth_started.wait(), timeout=1)
            assert peak == 3

            for release in releases.values():
                release.set()
            assert await asyncio.wait_for(
                asyncio.gather(*job_tasks), timeout=1
            ) == ["SUCCEEDED", "SUCCEEDED"]
        finally:
            for release in releases.values():
                release.set()
            for task in job_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*job_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_step_releases_the_global_file_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIPELINE_MAX_CONCURRENT_FILES", "1")
    monkeypatch.setenv("PIPELINE_MAX_CONCURRENT_FILES_PER_JOB", "2")
    repository = _RuntimeRepository()
    repository.steps = [
        {
            "step_id": "failed-step",
            "object_name": "failed.pdf",
            "stage_kind": "RENDER",
            "component_key": "render",
            "attempt_count": 0,
        },
        {
            "step_id": "next-step",
            "object_name": "next.pdf",
            "stage_kind": "RENDER",
            "component_key": "render",
            "attempt_count": 0,
        },
    ]
    engine = PipelineEngine(repository)  # type: ignore[arg-type]

    async def context(
        job_id: str,
        owner: str,
        generation: int,
        object_name: str,
        cache: dict[str, ObjectContext],
    ) -> ObjectContext:
        del job_id, owner, generation, cache
        return _object_context(object_name)

    async def execute(**kwargs: object) -> tuple[str, bool]:
        step = kwargs["step"]
        assert isinstance(step, dict)
        if step["object_name"] == "failed.pdf":
            raise RuntimeError("render failed")
        return "next-run", False

    engine._context = AsyncMock(side_effect=context)
    engine._execute = AsyncMock(side_effect=execute)

    with patch(
        "app.rag.pipeline_engine.asyncio.to_thread",
        side_effect=_inline_to_thread,
    ):
        status = await asyncio.wait_for(
            engine.process_job("job-1", "worker-1", 1), timeout=1
        )

    assert status == "FAILED"
    assert [call[1] for call in repository.calls if call[0] == "start"] == [
        "failed-step",
        "next-step",
    ]
    call_names = [call[0] for call in repository.calls]
    assert call_names.index("fail") < next(
        index
        for index, call in enumerate(repository.calls)
        if call[0] == "start" and call[1] == "next-step"
    )
    assert any(
        call[0] == "complete" and call[1] == "next-step"
        for call in repository.calls
    )


@pytest.mark.asyncio
async def test_cancelled_job_task_releases_the_global_file_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIPELINE_MAX_CONCURRENT_FILES", "1")
    repository = _MultiJobRuntimeRepository(
        {
            "job-1": [
                {
                    "step_id": "job-1-step",
                    "object_name": "first.pdf",
                    "stage_kind": "RENDER",
                    "component_key": "render",
                    "attempt_count": 0,
                }
            ],
            "job-2": [
                {
                    "step_id": "job-2-step",
                    "object_name": "second.pdf",
                    "stage_kind": "RENDER",
                    "component_key": "render",
                    "attempt_count": 0,
                }
            ],
        }
    )
    engine = PipelineEngine(repository)  # type: ignore[arg-type]
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def context(
        job_id: str,
        owner: str,
        generation: int,
        object_name: str,
        cache: dict[str, ObjectContext],
    ) -> ObjectContext:
        del job_id, owner, generation, cache
        return _object_context(object_name)

    async def execute(**kwargs: object) -> tuple[str, bool]:
        step = kwargs["step"]
        assert isinstance(step, dict)
        if step["object_name"] == "first.pdf":
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return f"run-{step['object_name']}", False

    engine._context = AsyncMock(side_effect=context)
    engine._execute = AsyncMock(side_effect=execute)

    with patch(
        "app.rag.pipeline_engine.asyncio.to_thread",
        side_effect=_inline_to_thread,
    ):
        first_job = asyncio.create_task(
            engine.process_job("job-1", "worker-1", 1)
        )
        await asyncio.wait_for(first_started.wait(), timeout=1)
        first_job.cancel()
        await asyncio.gather(first_job, return_exceptions=True)

        assert await asyncio.wait_for(
            engine.process_job("job-2", "worker-1", 1), timeout=1
        ) == "SUCCEEDED"

    assert second_started.is_set()


@pytest.mark.asyncio
async def test_independent_files_advance_while_a_vlm_step_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIPELINE_MAX_CONCURRENT_FILES_PER_JOB", "2")
    repository = _RuntimeRepository()
    repository.steps = [
        {
            "step_id": "slow-step",
            "object_name": "slow.pdf",
            "stage_kind": "VLM",
            "component_key": "vlm:1",
            "attempt_count": 0,
        },
        {
            "step_id": "fast-step",
            "object_name": "fast.pdf",
            "stage_kind": "VLM",
            "component_key": "vlm:1",
            "attempt_count": 0,
        },
    ]
    engine = PipelineEngine(repository)  # type: ignore[arg-type]
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    fast_completed = asyncio.Event()

    async def context(
        job_id: str,
        owner: str,
        generation: int,
        object_name: str,
        cache: dict[str, ObjectContext],
    ) -> ObjectContext:
        del job_id, owner, generation, cache
        revision = RevisionRecord(
            document_id=f"document-{object_name}",
            revision_id=f"revision-{object_name}",
            content_sha256="a" * 64,
            bucket="documents",
            object_name=object_name,
            file_name=object_name,
            media_type="application/pdf",
            document_type="pdf",
            content_changed=True,
        )
        return ObjectContext(b"pdf", revision, f"release-{object_name}")

    async def execute(**kwargs: object) -> tuple[str, bool]:
        step = kwargs["step"]
        assert isinstance(step, dict)
        if step["object_name"] == "slow.pdf":
            slow_started.set()
            await release_slow.wait()
            return "slow-run", False
        fast_completed.set()
        return "fast-run", False

    engine._context = AsyncMock(side_effect=context)
    engine._execute = AsyncMock(side_effect=execute)

    with patch(
        "app.rag.pipeline_engine.asyncio.to_thread",
        side_effect=_inline_to_thread,
    ):
        job_task = asyncio.create_task(
            engine.process_job("job-1", "worker-1", 1)
        )
        try:
            await asyncio.wait_for(slow_started.wait(), timeout=1)
            await asyncio.wait_for(fast_completed.wait(), timeout=1)
            release_slow.set()
            assert await asyncio.wait_for(job_task, timeout=1) == "SUCCEEDED"
        finally:
            release_slow.set()
            if not job_task.done():
                job_task.cancel()
                await asyncio.gather(job_task, return_exceptions=True)
    assert {call[1] for call in repository.calls if call[0] == "complete"} == {
        "slow-step",
        "fast-step",
    }


@pytest.mark.asyncio
async def test_steps_for_the_same_file_remain_serialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIPELINE_MAX_CONCURRENT_FILES_PER_JOB", "2")
    repository = _RuntimeRepository()
    repository.steps = [
        {
            "step_id": "first-step",
            "object_name": "same.pdf",
            "stage_kind": "VLM",
            "component_key": "vlm:1",
            "attempt_count": 0,
        },
        {
            "step_id": "second-step",
            "object_name": "same.pdf",
            "stage_kind": "VLM",
            "component_key": "vlm:2",
            "attempt_count": 0,
        },
    ]
    engine = PipelineEngine(repository)  # type: ignore[arg-type]
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    revision = RevisionRecord(
        document_id="document-1",
        revision_id="revision-1",
        content_sha256="a" * 64,
        bucket="documents",
        object_name="same.pdf",
        file_name="same.pdf",
        media_type="application/pdf",
        document_type="pdf",
        content_changed=True,
    )
    engine._context = AsyncMock(
        return_value=ObjectContext(b"pdf", revision, "release-1")
    )

    async def execute(**kwargs: object) -> tuple[str, bool]:
        step = kwargs["step"]
        assert isinstance(step, dict)
        if step["step_id"] == "first-step":
            first_started.set()
            await release_first.wait()
        return f"run-{step['step_id']}", False

    engine._execute = AsyncMock(side_effect=execute)

    with patch(
        "app.rag.pipeline_engine.asyncio.to_thread",
        side_effect=_inline_to_thread,
    ):
        job_task = asyncio.create_task(
            engine.process_job("job-1", "worker-1", 1)
        )
        try:
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await asyncio.sleep(0)
            assert [
                call[1] for call in repository.calls if call[0] == "start"
            ] == ["first-step"]

            release_first.set()
            assert await asyncio.wait_for(job_task, timeout=1) == "SUCCEEDED"
        finally:
            release_first.set()
            if not job_task.done():
                job_task.cancel()
                await asyncio.gather(job_task, return_exceptions=True)
    assert [call[1] for call in repository.calls if call[0] == "start"] == [
        "first-step",
        "second-step",
    ]


@pytest.mark.asyncio
async def test_context_failure_is_persisted_after_step_is_started() -> None:
    repository = _RuntimeRepository()
    engine = PipelineEngine(repository)  # type: ignore[arg-type]
    engine._context = AsyncMock(side_effect=RuntimeError("revision registration failed"))

    status = await asyncio.wait_for(
        engine.process_job("job-1", "worker-1", 1), timeout=2
    )

    assert status == "FAILED"
    assert [call[0] for call in repository.calls] == [
        "start",
        "fail",
        "heartbeat",
        "finish",
    ]
    assert repository.calls[0][2] == {"owner": "worker-1", "generation": 1}
    assert "revision registration failed" in str(repository.calls[1][2])


@pytest.mark.asyncio
async def test_successful_context_is_attached_before_execution() -> None:
    repository = _RuntimeRepository()
    engine = PipelineEngine(repository)  # type: ignore[arg-type]
    revision = RevisionRecord(
        document_id="document-1",
        revision_id="revision-1",
        content_sha256="a" * 64,
        bucket="documents",
        object_name="catalog.pdf",
        file_name="catalog.pdf",
        media_type="application/pdf",
        document_type="pdf",
        content_changed=True,
    )
    engine._context = AsyncMock(
        return_value=ObjectContext(b"pdf", revision, "release-1")
    )
    engine._execute = AsyncMock(return_value=("run-1", False))

    status = await engine.process_job("job-1", "worker-1", 1)

    assert status == "SUCCEEDED"
    assert [call[0] for call in repository.calls] == [
        "start",
        "attach",
        "complete",
        "heartbeat",
        "finish",
    ]
    assert repository.calls[1][2] == {
        "owner": "worker-1",
        "generation": 1,
        "document_id": "document-1",
        "revision_id": "revision-1",
        "release_id": "release-1",
    }


@pytest.mark.asyncio
async def test_transient_timeout_requeues_step_before_failing_dependents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIPELINE_TRANSIENT_STEP_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("PIPELINE_TRANSIENT_RETRY_DELAY_SECONDS", "0")
    repository = _RuntimeRepository()
    engine = PipelineEngine(repository)  # type: ignore[arg-type]
    revision = RevisionRecord(
        document_id="document-1",
        revision_id="revision-1",
        content_sha256="a" * 64,
        bucket="documents",
        object_name="catalog.pdf",
        file_name="catalog.pdf",
        media_type="application/pdf",
        document_type="pdf",
        content_changed=True,
    )
    engine._context = AsyncMock(
        return_value=ObjectContext(b"pdf", revision, "release-1")
    )
    engine._execute = AsyncMock(
        side_effect=[
            APITimeoutError(request=httpx.Request("POST", "https://example.com")),
            ("run-1", False),
        ]
    )

    status = await engine.process_job("job-1", "worker-1", 1)

    assert status == "SUCCEEDED"
    assert [call[0] for call in repository.calls] == [
        "start",
        "attach",
        "requeue",
        "heartbeat",
        "start",
        "attach",
        "complete",
        "heartbeat",
        "finish",
    ]
    assert not any(call[0] == "fail" for call in repository.calls)
    assert engine._execute.await_count == 2


@pytest.mark.asyncio
async def test_cancel_between_selection_and_start_returns_terminal_status() -> None:
    repository = _RuntimeRepository()
    get_job_calls = 0

    def get_job(job_id: str) -> dict[str, object]:
        nonlocal get_job_calls
        get_job_calls += 1
        if get_job_calls == 1:
            return {
                "job_id": job_id,
                "status": "RUNNING",
                "cancel_requested": False,
                "lease_owner": "worker-1",
                "lease_generation": 1,
            }
        return {
            "job_id": job_id,
            "status": "CANCELLED",
            "cancel_requested": True,
            "lease_owner": None,
            "lease_generation": 1,
        }

    repository.get_job = get_job  # type: ignore[method-assign]
    repository.start_step = MagicMock(  # type: ignore[method-assign]
        side_effect=LeaseLostError("処理Jobのリースが失効しました")
    )
    engine = PipelineEngine(repository)  # type: ignore[arg-type]

    status = await engine.process_job("job-1", "worker-1", 1)

    assert status == "CANCELLED"
    assert not any(call[0] == "fail" for call in repository.calls)
    assert not any(call[0] == "finish" for call in repository.calls)


def test_pipeline_repository_avoids_known_invalid_oracle_bind_names() -> None:
    source = Path(__file__).parents[1] / "app" / "rag" / "pipeline_repository.py"
    bind_names = set(re.findall(r":([A-Za-z][A-Za-z0-9_]*)", source.read_text()))

    assert bind_names.isdisjoint({"file", "size", "raw"})


def test_embedding_input_fingerprint_normalizes_oracle_null_source_ref() -> None:
    assert embedding_input_fingerprint("PAGE_IMAGE", None, "a" * 64) == (
        "PAGE_IMAGE",
        "",
        "a" * 64,
    )


def test_release_validation_error_lists_missing_components() -> None:
    message = release_validation_error_message(
        {
            "missing_components": ["vlm:1", "embedding:vlm_text_slot_1"],
            "stale_components": {},
            "invalid_stage_runs": [],
            "cross_revision_components": [],
            "config_mismatch_components": [],
            "invalid_embeddings": [],
        }
    )

    assert "未実行: vlm:1, embedding:vlm_text_slot_1" in message


def test_retry_publish_rebuilds_current_release_contract() -> None:
    repository = OraclePipelineRepository()
    repository.get_job = MagicMock(  # type: ignore[method-assign]
        return_value={
            "job_id": "job-source",
            "publish_mode": "AUTO",
            "request_json": {"mode": "CUSTOM", "force": True},
            "steps": [
                {
                    "object_name": "photo.jpg",
                    "stage_kind": "RENDER",
                    "component_key": "render",
                    "status": "SUCCEEDED",
                    "depends_on": [],
                },
                {
                    "object_name": "photo.jpg",
                    "stage_kind": "PUBLISH",
                    "component_key": "publish",
                    "status": "FAILED",
                    "depends_on": [],
                },
            ],
        }
    )
    repository.list_recipes = MagicMock(return_value=[])  # type: ignore[method-assign]
    repository.create_job = MagicMock(  # type: ignore[method-assign]
        return_value=("job-recovery", False)
    )

    with (
        patch.object(profile_repository, "enabled_profiles", return_value=[]),
        patch.object(
            retrieval_service_settings,
            "get_mineru",
            return_value=SimpleNamespace(enabled=True, base_url="http://mineru"),
        ),
        patch.object(
            retrieval_service_settings,
            "get_ocr",
            return_value=SimpleNamespace(enabled=False),
        ),
    ):
        result = repository.retry_job("job-source")

    assert result == "job-recovery"
    call = repository.create_job.call_args.kwargs
    specs = call["step_specs"]
    components = [item["component_key"] for item in specs]
    assert components == [
        "render",
        "native_parse",
        "mineru_parse",
        "normalize",
        "publish",
    ]
    assert next(item for item in specs if item["component_key"] == "mineru_parse")[
        "force"
    ] is False
    assert next(item for item in specs if item["component_key"] == "publish")[
        "force"
    ] is True
    assert set(next(item for item in specs if item["component_key"] == "publish")[
        "depends_on"
    ]) == {"render", "native_parse", "mineru_parse", "normalize"}
    request = json.loads(call["request_json"])
    assert request["retry_of_job_id"] == "job-source"
    assert request["recovery_mode"] == "CURRENT_RELEASE_CONTRACT"


def test_requeue_step_preserves_job_counters_and_records_retry_event() -> None:
    repository = OraclePipelineRepository()
    connection_context = MagicMock()
    connection = connection_context.__enter__.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("job-1", "vlm:1", "catalog.pdf")
    cursor.rowcount = 1

    with (
        patch.object(repository, "connection", return_value=connection_context),
        patch.object(repository, "_append_event_cursor") as append_event,
    ):
        repository.requeue_step(
            "step-1",
            "Request timed out.",
            owner="worker-1",
            generation=3,
            attempt=1,
        )

    update_sql, update_params = cursor.execute.call_args_list[1].args
    assert "SET status='QUEUED'" in update_sql
    assert "failed_steps" not in update_sql
    assert update_params["generation"] == 3
    append_event.assert_called_once_with(
        cursor,
        "job-1",
        "step_retrying",
        {
            "object_name": "catalog.pdf",
            "component_key": "vlm:1",
            "attempt": 1,
            "error": "Request timed out.",
        },
    )
    connection.commit.assert_called_once()


def test_start_step_clears_a_previous_transient_error() -> None:
    repository = OraclePipelineRepository()
    connection_context = MagicMock()
    connection = connection_context.__enter__.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.rowcount = 1

    with patch.object(repository, "connection", return_value=connection_context):
        repository.start_step("step-1", owner="worker-1", generation=3)

    update_sql = cursor.execute.call_args.args[0]
    assert "error_summary=NULL" in update_sql
    connection.commit.assert_called_once()


def test_next_step_excludes_files_that_are_already_running_in_the_job() -> None:
    repository = OraclePipelineRepository()
    connection_context = MagicMock()
    connection = connection_context.__enter__.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.rowcount = 0
    cursor.description = []
    cursor.fetchall.return_value = []

    with patch.object(repository, "connection", return_value=connection_context):
        assert repository.next_step(
            "job-1", ["slow.pdf", "another.pdf"]
        ) is None

    select_sql, select_params = cursor.execute.call_args_list[1].args
    assert "child.object_name NOT IN" in select_sql
    assert select_params["excluded_object_0"] == "slow.pdf"
    assert select_params["excluded_object_1"] == "another.pdf"
    assert "slow.pdf" not in select_sql
    assert "another.pdf" not in select_sql


def test_claim_does_not_reclaim_an_unexpired_same_owner_lease() -> None:
    repository = OraclePipelineRepository()
    connection_context = MagicMock()
    cursor = connection_context.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = None

    with patch.object(repository, "connection", return_value=connection_context):
        assert repository.claim_next_job("worker-1") is None

    claim_sql = cursor.execute.call_args_list[0].args[0]
    assert "cancel_requested=0" not in claim_sql
    assert "lease_owner=:owner" not in claim_sql
    assert "lease_until IS NULL" in claim_sql
    assert "lease_until<CAST(SYSTIMESTAMP AS TIMESTAMP)" in claim_sql


def test_claim_rejects_a_stale_snapshot_with_compare_and_set() -> None:
    repository = OraclePipelineRepository()
    connection_context = MagicMock()
    connection = connection_context.__enter__.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("job-1", 4)
    cursor.rowcount = 0

    with patch.object(repository, "connection", return_value=connection_context):
        assert repository.claim_next_job("worker-2") is None

    update_sql, update_params = cursor.execute.call_args_list[1].args
    assert "lease_generation=:expected_generation" in update_sql
    assert "lease_until IS NULL" in update_sql
    assert "lease_until<CAST(SYSTIMESTAMP AS TIMESTAMP)" in update_sql
    assert update_params["expected_generation"] == 4
    assert update_params["generation"] == 5
    assert not any(
        "UPDATE sds_pipeline_job_steps" in call.args[0]
        for call in cursor.execute.call_args_list
    )
    connection.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_forced_vlm_stage_bypasses_an_existing_cached_run() -> None:
    repository = MagicMock()
    repository.component_hash.side_effect = (
        lambda _release, component: f"{component}-hash"
    )
    repository.cached_stage_run.return_value = "cached-run"
    repository.start_stage_run.return_value = "new-run"
    repository.heartbeat.return_value = True
    repository.stage_output_hash.return_value = "output-hash"
    engine = PipelineEngine(repository)
    engine._run_executor = AsyncMock(
        return_value=(1, 1.0, {"profile_slot": 1, "profile_revision_id": "profile-v1"})
    )

    with (
        patch(
            "app.rag.pipeline_engine.stage_config_hash",
            return_value="config-hash",
        ),
        patch("app.rag.pipeline_engine.profile_repository.set_apply_status"),
        patch("app.rag.pipeline_engine.profile_repository.refresh_apply_status"),
        patch(
            "app.rag.pipeline_engine.asyncio.to_thread",
            side_effect=_inline_to_thread,
        ),
    ):
        result = await engine._execute(
            step={
                "stage_kind": "VLM",
                "component_key": "vlm:1",
                "force_run": 1,
            },
            context=_object_context("catalog.pdf"),
            job_id="job-1",
            owner="worker-1",
            generation=1,
            lease_lost=asyncio.Event(),
        )

    assert result == ("new-run", False)
    repository.cached_stage_run.assert_not_called()
    repository.start_stage_run.assert_called_once()
    repository.complete_stage_run.assert_called_once()
    repository.replace_component.assert_called_once()
    engine._run_executor.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_out_of_scope_vlm_output_is_stored_as_a_successful_artifact() -> None:
    profile = ProfileConfig(
        slot_no=1,
        name="Construction photos",
        enabled=True,
        extraction_prompt="Return an empty result for floor plans.",
        current_revision_id="profile-v1",
        config_hash="a" * 64,
    )
    page = {
        "artifact_id": "page-1",
        "artifact_kind": "PAGE_TEXT",
        "page_number": 1,
        "raw_text": "1階平面図",
    }
    repository = MagicMock()
    repository.component_artifacts.side_effect = (
        lambda _release, component, _kind=None: [page]
        if component == "normalize"
        else []
    )
    generate = AsyncMock(
        return_value={"summary": "", "keywords": [], "facts": []}
    )

    with (
        patch(
            "app.rag.pipeline_engine.profile_repository.get_profile",
            return_value=profile,
        ),
        patch(
            "app.rag.pipeline_engine.vlm_client.generate_json",
            new=generate,
        ),
        patch(
            "app.rag.pipeline_engine.asyncio.to_thread",
            side_effect=_inline_to_thread,
        ),
    ):
        count, coverage, metadata = await PipelineEngine(repository)._vlm(
            "run-1",
            _object_context("floor-plan.pdf"),
            1,
        )

    artifacts = repository.store_artifacts.call_args.args[2]
    assert count == 1
    assert coverage == 1.0
    assert metadata == {
        "profile_slot": 1,
        "profile_revision_id": "profile-v1",
        "empty_page_count": 1,
        "nonempty_page_count": 0,
    }
    assert len(artifacts) == 1
    assert artifacts[0].raw_text == ""
    assert artifacts[0].payload == {
        "summary": "",
        "keywords": [],
        "facts": [],
        "room_area_estimates": [],
    }
    assert artifacts[0].metadata["empty_output"] is True
    assert artifacts[0].metadata["original_file_name"] == "floor-plan.pdf"
    assert '"original_file_name": "floor-plan.pdf"' in generate.await_args.kwargs["prompt"]
    generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_required_empty_text_is_skipped_without_embedding_call() -> None:
    recipe = EmbeddingRecipe(
        recipe_id="vlm_text_slot_1",
        code="vlm_text_slot_1",
        name="VLM text",
        enabled=True,
        search_weight=1,
        target_scope="PAGE",
        inputs=[
            EmbeddingRecipeInput(
                source_type="VLM_TEXT", source_ref="1", required=True
            )
        ],
        current_revision_id="vlm_text_slot_1_v1",
        revision_no=1,
        config_hash="a" * 64,
    )
    empty_vlm = {
        "artifact_id": "vlm-1",
        "artifact_kind": "VLM_TEXT",
        "page_number": 1,
        "raw_text": "",
        "content_sha256": "b" * 64,
    }
    page_image = {
        "artifact_id": "image-1",
        "artifact_kind": "PAGE_IMAGE",
        "page_number": 1,
        "raw_text": "",
        "content_sha256": "c" * 64,
    }
    repository = MagicMock()
    repository.get_recipe.return_value = recipe
    repository.component_artifacts.side_effect = (
        lambda _release, component, _kind=None: (
            [empty_vlm] if component == "vlm:1" else [page_image]
        )
    )
    revision = RevisionRecord(
        document_id="document-1",
        revision_id="revision-1",
        content_sha256="d" * 64,
        bucket="documents",
        object_name="catalog.pdf",
        file_name="catalog.pdf",
        media_type="application/pdf",
        document_type="pdf",
        content_changed=True,
    )
    context = ObjectContext(b"pdf", revision, "release-1")
    embed = AsyncMock()

    with patch("app.rag.pipeline_engine.embedding_client.contents", new=embed):
        count, coverage, metadata = await PipelineEngine(repository)._embed(
            "run-1", context, recipe.code
        )

    assert count == 0
    assert coverage == 0
    assert metadata["skipped"] == 1
    embed.assert_not_awaited()
    assert repository.store_embeddings.call_args.kwargs["values"] == []


async def test_optional_inputs_all_missing_skip_without_embedding_call() -> None:
    recipe = EmbeddingRecipe(
        recipe_id="vlm_text_slot_2",
        code="vlm_text_slot_2",
        name="VLM text (optional)",
        enabled=True,
        search_weight=1,
        target_scope="PAGE",
        inputs=[
            EmbeddingRecipeInput(
                source_type="VLM_TEXT", source_ref="2", required=False
            )
        ],
        current_revision_id="vlm_text_slot_2_v1",
        revision_no=1,
        config_hash="a" * 64,
    )
    page_image = {
        "artifact_id": "image-1",
        "artifact_kind": "PAGE_IMAGE",
        "page_number": 1,
        "raw_text": "",
        "content_sha256": "c" * 64,
    }
    repository = MagicMock()
    repository.get_recipe.return_value = recipe
    repository.component_artifacts.side_effect = (
        lambda _release, component, _kind=None: (
            [] if component == "vlm:2" else [page_image]
        )
    )
    revision = RevisionRecord(
        document_id="document-1",
        revision_id="revision-1",
        content_sha256="d" * 64,
        bucket="documents",
        object_name="catalog.pdf",
        file_name="catalog.pdf",
        media_type="application/pdf",
        document_type="pdf",
        content_changed=True,
    )
    context = ObjectContext(b"pdf", revision, "release-1")
    embed = AsyncMock()

    with patch("app.rag.pipeline_engine.embedding_client.contents", new=embed):
        count, coverage, metadata = await PipelineEngine(repository)._embed(
            "run-1", context, recipe.code
        )

    assert count == 0
    assert coverage == 0
    assert metadata["skipped"] == 1
    embed.assert_not_awaited()
    assert repository.store_embeddings.call_args.kwargs["values"] == []
