"""跨程序互斥：PostgreSQL advisory lock（規格 5.10）。以多條真實連線驗證互斥、共享、崩潰釋放。"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from repopilot.db.locks import (
    acquire_advisory_lock,
    engine_of,
    lock_key,
    repo_lock_key,
    snapshot_lock_key,
    try_advisory_lock,
)
from repopilot.db.models import Repository
from repopilot.errors import DatabaseUnavailableError
from repopilot.services.repositories import repository_key
from tests.integration.jobs.conftest import named_engine, scalar


def _advisory_lock_count(engine: Engine, key: int) -> int:
    # bigint 鍵在 pg_locks 拆成 classid（高 32 位元）與 objid（低 32 位元）
    unsigned = key & 0xFFFFFFFFFFFFFFFF
    classid, objid = unsigned >> 32, unsigned & 0xFFFFFFFF
    return int(
        scalar(
            engine,
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND classid = :c AND objid = :o",
            c=classid,
            o=objid,
        )
    )


@pytest.fixture
def second_engine(test_database_url: str) -> Iterator[Engine]:
    # 另一個「程序」：獨立的 Engine（獨立連線池）
    engine = named_engine(test_database_url, "lock-test-second")
    yield engine
    engine.dispose()


# --- 鍵 ---


def test_keys_are_deterministic_signed_bigints_and_namespaced() -> None:
    key = repo_lock_key("local:1")

    assert key == repo_lock_key("local:1")
    assert -(2**63) <= key < 2**63
    assert repo_lock_key("local:1") != repo_lock_key("local:2")
    assert repo_lock_key("local:1") != repo_lock_key("github:1")
    # 同一個名稱在不同命名空間不會互撞（repo 鎖與快照鎖是不同的鍵空間）
    assert lock_key("repo", "x") != lock_key("snapshot", "x")


def test_snapshot_key_depends_on_every_part_of_the_snapshot_identity() -> None:
    base = snapshot_lock_key(1, "a" * 40, "h" * 64)

    assert base == snapshot_lock_key(1, "a" * 40, "h" * 64)
    assert base != snapshot_lock_key(2, "a" * 40, "h" * 64)
    assert base != snapshot_lock_key(1, "b" * 40, "h" * 64)
    assert base != snapshot_lock_key(1, "a" * 40, "i" * 64)


def test_github_repo_keys_use_the_lowercase_owner_and_name() -> None:
    upper = Repository(
        name="x",
        canonical_local_path="D:\\a",
        source_type="github",
        github_owner="Octo",
        github_name="Hello",
    )
    lower = Repository(
        name="x",
        canonical_local_path="D:\\b",
        source_type="github",
        github_owner="octo",
        github_name="hello",
    )
    local = Repository(id=7, name="x", canonical_local_path="D:\\c", source_type="local")

    assert repository_key(upper) == repository_key(lower) == "github:octo/hello"
    assert repository_key(local) == "local:7"


# --- 互斥 ---


def test_exclusive_lock_is_refused_while_another_connection_holds_it(
    db_engine: Engine, second_engine: Engine
) -> None:
    key = lock_key("test", "exclusive")
    first = acquire_advisory_lock(db_engine, key)
    assert first is not None
    try:
        assert _advisory_lock_count(db_engine, key) == 1
        assert acquire_advisory_lock(second_engine, key) is None  # 不等待，直接回 None
        assert acquire_advisory_lock(db_engine, key) is None  # 同一個 Engine、另一條連線也一樣
    finally:
        first.release()

    assert _advisory_lock_count(db_engine, key) == 0
    again = acquire_advisory_lock(second_engine, key)
    assert again is not None  # 釋放後可以取得
    again.release()


def test_shared_locks_coexist_but_exclude_the_exclusive_lock(
    db_engine: Engine, second_engine: Engine
) -> None:
    key = lock_key("test", "shared")
    first = acquire_advisory_lock(db_engine, key, shared=True)
    second = acquire_advisory_lock(second_engine, key, shared=True)
    assert first is not None
    assert second is not None
    try:
        assert acquire_advisory_lock(db_engine, key) is None  # 獨占（移除 repo）被共享（匯入）擋住
    finally:
        first.release()
    # 還有一個共享持有者時，獨占仍被擋
    assert acquire_advisory_lock(db_engine, key) is None
    second.release()

    exclusive = acquire_advisory_lock(db_engine, key)
    assert exclusive is not None
    try:
        assert acquire_advisory_lock(second_engine, key, shared=True) is None  # 獨占擋住共享
    finally:
        exclusive.release()


def test_context_manager_yields_none_when_busy_and_releases_on_exit(
    db_engine: Engine, second_engine: Engine
) -> None:
    key = lock_key("test", "context")
    with try_advisory_lock(db_engine, key) as held:
        assert held is not None
        with try_advisory_lock(second_engine, key) as busy:
            assert busy is None
    assert _advisory_lock_count(db_engine, key) == 0


def test_the_lock_is_released_when_the_body_raises(db_engine: Engine) -> None:
    key = lock_key("test", "raises")
    with pytest.raises(RuntimeError), try_advisory_lock(db_engine, key):
        raise RuntimeError("boom")

    assert _advisory_lock_count(db_engine, key) == 0


def test_release_is_idempotent(db_engine: Engine) -> None:
    lock = acquire_advisory_lock(db_engine, lock_key("test", "idempotent"))
    assert lock is not None

    lock.release()
    lock.release()  # 第二次什麼都不做


def test_a_released_lock_connection_is_not_returned_to_the_pool_holding_a_lock(
    db_engine: Engine,
) -> None:
    """鎖結束後，連線池裡不會殘留持有鎖的連線：之後任何連線都能取得同一把鎖。"""
    key = lock_key("test", "pool")
    for _ in range(5):
        lock = acquire_advisory_lock(db_engine, key)
        assert lock is not None
        lock.release()

    assert _advisory_lock_count(db_engine, key) == 0


# --- 崩潰釋放與連線中斷偵測 ---


def test_the_lock_is_freed_when_its_connection_dies_and_the_loss_is_detected(
    db_engine: Engine, second_engine: Engine
) -> None:
    key = lock_key("test", "crash")
    held = acquire_advisory_lock(second_engine, key)
    assert held is not None
    held.assert_alive()  # 連線正常時不拋例外
    pid = scalar(
        db_engine,
        "SELECT pid FROM pg_stat_activity WHERE application_name = 'lock-test-second' "
        "AND pid IN (SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted)",
    )
    assert pid is not None

    # 模擬持鎖程序崩潰／網路中斷：伺服器端結束該連線
    assert scalar(db_engine, "SELECT pg_terminate_backend(:p)", p=pid) is True

    # 伺服器已釋放鎖（沒有過期鎖），別人立刻可以取得
    taken = None
    for _ in range(100):
        taken = acquire_advisory_lock(db_engine, key)
        if taken is not None:
            break
    assert taken is not None
    taken.release()
    # 持有者在發布前的檢查會發現連線已斷（鎖默默消失）
    with pytest.raises(DatabaseUnavailableError):
        held.assert_alive()
    held.release()  # 清理不拋例外


def test_assert_alive_after_release_raises(db_engine: Engine) -> None:
    lock = acquire_advisory_lock(db_engine, lock_key("test", "released"))
    assert lock is not None
    lock.release()

    with pytest.raises(DatabaseUnavailableError):
        lock.assert_alive()


def test_unreachable_database_is_reported_as_database_unavailable() -> None:
    engine = create_engine(
        "postgresql+psycopg://nobody:nopass@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )
    try:
        with pytest.raises(DatabaseUnavailableError):
            acquire_advisory_lock(engine, 1)
    finally:
        engine.dispose()


def test_engine_of_returns_the_engine_behind_a_session(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        assert engine_of(session) is db_engine
