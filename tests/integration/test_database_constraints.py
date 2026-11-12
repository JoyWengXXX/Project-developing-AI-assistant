"""切片 6：資料庫約束的直接測試（PostgreSQL）。

不經 API，直接以 ORM／SQL 寫入，確認約束由資料庫本身保證（去重與完整性不能只靠應用程式先查再寫，
AGENTS.md 規則 5），並以約束名稱斷言，應用程式的衝突處理依賴這些名稱。
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from repopilot.db.models import Repository, Snapshot, SnapshotFile

HASH_A = "a" * 64
HASH_B = "b" * 64
SHA_1 = "1" * 40
SHA_2 = "2" * 40


@pytest.fixture
def session(db_engine: Engine, clean_repositories: None) -> Iterator[Session]:
    with Session(db_engine) as db_session:
        yield db_session


def _repository(session: Session, path: str = "/repo/a") -> Repository:
    repository = Repository(name="r", canonical_local_path=path)
    session.add(repository)
    session.commit()
    return repository


def _snapshot(
    session: Session,
    repository: Repository,
    *,
    commit_sha: str = SHA_1,
    profile: str = HASH_A,
    status: str = "completed",
) -> Snapshot:
    snapshot = Snapshot(
        repository_id=repository.id,
        commit_sha=commit_sha,
        import_profile_hash=profile,
        status=status,
        started_at=datetime.now(UTC),
    )
    session.add(snapshot)
    session.commit()
    return snapshot


def _violation(session: Session) -> str:
    """提交並回傳 IntegrityError 中的 PostgreSQL 訊息（內含約束名稱）；沒有違反就讓測試失敗。"""
    with pytest.raises(IntegrityError) as exc_info:
        session.commit()
    session.rollback()
    return str(exc_info.value.orig)


# --- snapshots ---


def test_snapshot_triple_is_unique_with_the_constraint_name_the_service_relies_on(
    session: Session,
) -> None:
    repository = _repository(session)
    _snapshot(session, repository)
    session.add(
        Snapshot(
            repository_id=repository.id,
            commit_sha=SHA_1,
            import_profile_hash=HASH_A,
            status="importing",
            started_at=datetime.now(UTC),
        )
    )

    message = _violation(session)

    assert "uq_snapshots_repository_id_commit_sha_import_profile_hash" in message


def test_snapshot_uniqueness_is_per_repository_commit_and_profile(session: Session) -> None:
    first = _repository(session, "/repo/a")
    second = _repository(session, "/repo/b")
    _snapshot(session, first)

    _snapshot(session, first, profile=HASH_B)  # 不同匯入設定
    _snapshot(session, first, commit_sha=SHA_2)  # 不同 commit
    _snapshot(session, second)  # 不同 repo

    assert session.scalar(select(func.count()).select_from(Snapshot)) == 4


def test_snapshot_status_must_be_importing_completed_or_failed(session: Session) -> None:
    repository = _repository(session)
    session.add(
        Snapshot(
            repository_id=repository.id,
            commit_sha=SHA_1,
            import_profile_hash=HASH_A,
            status="done",
            started_at=datetime.now(UTC),
        )
    )

    assert "ck_snapshots_status_valid" in _violation(session)


@pytest.mark.parametrize("column", ["repository_id", "commit_sha", "import_profile_hash", "status"])
def test_snapshot_required_columns_reject_null(session: Session, column: str) -> None:
    repository = _repository(session)
    values: dict[str, object] = {
        "repository_id": repository.id,
        "commit_sha": SHA_1,
        "import_profile_hash": HASH_A,
        "status": "importing",
        "started_at": datetime.now(UTC),
    }
    values[column] = None
    session.add(Snapshot(**values))

    message = _violation(session)

    assert "null value" in message.lower()
    assert column in message


def test_snapshot_must_reference_an_existing_repository(session: Session) -> None:
    session.add(
        Snapshot(
            repository_id=987_654,
            commit_sha=SHA_1,
            import_profile_hash=HASH_A,
            status="importing",
            started_at=datetime.now(UTC),
        )
    )

    assert "fk_snapshots_repository_id_repositories" in _violation(session)


def test_repository_with_snapshots_cannot_be_deleted_directly(session: Session) -> None:
    """snapshots 的外鍵沒有 CASCADE：移除 repo 必須先刪快照（`repository_removal` 的職責）。"""
    repository = _repository(session)
    _snapshot(session, repository)

    with pytest.raises(IntegrityError) as exc_info:
        session.execute(text("DELETE FROM repositories WHERE id = :i"), {"i": repository.id})
        session.commit()
    session.rollback()

    assert "fk_snapshots_repository_id_repositories" in str(exc_info.value.orig)
    assert session.scalar(select(func.count()).select_from(Repository)) == 1


@pytest.mark.parametrize(
    ("column", "value"), [("commit_sha", "x" * 65), ("import_profile_hash", "x" * 65)]
)
def test_snapshot_identifier_columns_reject_values_longer_than_64(
    session: Session, column: str, value: str
) -> None:
    repository = _repository(session)
    values: dict[str, object] = {
        "repository_id": repository.id,
        "commit_sha": SHA_1,
        "import_profile_hash": HASH_A,
        "status": "importing",
        "started_at": datetime.now(UTC),
        column: value,
    }
    session.add(Snapshot(**values))

    with pytest.raises(DataError):
        session.commit()
    session.rollback()


# --- snapshot_files ---


def _file(snapshot: Snapshot, path: str = "a.txt", /, **overrides: object) -> SnapshotFile:
    values: dict[str, object] = {
        "snapshot_id": snapshot.id,
        "path": path,
        "content": "x",
        "status": "imported",
    }
    values.update(overrides)
    return SnapshotFile(**values)


def test_snapshot_file_path_is_unique_within_a_snapshot_only(session: Session) -> None:
    repository = _repository(session)
    first = _snapshot(session, repository)
    second = _snapshot(session, repository, commit_sha=SHA_2)
    session.add(_file(first, "dup.txt"))
    session.commit()

    session.add(_file(second, "dup.txt"))  # 不同快照可以有相同路徑
    session.commit()
    session.add(_file(first, "dup.txt"))
    message = _violation(session)

    assert "uq_snapshot_files_snapshot_id_path" in message


def test_snapshot_file_paths_differing_only_by_case_or_normalization_are_distinct(
    session: Session,
) -> None:
    repository = _repository(session)
    snapshot = _snapshot(session, repository)

    session.add_all([_file(snapshot, name) for name in ("A.txt", "a.txt", "e\u0301", "\u00e9")])
    session.commit()

    assert session.scalar(select(func.count()).select_from(SnapshotFile)) == 4


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"status": "pending", "content": None}, "ck_snapshot_files_status_valid"),
        ({"status": "imported", "content": None}, "ck_snapshot_files_content_matches_status"),
        ({"status": "skipped", "content": "leaked"}, "ck_snapshot_files_content_matches_status"),
    ],
    ids=["unknown-status", "imported-without-content", "skipped-with-content"],
)
def test_snapshot_file_check_constraints(
    session: Session, overrides: dict[str, object], constraint: str
) -> None:
    repository = _repository(session)
    snapshot = _snapshot(session, repository)
    session.add(_file(snapshot, **overrides))

    assert constraint in _violation(session)


def test_snapshot_file_allows_empty_string_content_for_imported_files(session: Session) -> None:
    repository = _repository(session)
    snapshot = _snapshot(session, repository)

    session.add(_file(snapshot, "empty.txt", content=""))
    session.commit()

    stored = session.scalars(select(SnapshotFile)).one()
    assert (stored.status, stored.content) == ("imported", "")


def test_snapshot_file_must_reference_an_existing_snapshot(session: Session) -> None:
    session.add(SnapshotFile(snapshot_id=987_654, path="a.txt", content="x", status="imported"))

    assert "fk_snapshot_files_snapshot_id_snapshots" in _violation(session)


def test_postgresql_text_rejects_nul_so_binary_content_must_never_be_stored(
    session: Session,
) -> None:
    """D25 以「整個內容含 NUL 即 binary」為依據：資料庫拒絕含 NUL 的 text。"""
    repository = _repository(session)
    snapshot = _snapshot(session, repository)
    session.add(_file(snapshot, "nul.txt", content="a\x00b"))

    with pytest.raises(DataError):
        session.commit()
    session.rollback()


def test_deleting_a_snapshot_cascades_to_its_files_only(session: Session) -> None:
    repository = _repository(session)
    doomed = _snapshot(session, repository)
    kept = _snapshot(session, repository, commit_sha=SHA_2)
    session.add_all([_file(doomed, "a.txt"), _file(doomed, "b.txt"), _file(kept, "a.txt")])
    session.commit()

    session.execute(text("DELETE FROM snapshots WHERE id = :i"), {"i": doomed.id})
    session.commit()

    remaining = session.scalars(select(SnapshotFile)).all()
    assert [(f.snapshot_id, f.path) for f in remaining] == [(kept.id, "a.txt")]


def test_snapshot_file_columns_accept_a_path_of_a_few_hundred_characters(
    session: Session,
) -> None:
    """唯一索引 (snapshot_id, path) 是 btree；數百字元的路徑必須能存入（上限見 test_api_edges）。"""
    repository = _repository(session)
    snapshot = _snapshot(session, repository)
    path = "/".join(["segment" * 10] * 12)  # 約 850 字元

    session.add(_file(snapshot, path))
    session.commit()

    assert session.scalars(select(SnapshotFile.path)).one() == path
