"""切片 5d-1：專案描述檔解析的單元測試（純函式，不需資料庫與 Git）。"""

import time
from pathlib import Path

import pytest

from repopilot.services import project_manifests as pm
from repopilot.services.project_manifests import ManifestRef


def _ref(kind: pm.ManifestKind, path: str, size: int = 10) -> ManifestRef:
    return ManifestRef(kind, path, size)


# --- find_manifests ---


def test_find_manifests_picks_root_files_and_any_csproj_in_a_fixed_order() -> None:
    files = [
        ("src/App/App.csproj", 5),
        ("package.json", 5),
        ("requirements.txt", 5),
        ("pyproject.toml", 5),
        ("Other.CSPROJ", 5),
    ]

    found = pm.find_manifests(files)

    assert [(ref.kind, ref.path) for ref in found] == [
        ("pyproject", "pyproject.toml"),
        ("requirements", "requirements.txt"),
        ("package_json", "package.json"),
        ("csproj", "Other.CSPROJ"),  # 依 path 的字元順序：大寫排在小寫前
        ("csproj", "src/App/App.csproj"),
    ]


@pytest.mark.parametrize(
    "path",
    [
        "docs/pyproject.toml",
        "sub/requirements.txt",
        "web/package.json",
        "Pyproject.toml",
        "requirements-dev.txt",
        "package.json.bak",
    ],
)
def test_find_manifests_only_accepts_exact_root_names_for_non_csproj(path: str) -> None:
    assert pm.find_manifests([(path, 1)]) == []


def test_find_manifests_takes_only_the_first_20_csproj_by_path() -> None:
    files = [(f"p{n:02d}/P{n:02d}.csproj", 1) for n in range(30)]

    found = pm.find_manifests(reversed(files))

    assert [ref.path for ref in found] == [path for path, _ in files[:20]]


def test_find_manifests_treats_missing_size_as_zero() -> None:
    assert pm.find_manifests([("package.json", None)])[0].byte_size == 0


# --- pyproject.toml ---


def test_pyproject_reads_name_version_and_dependencies() -> None:
    text = """
[project]
name = "demo"
version = "1.2.3"
dependencies = ["fastapi>=0.115", "httpx[http2]==0.27; python_version >= '3.12'", 5]
"""
    info = pm.parse_pyproject("pyproject.toml", text)

    assert (info.name, info.version) == ("demo", "1.2.3")
    assert info.dependencies == ["fastapi>=0.115", "httpx[http2]==0.27; python_version >= '3.12'"]
    assert (info.truncated, info.skipped_reason, info.parse_error) == (False, None, None)


def test_pyproject_malformed_toml_reports_parse_error_without_content() -> None:
    info = pm.parse_pyproject("pyproject.toml", "[project\nname = SECRET-VALUE")

    assert info.parse_error == "invalid_toml"
    assert (info.name, info.dependencies) == (None, [])
    assert "SECRET-VALUE" not in repr(info)


def test_pyproject_without_project_table_is_listed_with_empty_fields() -> None:
    info = pm.parse_pyproject("pyproject.toml", "[tool.ruff]\nline-length = 100\n")

    assert (info.name, info.version, info.dependencies, info.parse_error) == (None, None, [], None)


def test_pyproject_with_wrongly_typed_fields_does_not_fail() -> None:
    info = pm.parse_pyproject("pyproject.toml", "[project]\nname = 5\ndependencies = 'x'\n")

    assert (info.name, info.dependencies, info.parse_error) == (None, [], None)


def test_pyproject_falls_back_to_poetry_tables() -> None:
    text = """
[tool.poetry]
name = "poet"
version = "0.1.0"

[tool.poetry.dependencies]
python = "^3.12"
requests = "^2.31"
rich = { version = "^13", extras = ["x"] }
local = { path = "../local" }
"""
    info = pm.parse_pyproject("pyproject.toml", text)

    assert (info.name, info.version) == ("poet", "0.1.0")
    assert info.dependencies == ["requests ^2.31", "rich ^13", "local"]


def test_pyproject_project_table_wins_over_poetry() -> None:
    text = '[project]\nname = "a"\n[tool.poetry]\nname = "b"\n'

    assert pm.parse_pyproject("pyproject.toml", text).name == "a"


def test_pyproject_deeply_nested_toml_is_a_parse_error_not_a_crash() -> None:
    text = "a = " + "[" * 5000 + "]" * 5000

    assert pm.parse_pyproject("pyproject.toml", text).parse_error == "invalid_toml"


# --- requirements.txt ---


def test_requirements_skips_comments_blank_lines_and_option_lines() -> None:
    text = """
# a comment
requests==2.31.0
  flask>=2.0   # inline comment

-r other.txt
--index-url https://example.invalid/simple
-e .
uvicorn[standard]>=0.30
pywin32>=300; sys_platform == "win32"
"""
    info = pm.parse_requirements("requirements.txt", text)

    assert info.dependencies == [
        "requests==2.31.0",
        "flask>=2.0",
        "uvicorn[standard]>=0.30",
        'pywin32>=300; sys_platform == "win32"',
    ]
    assert (info.name, info.version) == (None, None)


def test_requirements_handles_crlf_and_empty_input() -> None:
    assert pm.parse_requirements("r.txt", "a==1\r\nb==2\r\n").dependencies == ["a==1", "b==2"]
    assert pm.parse_requirements("r.txt", "").dependencies == []


# --- package.json ---


def test_package_json_lists_dependencies_then_dev_dependencies() -> None:
    text = """{
  "name": "web", "version": "0.0.1",
  "dependencies": {"react": "^19.0.0"},
  "devDependencies": {"vite": "^7.0.0", "weird": 5}
}"""
    info = pm.parse_package_json("package.json", text)

    assert (info.name, info.version) == ("web", "0.0.1")
    assert info.dependencies == ["react@^19.0.0", "vite@^7.0.0", "weird"]


@pytest.mark.parametrize("text", ["{", "", "not json", '{"name": "x",}'])
def test_package_json_malformed_json_is_a_parse_error(text: str) -> None:
    assert pm.parse_package_json("package.json", text).parse_error == "invalid_json"


@pytest.mark.parametrize("text", ["[]", '"x"', "42", "null"])
def test_package_json_non_object_is_a_parse_error(text: str) -> None:
    assert pm.parse_package_json("package.json", text).parse_error == "not_an_object"


def test_package_json_ignores_wrongly_typed_sections() -> None:
    info = pm.parse_package_json("package.json", '{"name": 1, "dependencies": ["a"]}')

    assert (info.name, info.dependencies, info.parse_error) == (None, [], None)


def test_package_json_deeply_nested_is_a_parse_error_not_a_crash() -> None:
    text = "[" * 100_000 + "]" * 100_000

    assert pm.parse_package_json("package.json", text).parse_error == "invalid_json"


def test_bom_is_ignored_for_every_manifest_kind() -> None:
    info = pm.parse_manifest(_ref("package_json", "package.json"), '\ufeff{"name": "bom"}')

    assert (info.name, info.parse_error) == ("bom", None)


# --- 依賴上限與長度 ---


def test_dependencies_are_capped_at_50_and_marked_truncated() -> None:
    text = "\n".join(f"pkg{n}==1" for n in range(51))

    info = pm.parse_requirements("requirements.txt", text)

    assert len(info.dependencies) == 50
    assert info.truncated is True
    assert info.dependencies[-1] == "pkg49==1"


def test_exactly_50_dependencies_are_not_truncated() -> None:
    text = "\n".join(f"pkg{n}==1" for n in range(50))

    info = pm.parse_requirements("requirements.txt", text)

    assert (len(info.dependencies), info.truncated) == (50, False)


def test_long_values_are_clipped_to_200_characters() -> None:
    text = f'[project]\nname = "{"n" * 500}"\ndependencies = ["{"d" * 500}"]\n'

    info = pm.parse_pyproject("pyproject.toml", text)

    assert info.name is not None and len(info.name) == 200
    assert len(info.dependencies[0]) == 200


# --- csproj ---

CSPROJ = """<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Serilog" Version="3.1.1" />
    <PackageReference Include="Dapper">
      <Version>2.1.35</Version>
    </PackageReference>
    <PackageReference Include="NoVersion" />
    <PackageReference Include='SingleQuoted' Version='1.0.0'></PackageReference>
    <PackageReference Update="Ignored" Version="9" />
    <PackageReference version="2.0" include="LowerCase" />
  </ItemGroup>
</Project>
"""


def test_csproj_extracts_package_references_in_every_version_form() -> None:
    info = pm.parse_csproj("src/App/App.csproj", CSPROJ)

    assert info.name == "App"
    assert info.dependencies == [
        "Serilog@3.1.1",
        "Dapper@2.1.35",
        "NoVersion",
        "SingleQuoted@1.0.0",
        "LowerCase@2.0",
    ]
    assert info.target_frameworks == ["net8.0"]
    assert (info.parse_error, info.skipped_reason) == (None, None)


def test_csproj_target_frameworks_plural_is_split_and_deduplicated() -> None:
    text = (
        "<Project><PropertyGroup>"
        "<TargetFrameworks>net8.0; net48 ;net8.0;</TargetFrameworks>"
        '<TargetFramework Condition="x">netstandard2.0</TargetFramework>'
        "</PropertyGroup></Project>"
    )

    assert pm.parse_csproj("A.csproj", text).target_frameworks == [
        "net8.0",
        "net48",
        "netstandard2.0",
    ]


def test_csproj_ignores_commented_out_packages_and_unterminated_comments() -> None:
    text = (
        '<Project><!-- <PackageReference Include="Old" Version="1" /> -->'
        '<PackageReference Include="Live" Version="2" />'
        '<!-- never closed <PackageReference Include="Hidden" Version="3" />'
    )

    assert pm.parse_csproj("A.csproj", text).dependencies == ["Live@2"]


def test_csproj_with_malicious_doctype_and_entities_is_plain_text() -> None:
    text = """<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<Project>
  <PropertyGroup><TargetFramework>&lol2;</TargetFramework></PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Evil&xxe;" Version="&lol2;" />
  </ItemGroup>
</Project>
"""
    info = pm.parse_csproj("Evil.csproj", text)

    # 實體沒有被展開（沒有 file 內容、沒有 "lol" 重複），只是原樣的文字
    assert info.dependencies == ["Evil&xxe;@&lol2;"]
    assert info.target_frameworks == ["&lol2"]  # `;` 是 TargetFrameworks 的分隔符；實體仍未被展開
    assert info.parse_error is None


def test_csproj_parser_does_not_use_an_xml_library() -> None:
    source = Path(pm.__file__).read_text(encoding="utf-8")
    for forbidden in ("xml.etree", "xml.dom", "xml.sax", "lxml", "defusedxml", "xmltodict"):
        assert f"import {forbidden}" not in source
        assert f"from {forbidden}" not in source


def test_csproj_caps_dependencies_and_stops_early() -> None:
    text = "<Project>" + "".join(
        f'<PackageReference Include="P{n}" Version="1" />' for n in range(60)
    )

    info = pm.parse_csproj("A.csproj", text + "</Project>")

    assert (len(info.dependencies), info.truncated) == (50, True)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("<PackageReference " * 20_000, id="open-tags-without-close"),
        pytest.param("<PackageReference Include='a'>" * 8_000, id="unclosed-references"),
        pytest.param("<!--" * 60_000, id="unterminated-comments"),
        pytest.param("<TargetFramework>" * 15_000, id="unterminated-target-framework"),
        pytest.param('<PackageReference Include="a"' + " " * 200_000, id="huge-open-tag"),
    ],
)
def test_csproj_hostile_input_finishes_quickly(payload: str) -> None:
    started = time.monotonic()

    pm.parse_csproj("A.csproj", payload[: pm.MAX_MANIFEST_BYTES])

    assert time.monotonic() - started < 2.0


def test_csproj_without_any_known_elements_is_empty() -> None:
    info = pm.parse_csproj("A.csproj", "not xml at all \x01 <<<>>>")

    assert (info.dependencies, info.target_frameworks, info.parse_error) == ([], [], None)


# --- 分派與跳過 ---


def test_skipped_info_carries_only_the_reason() -> None:
    info = pm.skipped_info(_ref("csproj", "Big.csproj", 999_999), pm.SKIPPED_TOO_LARGE)

    assert (info.skipped_reason, info.dependencies, info.name) == ("too_large", [], None)
