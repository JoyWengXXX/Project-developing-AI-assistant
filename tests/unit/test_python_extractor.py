import dataclasses

from repopilot.config import Settings
from repopilot.indexing.profile import IndexProfile
from repopilot.indexing.python_extractor import extract_python, module_name_from_path
from repopilot.indexing.types import ExtractedSymbol, FileExtraction


def _extract(source: str, path: str = "pkg/mod.py", max_symbols: int = 5000) -> FileExtraction:
    return extract_python(path, source, max_symbols=max_symbols)


def _by_qname(result: FileExtraction) -> dict[str, ExtractedSymbol]:
    return {s.qualified_name: s for s in result.symbols}


def test_module_names_come_from_the_path_only() -> None:
    assert module_name_from_path("src/pkg/a.py") == "src.pkg.a"
    assert module_name_from_path("pkg/__init__.py") == "pkg"
    assert module_name_from_path("__init__.py") == "__init__"
    assert module_name_from_path("my-dir/setup.PY") == "my-dir.setup"


def test_symbol_tree_with_nesting_methods_and_async() -> None:
    result = _extract(
        "class Outer:\n"
        "    class Inner:\n"
        "        def deep(self): ...\n"
        "    async def run(self):\n"
        "        def helper(): ...\n"
        "def top(): ...\n"
    )
    symbols = _by_qname(result)
    assert result.parse_status == "parsed"
    assert result.module_name == "pkg.mod"
    assert symbols["pkg.mod"].kind == "module"
    assert symbols["pkg.mod"].parent_local_id is None
    assert symbols["pkg.mod.Outer"].kind == "class"
    assert symbols["pkg.mod.Outer.Inner"].parent_local_id == symbols["pkg.mod.Outer"].local_id
    assert symbols["pkg.mod.Outer.Inner.deep"].kind == "method"
    assert symbols["pkg.mod.Outer.run"].kind == "method"
    assert symbols["pkg.mod.Outer.run"].is_async is True
    # 函式內的函式不是方法
    assert symbols["pkg.mod.Outer.run.helper"].kind == "function"
    assert symbols["pkg.mod.top"].kind == "function"
    assert symbols["pkg.mod.top"].is_async is False


def test_lines_are_one_based_and_inclusive_and_decorators_are_leading_lines() -> None:
    result = _extract("import os\n\n@a\n@b(1)\ndef f():\n    return 1\n")
    f = _by_qname(result)["pkg.mod.f"]
    assert (f.start_line, f.end_line) == (5, 6)
    assert f.leading_start_line == 3
    module = _by_qname(result)["pkg.mod"]
    assert (module.start_line, module.end_line) == (1, 6)


def test_columns_are_utf8_byte_offsets() -> None:
    result = _extract("x = '中文'; y = 1\nclass 類別:\n    pass\n")
    # 識別字可為 Unicode；欄位是位元組位移，不是字元位置
    cls = _by_qname(result)["pkg.mod.類別"]
    assert cls.start_column == 0
    assert cls.end_column == len(b"    pass")
    result = _extract("if True:\n    class 中: pass\n")
    nested = _by_qname(result)["pkg.mod.中"]
    assert nested.start_column == 4
    assert nested.end_column == 4 + len("class 中: pass".encode())


def test_bom_is_counted_in_columns_of_the_first_line() -> None:
    result = _extract("\ufeffclass A: pass\n")
    a = _by_qname(result)["pkg.mod.A"]
    assert result.parse_status == "parsed"
    assert a.start_column == 3  # BOM 的 3 個位元組
    assert a.end_column == 3 + len("class A: pass")
    assert _by_qname(result)["pkg.mod"].end_column == 3 + len("class A: pass")


def test_conditional_definitions_inherit_to_nested_symbols() -> None:
    result = _extract(
        "try:\n"
        "    import fast\n"
        "except ImportError:\n"
        "    def slow(): ...\n"
        "if X:\n"
        "    class A:\n"
        "        def m(self): ...\n"
        "def plain(): ...\n"
        "with ctx():\n"
        "    def in_with(): ...\n"
        "match v:\n"
        "    case 1:\n"
        "        def in_match(): ...\n"
        "for i in x:\n"
        "    def in_loop(): ...\n"
    )
    symbols = _by_qname(result)
    assert symbols["pkg.mod.slow"].conditional
    assert symbols["pkg.mod.A"].conditional
    assert symbols["pkg.mod.A.m"].conditional
    assert symbols["pkg.mod.in_with"].conditional
    assert symbols["pkg.mod.in_match"].conditional
    assert symbols["pkg.mod.in_loop"].conditional
    assert not symbols["pkg.mod.plain"].conditional
    assert not symbols["pkg.mod"].conditional


def test_imports_cover_aliases_relative_levels_and_nested_scopes() -> None:
    result = _extract(
        "import os, numpy as np\n"
        "from . import sibling\n"
        "from ..pkg import thing as t, other\n"
        "from x import *\n"
        "def f():\n"
        "    import json\n"
    )
    imports = [
        (i.kind, i.target, i.imported_name, i.alias, i.relative_level, i.line)
        for i in result.imports
    ]
    assert imports == [
        ("import", "os", None, None, None, 1),
        ("import", "numpy", None, "np", None, 1),
        ("from_import", "", "sibling", None, 1, 2),
        ("from_import", "pkg", "thing", "t", 2, 3),
        ("from_import", "pkg", "other", None, 2, 3),
        ("from_import", "x", "*", None, 0, 4),
        ("import", "json", None, None, None, 6),
    ]
    symbols = {s.local_id: s.qualified_name for s in result.symbols}
    assert symbols[result.imports[0].source_local_id or 0] == "pkg.mod"
    assert symbols[result.imports[-1].source_local_id or 0] == "pkg.mod.f"


def test_base_class_resolution_resolved_ambiguous_unresolved() -> None:
    result = _extract(
        "class Base: ...\n"
        "class Dup: ...\n"
        "class Other:\n"
        "    class Dup: ...\n"
        "class Child(Base, Dup, ext.Remote, Missing, Generic[T]): ...\n"
    )
    child = _by_qname(result)["pkg.mod.Child"].local_id
    base_id = _by_qname(result)["pkg.mod.Base"].local_id
    rels = {r.target_name: r for r in result.relations if r.source_local_id == child}
    assert rels["Base"].resolution == "resolved_same_file"
    assert rels["Base"].target_local_id == base_id
    assert rels["Dup"].resolution == "ambiguous"
    assert rels["Dup"].target_local_id is None
    assert rels["ext.Remote"].resolution == "unresolved"
    assert rels["Missing"].resolution == "unresolved"
    assert rels["Generic[T]"].resolution == "unresolved"
    assert all(r.relation_type == "base_class" for r in rels.values())


def test_a_class_never_resolves_to_itself() -> None:
    result = _extract("from x import Foo\nclass Foo(Foo): ...\n")
    (relation,) = result.relations
    assert relation.resolution == "unresolved"
    assert relation.target_local_id is None


def test_decorators_keep_only_the_callee_name_not_the_arguments() -> None:
    result = _extract(
        "@app.get('/secret-path', token='abc123')\n@plain\n@pkg.factory(1)(2)\ndef f(): ...\n"
    )
    names = [r.target_name for r in result.relations if r.relation_type == "decorator"]
    assert names == ["app.get", "plain", "pkg.factory(1)"]
    assert all("abc123" not in n and "secret" not in n for n in names)
    assert all(r.resolution == "unresolved" for r in result.relations)


def test_type_parameters_are_counted() -> None:
    result = _extract("class A[T, U]: ...\ndef f[T](x: T): ...\n")
    symbols = _by_qname(result)
    assert symbols["pkg.mod.A"].type_parameter_count == 2
    assert symbols["pkg.mod.f"].type_parameter_count == 1


def test_syntax_error_fails_the_whole_file_without_symbols_or_message() -> None:
    result = _extract("def f(:\n    pass\n")
    assert result.parse_status == "failed"
    assert result.error_kind == "syntax_error"
    assert result.error_count == 1
    assert result.first_error_line == 1
    assert result.first_error_column is not None
    assert result.symbols == [] and result.imports == [] and result.relations == []


def test_syntax_error_column_is_a_byte_offset_after_non_ascii_text() -> None:
    result = _extract("x = '中文' +\n")
    assert result.parse_status == "failed"
    assert result.first_error_line == 1
    assert result.first_error_column is not None and result.first_error_column > len("x = '中文'")


def test_null_bytes_and_invalid_escapes_are_isolated_to_the_file() -> None:
    assert _extract("x = 1\x00\n").parse_status == "failed"
    # 無效逸出序列只是 SyntaxWarning：不影響解析，也不應洩漏為例外
    assert _extract("x = '\\d'\n").parse_status == "parsed"


def test_deeply_nested_expression_is_failed_not_a_crash() -> None:
    result = _extract("x = " + "(" * 5000 + "1" + ")" * 5000 + "\n")
    assert result.parse_status == "failed"
    assert result.error_kind in ("too_deep", "syntax_error")


def test_symbol_limit_truncates_without_failing() -> None:
    source = "".join(f"def f{i}(): ...\n" for i in range(10)) + "import late\n"
    result = _extract(source, max_symbols=4)
    assert result.parse_status == "parsed"
    assert result.symbols_truncated
    assert len(result.symbols) == 4
    assert result.imports == []  # 達上限後停止走訪


def test_empty_file_has_only_a_module_symbol() -> None:
    result = _extract("")
    assert [s.kind for s in result.symbols] == ["module"]
    assert (result.symbols[0].start_line, result.symbols[0].end_line) == (1, 1)


def test_lone_carriage_returns_count_as_line_breaks_like_ast_does() -> None:
    # 規格 7.1 把單獨的 \r 列為未驗證：這個測試固定目前行為（ast 與模組符號都視為換行）
    result = _extract("class A: ...\rclass B: ...\r")
    symbols = _by_qname(result)
    assert symbols["pkg.mod.A"].start_line == 1
    assert symbols["pkg.mod.B"].start_line == 2
    assert symbols["pkg.mod"].end_line == 2


def test_index_profile_hash_changes_with_versions_limits_and_the_field_switch() -> None:
    base = IndexProfile.from_settings(Settings())
    assert base.hash() == IndexProfile.from_settings(Settings()).hash()
    assert len(base.hash()) == 64
    assert IndexProfile.from_settings(Settings(index_max_symbols_per_file=10)).hash() != base.hash()
    assert (
        IndexProfile.from_settings(Settings(index_csharp_include_fields=True)).hash() != base.hash()
    )
    changed = [
        dataclasses.replace(base, python_major_minor="3.99"),
        dataclasses.replace(base, tree_sitter="9.9.9"),
        dataclasses.replace(base, csharp_grammar="9.9.9"),
        dataclasses.replace(base, typescript_grammar="9.9.9"),
        dataclasses.replace(base, javascript_grammar="9.9.9"),
        dataclasses.replace(base, java_grammar="9.9.9"),
        dataclasses.replace(base, go_grammar="9.9.9"),
    ]
    assert all(profile.hash() != base.hash() for profile in changed)
    csharp = base.analyzers()["csharp"]
    assert csharp["enabled"] is True and csharp["include_fields"] is False
    # ABI 版本只供除錯：出現在存入資料庫的說明，但不影響雜湊
    assert isinstance(base.describe()["csharp"]["abi_version"], int)
    assert "abi_version" not in csharp
