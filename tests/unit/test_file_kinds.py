import pytest

from repopilot.indexing.file_kinds import FILE_KINDS, RETRIEVAL_EXCLUDED_KINDS, classify_path
from repopilot.services.retrieval_chunks import chunk_profile_hash

PATHS = [
    ("src/app/main.py", "source"),
    ("deds-portal/Controllers/AccountController.cs", "source"),
    ("README.md", "docs"),
    ("docs/integration-tests/issue-14/plan.md", "docs"),
    ("docs/conf.py", "docs"),
    ("Tests/deds-portal.Tests/Controllers/V2/LoggerFilesControllerTests.cs", "test"),
    ("deds-portal-vue/tests/integration/issue-22/vehicle.spec.ts", "test"),
    ("src/__tests__/button.test.tsx", "test"),
    ("pkg/service_test.go", "test"),
    ("tests/test_search.py", "test"),
    ("deds-portal/wwwroot/assets/index-Be5Xn6Gr.js", "build"),
    ("app/dist/main.js", "build"),
    ("assets/vendor.min.js", "build"),
    ("web/node_modules/left-pad/index.js", "build"),
    ("NLP/QApair_NLP.csv", "data"),
    ("icons/logo.svg", "data"),
    ("frontend/package-lock.json", "data"),
    ("poetry.lock", "data"),
    # 目錄名稱必須完整相符；名稱相近或檔名巧合不算
    ("src/rebuild/tool.py", "source"),
    ("src/latest.cs", "source"),
    ("src/builder.py", "source"),
    ("SRC/Docs/Guide.TXT", "docs"),
    # build 優先於 test，data 優先於 docs
    ("dist/tests/app.js", "build"),
    ("docs/table.csv", "data"),
]


@pytest.mark.parametrize(("path", "kind"), PATHS)
def test_classify_path(path: str, kind: str) -> None:
    assert classify_path(path) == kind


def test_retrieval_skips_only_build_and_data() -> None:
    assert set(RETRIEVAL_EXCLUDED_KINDS) == {"build", "data"}
    assert set(RETRIEVAL_EXCLUDED_KINDS) <= set(FILE_KINDS)


def test_excluded_kinds_change_the_chunk_set_identity_only_when_present() -> None:
    plain = chunk_profile_hash(32768, 1)
    assert chunk_profile_hash(32768, 1, ()) == plain
    excluded = chunk_profile_hash(32768, 1, RETRIEVAL_EXCLUDED_KINDS)
    assert excluded != plain
    assert chunk_profile_hash(32768, 1, ("build", "data")) == chunk_profile_hash(
        32768, 1, ("data", "build", "data")
    )
