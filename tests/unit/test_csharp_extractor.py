from repopilot.indexing.csharp_extractor import extract_csharp
from repopilot.indexing.types import ExtractedSymbol, FileExtraction


def _extract(
    source: str, *, max_symbols: int = 5000, include_fields: bool = False
) -> FileExtraction:
    return extract_csharp(
        "src/App.cs", source, max_symbols=max_symbols, include_fields=include_fields
    )


def _by_qname(result: FileExtraction) -> dict[str, ExtractedSymbol]:
    return {s.qualified_name: s for s in result.symbols}


BLOCK = """using System;
namespace Outer.Inner
{
    public class Service
    {
        public class Nested { public void Run() { } }
        public Service() { }
        public string Name { get; set; }
        public async Task<int> Fetch<T, U>(int a) => 1;
    }
    public enum Color { Red, Green = 2 }
    public delegate void Handler<T>(T value);
    public interface IThing { void Do(); }
    public struct Point { }
}
"""


def test_block_namespace_with_nested_types_and_all_member_kinds() -> None:
    result = _extract(BLOCK)
    symbols = _by_qname(result)

    assert result.parse_status == "parsed" and result.error_count == 0
    assert result.module_name is None  # C# 沒有 module 符號
    assert symbols["Outer.Inner"].kind == "namespace"
    assert symbols["Outer.Inner.Service"].kind == "class"
    assert (
        symbols["Outer.Inner.Service.Nested"].parent_local_id
        == symbols["Outer.Inner.Service"].local_id
    )
    assert symbols["Outer.Inner.Service.Nested.Run"].kind == "method"
    assert symbols["Outer.Inner.Service.Service"].kind == "constructor"
    assert symbols["Outer.Inner.Service.Name"].kind == "property"
    assert symbols["Outer.Inner.Color"].kind == "enum"
    assert symbols["Outer.Inner.Color.Green"].kind == "enum_member"
    assert symbols["Outer.Inner.Handler"].kind == "delegate"
    assert symbols["Outer.Inner.IThing"].kind == "interface"
    assert symbols["Outer.Inner.IThing.Do"].kind == "method"
    assert symbols["Outer.Inner.Point"].kind == "struct"
    # 外層命名空間沒有父符號；型別的父符號是命名空間
    assert symbols["Outer.Inner"].parent_local_id is None
    assert symbols["Outer.Inner.Service"].parent_local_id == symbols["Outer.Inner"].local_id


def test_generics_are_counted_not_embedded_in_names_and_async_is_marked() -> None:
    symbols = _by_qname(_extract(BLOCK))

    assert symbols["Outer.Inner.Service.Fetch"].type_parameter_count == 2
    assert symbols["Outer.Inner.Handler"].type_parameter_count == 1
    fetch = symbols["Outer.Inner.Service.Fetch"]
    assert fetch.is_async is True and fetch.modifiers == "async public"
    assert symbols["Outer.Inner.Service.Name"].is_async is False


def test_lines_are_one_based_and_inclusive_and_columns_are_byte_offsets() -> None:
    symbols = _by_qname(_extract(BLOCK))

    service = symbols["Outer.Inner.Service"]
    assert (service.start_line, service.end_line) == (4, 10)
    assert service.start_column == 4
    ns = symbols["Outer.Inner"]
    assert (ns.start_line, ns.end_line) == (2, 15)


def test_file_scoped_namespace_applies_to_following_siblings_and_spans_the_file() -> None:
    result = _extract(
        "namespace Acme.Core;\nusing System;\nclass A { void M() { } }\nclass B { }\n"
    )
    symbols = _by_qname(result)

    ns = symbols["Acme.Core"]
    assert ns.kind == "namespace"
    assert (ns.start_line, ns.end_line) == (1, 4)  # 語意範圍：到檔案最後一行
    assert symbols["Acme.Core.A"].parent_local_id == ns.local_id
    assert symbols["Acme.Core.B"].parent_local_id == ns.local_id
    assert symbols["Acme.Core.A.M"].kind == "method"
    # file-scoped namespace 之後的 using 屬於它
    assert result.imports[0].source_local_id == ns.local_id


def test_attributes_keep_names_only_and_set_the_leading_line() -> None:
    result = _extract(
        '[Serializable]\n[Obsolete("secret-token")]\npublic class A\n{\n'
        '    [HttpGet("/x"), Authorize]\n    public void M() { }\n}\n'
    )
    symbols = _by_qname(result)
    relations = [
        (result.symbols[r.source_local_id].qualified_name, r.relation_type, r.target_name, r.line)
        for r in result.relations
    ]

    assert symbols["A"].leading_start_line == 1
    assert symbols["A.M"].leading_start_line == 5
    assert relations == [
        ("A", "attribute", "Serializable", 1),
        ("A", "attribute", "Obsolete", 2),
        ("A.M", "attribute", "HttpGet", 5),
        ("A.M", "attribute", "Authorize", 5),
    ]
    assert all("secret" not in r.target_name for r in result.relations)  # 引數不保存
    assert all(r.resolution == "unresolved" for r in result.relations)


def test_base_types_resolve_only_within_the_file_and_never_to_themselves() -> None:
    result = _extract(
        "class Base { }\nclass Dup { }\nnamespace N { class Dup { } }\n"
        "class Child : Base, Dup, IExternal, Generic<int>, Other.Qualified { }\n"
        "class Same : Same { }\nrecord R(int X) : Base(X);\n"
    )
    child = next(s for s in result.symbols if s.name == "Child").local_id
    base_id = next(s for s in result.symbols if s.name == "Base").local_id
    rels = {r.target_name: r for r in result.relations if r.source_local_id == child}

    assert rels["Base"].resolution == "resolved_same_file"
    assert rels["Base"].target_local_id == base_id
    assert rels["Dup"].resolution == "ambiguous" and rels["Dup"].target_local_id is None
    assert rels["IExternal"].resolution == "unresolved"
    assert rels["Generic<int>"].resolution == "unresolved"
    assert rels["Other.Qualified"].resolution == "unresolved"
    assert all(r.relation_type == "base_type" for r in rels.values())  # 不宣稱「繼承」或「實作」
    same = next(s for s in result.symbols if s.name == "Same").local_id
    (self_relation,) = [r for r in result.relations if r.source_local_id == same]
    assert self_relation.resolution == "unresolved" and self_relation.target_local_id is None
    record = next(s for s in result.symbols if s.name == "R").local_id
    (primary,) = [r for r in result.relations if r.source_local_id == record]
    assert (primary.target_name, primary.resolution) == ("Base", "resolved_same_file")


def test_record_and_record_struct_are_both_records_with_struct_as_a_modifier() -> None:
    symbols = _by_qname(_extract("record A(int X);\nrecord struct B(int Y);\nrecord class C { }\n"))

    assert {s.kind for s in symbols.values()} == {"record"}
    assert symbols["A"].modifiers is None
    assert symbols["B"].modifiers == "struct"


def test_partial_types_are_recorded_per_file_without_merging() -> None:
    first = _extract("public partial class Order { void A() { } }\n")
    second = extract_csharp(
        "src/Order.Part2.cs", "partial class Order { void B() { } }\n", max_symbols=100
    )

    assert "partial" in (first.symbols[0].modifiers or "")
    assert "partial" in (second.symbols[0].modifiers or "")
    assert first.symbols[0].qualified_name == second.symbols[0].qualified_name == "Order"


def test_ifdef_branches_are_both_extracted_marked_conditional_and_may_duplicate() -> None:
    result = _extract(
        "class Always { }\n#if DEBUG\nclass A { void M() { } }\n"
        "#else\nclass A { void N() { } }\n#endif\n"
    )
    symbols = [(s.qualified_name, s.conditional) for s in result.symbols]

    assert symbols == [
        ("Always", False),
        ("A", True),
        ("A.M", True),
        ("A", True),  # 不求值條件：兩個分支的同名型別都記錄
        ("A.N", True),
    ]
    assert result.parse_status == "parsed"


def test_using_directives_cover_every_form() -> None:
    result = _extract(
        "using System;\nusing static System.Math;\nusing Alias = System.Text.StringBuilder;\n"
        "global using System.Linq;\nglobal using static System.Console;\n"
        "namespace N { using System.IO; class A { } }\n"
    )
    imports = [(i.kind, i.target, i.alias, i.line) for i in result.imports]

    assert imports == [
        ("using", "System", None, 1),
        ("using_static", "System.Math", None, 2),
        ("using_alias", "System.Text.StringBuilder", "Alias", 3),
        ("global_using", "System.Linq", None, 4),
        ("global_using", "System.Console", None, 5),
        ("using", "System.IO", None, 6),
    ]
    assert result.imports[0].source_local_id is None  # 頂層 using 沒有所在符號
    ns = next(s for s in result.symbols if s.kind == "namespace")
    assert result.imports[-1].source_local_id == ns.local_id


def test_fields_are_excluded_by_default_and_extracted_per_declarator_when_enabled() -> None:
    source = 'class A { private int _f = 1, _g; public const string K = "k"; int P { get; } }\n'

    default = _by_qname(_extract(source))
    enabled = _by_qname(_extract(source, include_fields=True))

    assert not any(s.kind == "field" for s in default.values())
    assert {n for n, s in enabled.items() if s.kind == "field"} == {"A._f", "A._g", "A.K"}
    assert enabled["A.K"].modifiers == "const public"


def test_unsupported_members_are_not_extracted() -> None:
    result = _extract(
        "class A {\n  ~A() { }\n  event System.EventHandler E;\n  public int this[int i] => 0;\n"
        "  public static A operator +(A a, A b) => a;\n  void Outer() { void Local() { } }\n}\n"
        "Console.WriteLine(1);\n"
    )

    assert [(s.qualified_name, s.kind) for s in result.symbols] == [
        ("A", "class"),
        ("A.Outer", "method"),
    ]


def test_utf8_byte_columns_and_bom_are_counted_in_columns() -> None:
    cjk = _by_qname(_extract("class 類別 { }\nclass B { 類 x; int 方法() { return 1; } }\n"))
    assert cjk["類別"].end_column == len("class 類別 { }".encode())
    assert cjk["B.方法"].start_column == len("class B { 類 x; ".encode())

    bom = _by_qname(_extract("\ufeffclass A { }\n"))
    assert bom["A"].start_column == 3  # BOM 的 3 個位元組
    assert bom["A"].end_column == 3 + len("class A { }")


def test_a_lone_carriage_return_is_not_a_line_break_in_tree_sitter() -> None:
    # 規格 7.1 把單獨的 \r 列為未驗證：這個測試固定目前行為（與 Python 的 ast 不同）
    result = _extract("class A { }\rclass B { }\r")
    lines = {s.name: s.start_line for s in result.symbols}

    assert lines == {"A": 1, "B": 1}


def test_syntax_errors_are_partial_parses_with_positions_and_flagged_symbols() -> None:
    result = _extract("class Ok { void Fine() { } }\nclass Broken {\n    void M( { }\n}\n")
    symbols = _by_qname(result)

    assert result.parse_status == "parsed_with_errors"
    assert result.error_kind == "tree_error"
    assert result.error_count >= 1
    assert result.first_error_line == 3
    assert result.first_error_column is not None
    assert symbols["Ok"].has_errors is False and symbols["Ok.Fine"].has_errors is False
    assert symbols["Broken"].has_errors is True  # 可能不正確，標示而不隱藏
    assert result.symbols  # 其他宣告仍被抽取


def test_newer_syntax_the_grammar_does_not_know_degrades_to_a_partial_parse() -> None:
    result = _extract("class A { void M() { var x = 1 !!! 2; } }\n")

    assert result.parse_status == "parsed_with_errors"
    assert [s.qualified_name for s in result.symbols] == ["A", "A.M"]


def test_empty_and_comment_only_files_have_no_symbols() -> None:
    for source in ("", "// nothing\n/* here */\n"):
        result = _extract(source)
        assert result.parse_status == "parsed" and result.symbols == []


def test_symbol_limit_truncates_without_failing() -> None:
    source = "".join(f"class C{i} {{ }}\n" for i in range(10))
    result = _extract(source, max_symbols=4)

    assert result.parse_status == "parsed" and result.symbols_truncated
    assert len(result.symbols) == 4


def test_very_deep_nesting_never_crashes_the_extractor() -> None:
    source = "class A " + "{ " * 3000 + "}" * 3000 + "\n"
    result = _extract(source)

    assert result.parse_status in ("parsed", "parsed_with_errors", "failed")
