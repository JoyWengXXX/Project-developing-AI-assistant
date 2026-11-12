"""容器 healthcheck（第二階段切片 3）：`python -m repopilot.jobs.healthcheck dispatcher|worker`。

退出碼 0＝這個實例（主機名稱，容器內是容器 ID）的心跳在 `3 × SERVICE_HEARTBEAT_SECONDS` 內更新過；
否則 1。讓 `docker compose up -d --wait` 能等到服務**真的在寫心跳**才算就緒，而不只是行程存在。
不連 Redis（broker 的健康由 dispatcher 的發布失敗與日誌反映）。
"""

import sys

from repopilot.config import get_settings
from repopilot.db.session import create_db_engine, create_session_factory
from repopilot.services import service_heartbeats


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in service_heartbeats.SERVICES:
        sys.stderr.write("usage: python -m repopilot.jobs.healthcheck dispatcher|worker\n")
        return 2
    settings = get_settings()
    engine = create_db_engine(settings)
    try:
        with create_session_factory(engine)() as session:
            alive = service_heartbeats.is_instance_alive(
                session, argv[0], service_heartbeats.default_instance_id(), settings
            )
    except Exception:
        return 1
    finally:
        engine.dispose()
    return 0 if alive else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
