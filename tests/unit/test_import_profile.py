import pytest

from repopilot.config import Settings
from repopilot.services.import_profile import ImportProfile


def _profile(**overrides: object) -> ImportProfile:
    return ImportProfile.from_settings(Settings(**overrides))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "config/.env",
        ".env.production",
        ".ENV.local",
        "server.pem",
        "keys/private.key",
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "cert.p12",
        "cert.pfx",
        "credentials.json",
        "credentials-prod.json",
        ".npmrc",
        ".pypirc",
        "sub/.netrc",
    ],
)
def test_default_profile_excludes_secret_files(path: str) -> None:
    assert _profile().is_excluded(path)


@pytest.mark.parametrize(
    "path", [".env.example", "docs/.env.example", "README.md", "src/environment.py", "key.txt"]
)
def test_default_profile_keeps_normal_files(path: str) -> None:
    assert not _profile().is_excluded(path)


def test_directory_names_do_not_trigger_exclusion() -> None:
    assert not _profile().is_excluded(".env/readme.txt")


def test_hash_is_stable_and_ignores_pattern_order_and_case() -> None:
    a = _profile(import_excluded_patterns=["*.PEM", ".env"])
    b = _profile(import_excluded_patterns=[".env", "*.pem"])

    assert a.hash() == b.hash()
    assert len(a.hash()) == 64


@pytest.mark.parametrize(
    "overrides",
    [
        {"import_max_file_bytes": 10},
        {"import_max_files": 10},
        {"import_max_snapshot_bytes": 10},
        {"import_excluded_patterns": [".env"]},
        {"import_excluded_exceptions": []},
    ],
)
def test_hash_changes_when_any_rule_changes(overrides: dict[str, object]) -> None:
    assert _profile(**overrides).hash() != _profile().hash()
