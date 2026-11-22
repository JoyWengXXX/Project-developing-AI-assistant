"""新專案（第五階段）整合測試共用：環境資料類別、常數與帳本查詢（fixture 在 conftest.py）。"""

from dataclasses import dataclass, field

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from repopilot.qa.fake_provider import FakeProvider

HAIKU = "claude-haiku-5-5"
FAKE_API_KEY = "sk-ant-FAKE-0123456789-abcdefghijklmnopqrstuvwxyz-ZYXWVUTSRQ"
REFERENCE_FILES = {
    "requirements.txt": "fastapi>=0.110\nSQLAlchemy>=2\npytest\npsycopg\n",
    "src/app.py": "print('x')\n",
    "src/util.py": "print('y')\n",
}


@dataclass
class Env:
    client: TestClient
    engine: Engine
    repository_id: int
    snapshot_id: int
    provider: FakeProvider = field(default_factory=FakeProvider)
    keys_seen: list[str] = field(default_factory=list)

    def configure(self, *, consent: bool = True) -> None:
        put = self.client.put
        assert put("/api/v1/qa/provider-key", json={"api_key": FAKE_API_KEY}).status_code == 200
        assert put("/api/v1/qa/settings", json={"model": HAIKU}).status_code == 200
        if consent:
            assert put(f"/api/v1/repositories/{self.repository_id}/qa-consent").status_code == 200


def ledger_events(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                text("SELECT event_type FROM qa_usage_ledger ORDER BY id")
            )
        ]
