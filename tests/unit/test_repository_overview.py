"""切片 5d-1：概況的純函式單元測試——語言對照、README 挑選、字元截斷（不需資料庫與 Git）。"""

import pytest

from repopilot.services.repository_overview import (
    MAX_LANGUAGES,
    OTHER_LANGUAGE,
    LanguageStat,
    language_for_path,
    pick_readme,
    summarize_languages,
    truncate_chars,
)

# --- 副檔名 → 語言 ---


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("main.py", "Python"),
        ("src/App/Program.cs", "C#"),
        ("web/app.ts", "TypeScript"),
        ("web/App.tsx", "TypeScript"),
        ("web/app.js", "JavaScript"),
        ("web/App.JSX", "JavaScript"),
        ("docs/guide.md", "Markdown"),
        ("package.json", "JSON"),
        ("ci.yml", "YAML"),
        ("ci.yaml", "YAML"),
        ("schema.sql", "SQL"),
        ("index.html", "HTML"),
        ("site.css", "CSS"),
        ("run.sh", "Shell"),
        ("main.go", "Go"),
        ("Main.java", "Java"),
        ("App.csproj", "MSBuild"),
        ("All.sln", "MSBuild"),
        ("Directory.Build.props", "MSBuild"),
        ("Common.targets", "MSBuild"),
        ("Dockerfile", "Dockerfile"),
        ("docker/DOCKERFILE", "Dockerfile"),
        ("README.MD", "Markdown"),  # 大小寫不敏感
        ("Program.CS", "C#"),
    ],
)
def test_language_is_decided_by_extension_or_name_case_insensitively(
    path: str, language: str
) -> None:
    assert language_for_path(path) == language


@pytest.mark.parametrize(
    "path",
    ["LICENSE", "Makefile", ".gitignore", ".env.example", "data.unknownext", "trailing.", "a/b/c"],
)
def test_unknown_or_extensionless_files_are_other(path: str) -> None:
    assert language_for_path(path) == OTHER_LANGUAGE


def test_only_the_file_name_matters_not_dots_in_directories() -> None:
    assert language_for_path("v1.2/Makefile") == OTHER_LANGUAGE
    assert language_for_path("a.py/notes") == OTHER_LANGUAGE


def test_dockerfile_variants_with_suffix_are_not_recognised_by_name() -> None:
    # 限制：只認 `Dockerfile` 這個檔名；`Dockerfile.dev` 的副檔名 `.dev` 不在對照表
    assert language_for_path("Dockerfile.dev") == OTHER_LANGUAGE


# --- 彙總 ---


def test_summary_tie_break_is_by_language_name() -> None:
    files = [("a.py", 1), ("b.md", 1), ("c.cs", 1), ("d.go", 1)]

    assert [s.language for s in summarize_languages(files)] == ["C#", "Go", "Markdown", "Python"]


def test_summary_counts_and_bytes_per_language() -> None:
    files = [("a.py", 10), ("b.py", 20), ("c.cs", 5), ("x.weird", 3), ("README", 4)]

    assert summarize_languages(files) == [
        LanguageStat("Python", 2, 30),
        LanguageStat("C#", 1, 5),
        LanguageStat(OTHER_LANGUAGE, 2, 7),
    ]


def test_other_is_always_last_even_when_it_has_the_most_files() -> None:
    files = [("a.py", 1)] + [(f"f{n}.weird", 1) for n in range(5)]

    assert [s.language for s in summarize_languages(files)] == ["Python", OTHER_LANGUAGE]


def test_summary_of_nothing_is_empty() -> None:
    assert summarize_languages([]) == []


def _many_languages(count: int) -> list[tuple[str, int]]:
    extensions = [".py", ".cs", ".ts", ".js", ".md", ".json", ".yml", ".sql", ".html", ".css"]
    extensions += [".sh", ".go", ".java", ".rs"]
    # 第 n 種語言有 (count - n) 個檔案，讓排序可預期
    files: list[tuple[str, int]] = []
    for index, extension in enumerate(extensions[:count]):
        files.extend((f"f{index}_{n}{extension}", 1) for n in range(count - index))
    return files


def test_exactly_ten_languages_are_all_kept() -> None:
    result = summarize_languages(_many_languages(10))

    assert len(result) == 10
    assert OTHER_LANGUAGE not in [s.language for s in result]


def test_more_than_ten_languages_fold_the_tail_into_other() -> None:
    files = _many_languages(12)
    total = len(files)

    result = summarize_languages(files)

    assert len(result) == MAX_LANGUAGES
    assert result[-1].language == OTHER_LANGUAGE
    assert sum(s.files for s in result) == total  # 合併不丟檔案
    # 保留的 9 項是檔案數最多的 9 種
    assert [s.files for s in result[:-1]] == sorted((s.files for s in result[:-1]), reverse=True)
    assert result[-1].files == 3 + 2 + 1  # 最後三種語言（3、2、1 個檔案）


def test_ten_languages_plus_unrecognised_files_also_fit_in_ten_entries() -> None:
    files = [*_many_languages(10), ("x.weird", 1)]

    result = summarize_languages(files)

    assert len(result) == MAX_LANGUAGES
    assert result[-1] == LanguageStat(OTHER_LANGUAGE, 1 + 1, 2)  # 第 10 種語言（1 檔）併入


# --- README 挑選 ---


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["README.md"], "README.md"),
        (["readme.MD"], "readme.MD"),
        (["Readme"], "Readme"),
        (["README.rst"], "README.rst"),
        (["readme.txt"], "readme.txt"),
        (["README.markdown"], "README.markdown"),
        # 優先序：README > README.md > README.rst > README.txt > README.markdown
        (["README.txt", "README.md", "README.rst"], "README.md"),
        (["README.markdown", "README.txt"], "README.txt"),
        (["README.md", "README"], "README"),
        # 同優先序（大小寫不同的兩個檔案）依 path 排序，結果固定
        (["readme.md", "README.md"], "README.md"),
        # 巢狀目錄不算；非 README 名稱不算
        (["docs/README.md", "src/readme"], None),
        (["README.md.bak", "READ_ME.md", "README.html", "notes.md"], None),
        ([], None),
    ],
)
def test_pick_readme_uses_fixed_priority_and_root_only(
    paths: list[str], expected: str | None
) -> None:
    assert pick_readme(paths) == expected


def test_nested_readme_does_not_hide_a_lower_priority_root_readme() -> None:
    assert pick_readme(["docs/README.md", "README.txt"]) == "README.txt"


# --- 字元截斷 ---


def test_truncate_chars_below_at_and_above_the_limit() -> None:
    assert truncate_chars("abc", 4) == ("abc", False)
    assert truncate_chars("abcd", 4) == ("abcd", False)
    assert truncate_chars("abcde", 4) == ("abcd", True)


def test_truncate_chars_counts_characters_and_never_splits_multibyte_characters() -> None:
    text = "中" * 10 + "😀" * 10  # 中文 3 bytes、emoji 4 bytes（UTF-8）

    cut, truncated = truncate_chars(text, 15)

    assert truncated is True
    assert cut == "中" * 10 + "😀" * 5
    assert len(cut.encode("utf-8")) == 10 * 3 + 5 * 4
    assert cut.encode("utf-8").decode("utf-8") == cut  # 仍是合法的 UTF-8


def test_truncate_chars_of_empty_text() -> None:
    assert truncate_chars("", 5) == ("", False)
