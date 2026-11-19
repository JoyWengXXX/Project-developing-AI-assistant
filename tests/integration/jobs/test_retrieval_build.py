"""檢索索引與工作完成的交易邊界。"""

from collections.abc import Callable
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from repopilot.api.job_responses import to_job_response
from repopilot.config import Settings
from repopilot.db.locks import try_advisory_lock
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.errors import ImportCancelledError, JobLeaseLostError
from repopilot.services import jobs
from repopilot.services.retrieval_build import (
    BuildProgress,
    RetrievalSummary,
    SliceExhausted,
    build_retrieval_index,
)
from repopilot.services.retrieval_chunks import build_chunk_set
from repopilot.services.retrieval_vectors import (
    VectorSegment,
    claim_index,
    copy_matching_vectors,
    fail_index,
    retrieval_lock_key,
    write_segments,
)
from tests.integration import test_embedding_storage
from tests.integration.jobs.conftest import running_job
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files

vector_chunk_set = test_embedding_storage.chunk_set


def test_index_and_job_publish_in_same_transaction(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    vector_chunk_set: tuple[int, int, int],
) -> None:
    snapshot_id, set_id, _ = vector_chunk_set
    job, token = running_job(session, job_settings)
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)

    def finalize(summary: RetrievalSummary, publish: Callable[[Session], None]) -> bool:
        done = jobs.complete_job(
            session,
            job.id,
            token,
            result={"index_id": summary.index_id},
            snapshot_id=snapshot_id,
            before_publish=publish,
        )
        return done.status == "succeeded"

    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        summary = build_retrieval_index(
            session,
            chunk_set_id=set_id,
            owner_token=token,
            profile=profile,
            lock=lock,
            count_tokens=lambda value: len(value) + 1,
            embed=lambda value: [1.0] + [0.0] * 767,
            checkpoint=lambda: None,
            finalize=finalize,
        )
        reused = build_retrieval_index(
            session,
            chunk_set_id=set_id,
            owner_token=uuid4(),
            profile=profile,
            lock=lock,
            count_tokens=lambda value: pytest.fail("completed index must not tokenize"),
            embed=lambda value: pytest.fail("completed index must not call the model"),
            checkpoint=lambda: None,
            finalize=lambda summary, publish: pytest.fail("reused index must not be published"),
        )
        assert reused.reused
        assert reused.index_id == summary.index_id
        assert reused.segment_count == summary.segment_count
    with db_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT status FROM retrieval_indexes WHERE id=:id"), {"id": summary.index_id}
            )
            == "completed"
        )
        assert connection.scalar(text("SELECT status FROM jobs WHERE id=:id"), {"id": job.id}) == (
            "succeeded"
        )


@pytest.mark.parametrize("outcome", ["cancelled", "error"])
def test_cancel_or_publish_error_never_exposes_completed_index(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    vector_chunk_set: tuple[int, int, int],
    outcome: str,
) -> None:
    snapshot_id, set_id, _ = vector_chunk_set
    job, token = running_job(session, job_settings)
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)

    def finalize(summary: RetrievalSummary, publish: Callable[[Session], None]) -> bool:
        if outcome == "cancelled":
            jobs.cancel_job(session, job.id)
            before_publish = publish
        else:

            def before_publish(transaction: Session) -> None:
                publish(transaction)
                raise RuntimeError("injected failure")

        done = jobs.complete_job(
            session,
            job.id,
            token,
            result={"index_id": summary.index_id},
            snapshot_id=snapshot_id,
            before_publish=before_publish,
        )
        return done.status == "succeeded"

    expected_error = ImportCancelledError if outcome == "cancelled" else RuntimeError
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with pytest.raises(expected_error):
            build_retrieval_index(
                session,
                chunk_set_id=set_id,
                owner_token=token,
                profile=profile,
                lock=lock,
                count_tokens=lambda value: len(value) + 1,
                embed=lambda value: [1.0] + [0.0] * 767,
                checkpoint=lambda: None,
                finalize=finalize,
            )
    with db_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT status FROM retrieval_indexes WHERE chunk_set_id=:id"), {"id": set_id}
            )
            == "failed"
        )
        assert connection.scalar(text("SELECT status FROM jobs WHERE id=:id"), {"id": job.id}) == (
            "cancelled" if outcome == "cancelled" else "running"
        )


def test_stale_builder_does_not_overwrite_new_owner(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    vector_chunk_set: tuple[int, int, int],
) -> None:
    _, set_id, _ = vector_chunk_set
    _, token = running_job(session, job_settings)
    new_owner = uuid4()
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)

    def replace_owner(_: str) -> list[float]:
        with db_engine.begin() as connection:
            connection.execute(
                text("UPDATE retrieval_indexes SET owner_token=:owner WHERE chunk_set_id=:id"),
                {"owner": new_owner, "id": set_id},
            )
        return [1.0] + [0.0] * 767

    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with pytest.raises(JobLeaseLostError):
            build_retrieval_index(
                session,
                chunk_set_id=set_id,
                owner_token=token,
                profile=profile,
                lock=lock,
                count_tokens=lambda value: len(value) + 1,
                embed=replace_owner,
                checkpoint=lambda: None,
                finalize=lambda summary, publish: True,
            )
    with db_engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, owner_token FROM retrieval_indexes WHERE chunk_set_id=:id"),
            {"id": set_id},
        ).one()
        assert row == ("building", new_owner)
        assert connection.scalar(text("SELECT count(*) FROM retrieval_vectors")) == 0


def test_finalizer_must_publish_before_reporting_success(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    vector_chunk_set: tuple[int, int, int],
) -> None:
    _, set_id, _ = vector_chunk_set
    _, token = running_job(session, job_settings)
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with pytest.raises(RuntimeError, match="finalizer did not publish"):
            build_retrieval_index(
                session,
                chunk_set_id=set_id,
                owner_token=token,
                profile=profile,
                lock=lock,
                count_tokens=lambda value: len(value) + 1,
                embed=lambda value: [1.0] + [0.0] * 767,
                checkpoint=lambda: None,
                finalize=lambda summary, publish: True,
            )
    with db_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT status FROM retrieval_indexes WHERE chunk_set_id=:id"), {"id": set_id}
            )
            == "failed"
        )


@pytest.fixture
def three_chunk_set(db_engine: Engine, clean_repositories: None) -> tuple[int, int]:
    with db_engine.begin() as connection:
        repository_id = connection.scalar(
            text(
                "INSERT INTO repositories (name, canonical_local_path) "
                "VALUES ('progress-test', '/progress-test') RETURNING id"
            )
        )
    assert isinstance(repository_id, int)
    snapshot_id = add_snapshot(db_engine, repository_id)
    add_files(
        db_engine, snapshot_id, {"a.txt": "alpha text", "b.txt": "beta text!", "c.txt": "gamma"}
    )
    with Session(db_engine) as setup, setup.begin():
        result = build_chunk_set(setup, snapshot_id, max_bytes=24)
    return snapshot_id, result.chunk_set_id


def _build_with_progress(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    set_ids: tuple[int, int],
    interval: float,
) -> tuple[RetrievalSummary, list[BuildProgress]]:
    snapshot_id, set_id = set_ids
    job, token = running_job(session, job_settings)
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    reports: list[BuildProgress] = []

    def finalize(summary: RetrievalSummary, publish: Callable[[Session], None]) -> bool:
        done = jobs.complete_job(
            session, job.id, token, result={}, snapshot_id=snapshot_id, before_publish=publish
        )
        return done.status == "succeeded"

    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        summary = build_retrieval_index(
            session,
            chunk_set_id=set_id,
            owner_token=token,
            profile=profile,
            lock=lock,
            count_tokens=lambda value: len(value) + 1,
            embed=lambda value: [1.0] + [0.0] * 767,
            checkpoint=lambda: None,
            finalize=finalize,
            on_progress=reports.append,
            progress_interval_seconds=interval,
        )
    return summary, reports


def test_progress_starts_at_zero_and_ends_with_every_chunk_done(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    three_chunk_set: tuple[int, int],
) -> None:
    summary, reports = _build_with_progress(db_engine, session, job_settings, three_chunk_set, 0.0)

    assert [report.chunks_done for report in reports] == [0, 1, 2, 3]
    assert all(report.chunks_total == 3 and report.chars_total == 25 for report in reports)
    assert reports[0].vectors == 0 and reports[0].chars_done == 0
    assert reports[-1].chars_done == 25
    assert reports[-1].vectors == summary.segment_count
    assert [report.vectors for report in reports] == sorted(report.vectors for report in reports)
    assert [report.run_seconds for report in reports] == sorted(
        report.run_seconds for report in reports
    )


def test_progress_is_throttled_but_start_and_final_chunk_are_always_reported(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    three_chunk_set: tuple[int, int],
) -> None:
    _, reports = _build_with_progress(db_engine, session, job_settings, three_chunk_set, 3600.0)

    assert [report.chunks_done for report in reports] == [0, 3]


def test_job_response_exposes_vectors_and_an_estimate_only_while_running(
    session: Session, job_settings: Settings
) -> None:
    created = jobs.create_job(
        session, kind="snapshot_retrieval", dedupe_key="retrieval:progress", settings=job_settings
    )
    leased = jobs.acquire_lease(session, created.job.id, job_settings)
    assert leased is not None and leased.lease_token is not None
    updated = jobs.update_progress(
        session,
        leased.id,
        leased.lease_token,
        phase="embedding",
        processed_files=1,
        total_files=4,
        work_state={
            "embedding": {
                "vectors": 7,
                "chars_done": 100,
                "chars_total": 400,
                "run_base_chars": 0,
                "run_seconds": 50.0,
            }
        },
    )

    progress = to_job_response(updated).progress
    assert (progress.processed_files, progress.total_files) == (1, 4)
    assert progress.vectors_written == 7
    assert progress.estimated_remaining_seconds == 150

    failed = jobs.fail_job(
        session,
        leased.id,
        leased.lease_token,
        job_settings,
        error_code="job_time_limit_exceeded",
        error_summary="x",
    )
    finished = to_job_response(failed).progress
    assert finished.vectors_written is None
    assert finished.estimated_remaining_seconds is None


def _unit_vector_for(value: str) -> list[float]:
    vector = [0.0] * 768
    vector[sum(map(ord, value)) % 768] = 1.0
    return vector


def _chunks(db_engine: Engine, set_id: int) -> list[tuple[int, int]]:
    with db_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, end_offset - start_offset FROM retrieval_chunks "
                "WHERE chunk_set_id = :id ORDER BY id"
            ),
            {"id": set_id},
        ).all()
    return [(row[0], row[1]) for row in rows]


def _vector_rows(db_engine: Engine, set_id: int) -> list[tuple[int, int, int, int]]:
    with db_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT chunk_id, ordinal, start_offset, end_offset FROM retrieval_vectors "
                "WHERE chunk_set_id = :id ORDER BY chunk_id, ordinal"
            ),
            {"id": set_id},
        ).all()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


PROFILE = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)

# 第二個片段（10 字元）的各種「不完整」向量排列：(ordinal, start, end)
INCOMPLETE_LAYOUTS = {
    "prefix only": [(0, 0, 4)],
    "gap": [(0, 0, 3), (1, 5, 10)],
    "overlap": [(0, 0, 6), (1, 4, 10)],
    "does not start at zero": [(0, 2, 10)],
    "ordinal skipped": [(0, 0, 4), (2, 4, 10)],
}


@pytest.mark.parametrize("layout", list(INCOMPLETE_LAYOUTS))
def test_takeover_keeps_fully_covered_chunks_and_clears_incomplete_ones(
    db_engine: Engine, three_chunk_set: tuple[int, int], layout: str
) -> None:
    _, set_id = three_chunk_set
    (first, first_length), (second, _), _ = _chunks(db_engine, set_id)
    owner = uuid4()
    vector = [1.0] + [0.0] * 767
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, PROFILE)) as lock:
        assert lock is not None
        with Session(db_engine) as session, session.begin():
            index = claim_index(
                session, chunk_set_id=set_id, profile=PROFILE, owner_token=owner, lock=lock
            )
            write_segments(
                session,
                index_id=index.index_id,
                owner_token=owner,
                segments=[
                    VectorSegment(first, 0, 0, 6, 2, vector),
                    VectorSegment(first, 1, 6, first_length, 2, vector),
                    *[
                        VectorSegment(second, ordinal, start, end, 2, vector)
                        for ordinal, start, end in INCOMPLETE_LAYOUTS[layout]
                    ],
                ],
            )
            fail_index(session, index_id=index.index_id, owner_token=owner)
        with Session(db_engine) as session, session.begin():
            retry = claim_index(
                session, chunk_set_id=set_id, profile=PROFILE, owner_token=uuid4(), lock=lock
            )
            segment_count = session.scalar(
                text("SELECT segment_count FROM retrieval_indexes WHERE id = :id"),
                {"id": retry.index_id},
            )

    assert retry.index_id == index.index_id and not retry.reused
    assert retry.covered_chunk_ids == {first}
    assert (retry.covered_chars, retry.covered_vectors, segment_count) == (first_length, 2, 2)
    assert _vector_rows(db_engine, set_id) == [(first, 0, 0, 6), (first, 1, 6, first_length)]


def test_a_different_profile_never_resumes_another_profiles_vectors(
    db_engine: Engine, three_chunk_set: tuple[int, int]
) -> None:
    _, set_id = three_chunk_set
    (first, first_length), *_ = _chunks(db_engine, set_id)
    other = EmbeddingProfile(model_sha256="c" * 64, runtime_bundle_sha256="b" * 64)
    owner = uuid4()
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, PROFILE)) as lock:
        assert lock is not None
        with Session(db_engine) as session, session.begin():
            index = claim_index(
                session, chunk_set_id=set_id, profile=PROFILE, owner_token=owner, lock=lock
            )
            write_segments(
                session,
                index_id=index.index_id,
                owner_token=owner,
                segments=[VectorSegment(first, 0, 0, first_length, 2, [1.0] + [0.0] * 767)],
            )
            fail_index(session, index_id=index.index_id, owner_token=owner)
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, other)) as lock:
        assert lock is not None
        with Session(db_engine) as session, session.begin():
            claimed = claim_index(
                session, chunk_set_id=set_id, profile=other, owner_token=uuid4(), lock=lock
            )

    assert claimed.index_id != index.index_id
    assert claimed.covered_chunk_ids == frozenset() and claimed.covered_vectors == 0


def _run_build(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    set_ids: tuple[int, int],
    *,
    key: str,
    embed: Callable[[str], list[float]],
    time_slice_seconds: float | None = None,
    profile: EmbeddingProfile = PROFILE,
) -> tuple[RetrievalSummary, list[BuildProgress]]:
    snapshot_id, set_id = set_ids
    job, token = running_job(session, job_settings, key=key)
    reports: list[BuildProgress] = []

    def finalize(summary: RetrievalSummary, publish: Callable[[Session], None]) -> bool:
        done = jobs.complete_job(
            session, job.id, token, result={}, snapshot_id=snapshot_id, before_publish=publish
        )
        return done.status == "succeeded"

    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        summary = build_retrieval_index(
            session,
            chunk_set_id=set_id,
            owner_token=token,
            profile=profile,
            lock=lock,
            count_tokens=lambda value: len(value) + 1,
            embed=embed,
            checkpoint=lambda: None,
            finalize=finalize,
            on_progress=reports.append,
            progress_interval_seconds=0.0,
            time_slice_seconds=time_slice_seconds,
        )
    return summary, reports


def _new_repository(db_engine: Engine, name: str) -> int:
    with db_engine.begin() as connection:
        repository_id = connection.scalar(
            text(
                "INSERT INTO repositories (name, canonical_local_path) "
                "VALUES (:name, :path) RETURNING id"
            ),
            {"name": name, "path": f"/reuse-{name}"},
        )
    assert isinstance(repository_id, int)
    return repository_id


def _snapshot_with_chunks(
    db_engine: Engine, repository_id: int, index: int, files: dict[str, str]
) -> tuple[int, int]:
    snapshot_id = add_snapshot(db_engine, repository_id, index=index)
    add_files(db_engine, snapshot_id, files)
    with Session(db_engine) as setup, setup.begin():
        return snapshot_id, build_chunk_set(setup, snapshot_id, max_bytes=24).chunk_set_id


_CHUNK_TEXT = "substr(f.content, c.start_offset + 1, c.end_offset - c.start_offset)"
_VECTOR_JOIN = (
    "FROM retrieval_vectors v "
    "JOIN retrieval_chunks c ON c.id = v.chunk_id "
    "JOIN snapshot_files f ON f.id = c.snapshot_file_id "
)


def _vectors_by_text(db_engine: Engine, set_id: int) -> dict[str, list[tuple[int, int, int]]]:
    """每個片段（以原文為鍵）的 (序號, 起點, 終點)；同時核對 token 數。"""
    result: dict[str, list[tuple[int, int, int]]] = {}
    with db_engine.connect() as connection:
        for value, ordinal, start, end, tokens in connection.execute(
            text(
                f"SELECT {_CHUNK_TEXT}, v.ordinal, v.start_offset, v.end_offset, v.token_count "
                f"{_VECTOR_JOIN} WHERE v.chunk_set_id = :id ORDER BY 1, 2"
            ),
            {"id": set_id},
        ):
            result.setdefault(value, []).append((ordinal, start, end))
            assert tokens == len(value) + 1
    return result


def _assert_vectors_equal_the_model_output(db_engine: Engine, set_id: int) -> None:
    """每個向量都與假模型對該片段原文的輸出完全相同（距離 0）。"""
    with db_engine.connect() as connection:
        values = connection.scalars(
            text(f"SELECT DISTINCT {_CHUNK_TEXT} {_VECTOR_JOIN} WHERE v.chunk_set_id = :id"),
            {"id": set_id},
        ).all()
        assert values
        for value in values:
            expected = "[" + ",".join(str(x) for x in _unit_vector_for(value)) + "]"
            mismatches = connection.scalar(
                text(
                    f"SELECT count(*) {_VECTOR_JOIN} WHERE v.chunk_set_id = :id "
                    f"AND {_CHUNK_TEXT} = :v AND v.embedding <-> CAST(:expected AS vector) > 0"
                ),
                {"id": set_id, "v": value, "expected": expected},
            )
            assert mismatches == 0


@pytest.mark.parametrize("same_repository", [True, False])
def test_identical_chunks_reuse_vectors_across_snapshots_and_repositories(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    clean_repositories: None,
    same_repository: bool,
) -> None:
    first_repo = _new_repository(db_engine, "reuse-a")
    second_repo = first_repo if same_repository else _new_repository(db_engine, "reuse-b")
    first = _snapshot_with_chunks(
        db_engine, first_repo, 1, {"a.txt": "alpha text", "b.txt": "beta text!", "c.txt": "gamma"}
    )
    second = _snapshot_with_chunks(
        db_engine,
        second_repo,
        2 if same_repository else 1,
        {"x/a.txt": "alpha text", "x/b.txt": "beta text!", "d.txt": "delta"},
    )
    _run_build(
        db_engine, session, job_settings, first, key="first", embed=lambda v: _unit_vector_for(v)
    )
    embedded: list[str] = []

    def counting_embed(value: str) -> list[float]:
        embedded.append(value)
        return _unit_vector_for(value)

    summary, reports = _run_build(
        db_engine, session, job_settings, second, key="second", embed=counting_embed
    )

    assert embedded == ["delta"]  # 內容相同的兩個片段沒有呼叫模型
    assert summary.reused_chunks == 2 and summary.segment_count == 3
    assert reports[-1].reused_chunks == 2 and reports[-1].chunks_done == 3
    # 估計速率只看實際呼叫模型的字元：重用的片段（10＋10 字元）算在 base_chars
    assert reports[-1].chars_done - reports[-1].base_chars == len("delta")
    _assert_vectors_equal_the_model_output(db_engine, second[1])
    assert _vectors_by_text(db_engine, second[1]) == {
        "alpha text": [(0, 0, 10)],
        "beta text!": [(0, 0, 10)],
        "delta": [(0, 0, 5)],
    }
    with db_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT status FROM retrieval_indexes WHERE chunk_set_id = :id"),
                {"id": second[1]},
            )
            == "completed"
        )


def test_vectors_are_not_reused_across_embedding_profiles(
    db_engine: Engine, session: Session, job_settings: Settings, clean_repositories: None
) -> None:
    repository_id = _new_repository(db_engine, "profiles")
    first = _snapshot_with_chunks(db_engine, repository_id, 1, {"a.txt": "alpha text"})
    second = _snapshot_with_chunks(db_engine, repository_id, 2, {"a.txt": "alpha text"})
    other = EmbeddingProfile(model_sha256="c" * 64, runtime_bundle_sha256="b" * 64)
    _run_build(db_engine, session, job_settings, first, key="first", embed=_unit_vector_for)
    embedded: list[str] = []

    def counting_embed(value: str) -> list[float]:
        embedded.append(value)
        return _unit_vector_for(value)

    summary, _ = _run_build(
        db_engine,
        session,
        job_settings,
        second,
        key="second",
        embed=counting_embed,
        profile=other,
    )

    assert embedded == ["alpha text"] and summary.reused_chunks == 0


def test_a_failed_or_unfinished_index_is_never_a_reuse_source(
    db_engine: Engine, session: Session, job_settings: Settings, clean_repositories: None
) -> None:
    repository_id = _new_repository(db_engine, "unfinished")
    first = _snapshot_with_chunks(
        db_engine, repository_id, 1, {"a.txt": "alpha text", "b.txt": "boom"}
    )
    second = _snapshot_with_chunks(db_engine, repository_id, 2, {"a.txt": "alpha text"})

    def failing_embed(value: str) -> list[float]:
        if value == "boom":
            raise RuntimeError("injected")
        return _unit_vector_for(value)

    with pytest.raises(RuntimeError):
        _run_build(db_engine, session, job_settings, first, key="first", embed=failing_embed)
    # 第一個索引停在 failed，且已有 "alpha text" 的完整向量，但它不是已完成的索引
    embedded: list[str] = []

    def counting_embed(value: str) -> list[float]:
        embedded.append(value)
        return _unit_vector_for(value)

    summary, _ = _run_build(
        db_engine, session, job_settings, second, key="second", embed=counting_embed
    )

    assert embedded == ["alpha text"] and summary.reused_chunks == 0


def test_copied_vectors_survive_removing_the_source_snapshot(
    db_engine: Engine, session: Session, job_settings: Settings, clean_repositories: None
) -> None:
    repository_id = _new_repository(db_engine, "removal")
    first = _snapshot_with_chunks(db_engine, repository_id, 1, {"a.txt": "alpha text"})
    second = _snapshot_with_chunks(db_engine, repository_id, 2, {"a.txt": "alpha text"})
    _run_build(db_engine, session, job_settings, first, key="first", embed=_unit_vector_for)
    summary, _ = _run_build(
        db_engine, session, job_settings, second, key="second", embed=_unit_vector_for
    )
    assert summary.reused_chunks == 1

    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM snapshots WHERE id = :id"), {"id": first[0]})

    assert _vectors_by_text(db_engine, second[1]) == {"alpha text": [(0, 0, 10)]}
    with db_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT status FROM retrieval_indexes WHERE chunk_set_id = :id"),
                {"id": second[1]},
            )
            == "completed"
        )


def test_reuse_requires_the_caller_to_still_own_the_index(
    db_engine: Engine, three_chunk_set: tuple[int, int]
) -> None:
    _, set_id = three_chunk_set
    (chunk_id, _), *_ = _chunks(db_engine, set_id)
    owner = uuid4()
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, PROFILE)) as lock:
        assert lock is not None
        with Session(db_engine) as session, session.begin():
            index = claim_index(
                session, chunk_set_id=set_id, profile=PROFILE, owner_token=owner, lock=lock
            )
        with Session(db_engine) as session, pytest.raises(JobLeaseLostError):
            copy_matching_vectors(
                session,
                index_id=index.index_id,
                owner_token=uuid4(),
                chunk_id=chunk_id,
                profile=PROFILE,
            )


def test_every_slice_finishes_at_least_one_new_chunk_and_the_last_run_publishes(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    three_chunk_set: tuple[int, int],
) -> None:
    _, set_id = three_chunk_set
    embedded: list[str] = []

    def counting_embed(value: str) -> list[float]:
        embedded.append(value)
        return _unit_vector_for(value)

    reports_per_run: list[int] = []
    for run in range(3):
        try:
            summary, reports = _run_build(
                db_engine,
                session,
                job_settings,
                three_chunk_set,
                key=f"slice-{run}",
                embed=counting_embed,
                time_slice_seconds=0.0,
            )
        except SliceExhausted:
            with db_engine.connect() as connection:
                # 切片用完不是失敗：索引維持 building，已完成的片段向量都已提交
                assert (
                    connection.scalar(
                        text("SELECT status FROM retrieval_indexes WHERE chunk_set_id = :id"),
                        {"id": set_id},
                    )
                    == "building"
                )
                assert (
                    connection.scalar(
                        text("SELECT count(DISTINCT chunk_id) FROM retrieval_vectors")
                    )
                    == run + 1
                )
            reports_per_run.append(run)
        else:
            break
    else:
        pytest.fail("the third run must publish the index")

    assert reports_per_run == [0, 1]
    assert len(embedded) == 3 and len(set(embedded)) == 3  # 每個片段只算一次
    assert summary.segment_count == 3 and reports[-1].chunks_done == 3


@pytest.mark.parametrize("failure", [RuntimeError, ImportCancelledError])
def test_retry_after_failure_or_cancel_embeds_only_the_missing_chunks(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    three_chunk_set: tuple[int, int],
    failure: type[Exception],
) -> None:
    _, set_id = three_chunk_set

    def failing_embed(value: str) -> list[float]:
        if value == "gamma":
            raise failure("injected")
        return _unit_vector_for(value)

    with pytest.raises(failure):
        _run_build(
            db_engine, session, job_settings, three_chunk_set, key="first", embed=failing_embed
        )
    assert {row[0] for row in _vector_rows(db_engine, set_id)} == {
        chunk_id for chunk_id, _ in _chunks(db_engine, set_id)[:2]
    }
    with db_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT status FROM retrieval_indexes WHERE chunk_set_id = :id"),
                {"id": set_id},
            )
            == "failed"
        )

    embedded: list[str] = []

    def counting_embed(value: str) -> list[float]:
        embedded.append(value)
        return _unit_vector_for(value)

    summary, reports = _run_build(
        db_engine, session, job_settings, three_chunk_set, key="second", embed=counting_embed
    )

    assert embedded == ["gamma"]
    assert (reports[0].chunks_done, reports[0].chars_done, reports[0].base_chars) == (2, 20, 20)
    assert (reports[-1].chunks_done, reports[-1].vectors) == (3, 3)
    assert summary.segment_count == 3
    texts = dict(
        zip(_chunks(db_engine, set_id), ["alpha text", "beta text!", "gamma"], strict=True)
    )
    with db_engine.connect() as connection:
        for (chunk_id, _), value in texts.items():
            distance = connection.scalar(
                text(
                    "SELECT embedding <-> CAST(:expected AS vector) FROM retrieval_vectors "
                    "WHERE chunk_id = :id"
                ),
                {
                    "expected": "[" + ",".join(str(x) for x in _unit_vector_for(value)) + "]",
                    "id": chunk_id,
                },
            )
            assert distance == 0
        assert (
            connection.scalar(
                text("SELECT status FROM retrieval_indexes WHERE chunk_set_id = :id"),
                {"id": set_id},
            )
            == "completed"
        )
        assert (
            connection.scalar(
                text("SELECT segment_count FROM retrieval_indexes WHERE chunk_set_id = :id"),
                {"id": set_id},
            )
            == 3
        )
