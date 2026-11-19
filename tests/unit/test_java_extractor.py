from repopilot.indexing.java_extractor import extract_java


def test_java_declarations_imports_and_positions() -> None:
    source = (
        "package com.example;\n"
        "import java.util.List;\n"
        "import static java.util.Collections.emptyList;\n"
        "public class Service {\n"
        "  public Service() {}\n"
        "  public void run() {}\n"
        "  class Nested {}\n"
        "}\n"
        "interface Worker { void work(); }\n"
        "enum Status { OK, BAD; void check() {} }\n"
        "record User(String name) {}\n"
    )
    result = extract_java("src/Service.java", source, max_symbols=100)
    symbols = {item.qualified_name: item for item in result.symbols}
    assert result.language == "java"
    assert result.parse_status == "parsed"
    assert result.module_name == "com.example"
    assert {name: symbol.kind for name, symbol in symbols.items()} == {
        "Service": "class",
        "Service.Service": "constructor",
        "Service.run": "method",
        "Service.Nested": "class",
        "Worker": "interface",
        "Worker.work": "method",
        "Status": "enum",
        "Status.OK": "enum_member",
        "Status.BAD": "enum_member",
        "Status.check": "method",
        "User": "record",
    }
    assert symbols["Service.run"].parent_local_id == symbols["Service"].local_id
    assert symbols["Service.run"].start_line == 6
    assert [(item.kind, item.target, item.line) for item in result.imports] == [
        ("java_import", "java.util.List", 2),
        ("java_static_import", "java.util.Collections.emptyList", 3),
    ]


def test_java_partial_parse_and_limit() -> None:
    malformed = extract_java("Broken.java", "class Good {}\nclass Broken {\n", max_symbols=100)
    assert malformed.parse_status == "parsed_with_errors"
    assert malformed.error_kind == "tree_error"
    assert malformed.error_count > 0
    assert any(item.name == "Good" for item in malformed.symbols)

    limited = extract_java("Many.java", "class One {}\nclass Two {}\n", max_symbols=1)
    assert [item.name for item in limited.symbols] == ["One"]
    assert limited.symbols_truncated


def test_java_wildcard_imports_and_invalid_package() -> None:
    result = extract_java(
        "Imports.java",
        "package ;\nimport java.util.*;\nimport static java.util.Collections.*;\nclass C {}\n",
        max_symbols=100,
    )
    assert result.parse_status == "parsed_with_errors"
    assert [(item.kind, item.target) for item in result.imports] == [
        ("java_import", "java.util.*"),
        ("java_static_import", "java.util.Collections.*"),
    ]


def test_java_unicode_columns_and_compact_record_constructor() -> None:
    result = extract_java(
        "User.java", "record 使用者(String name) {\n  使用者 { }\n}\n", max_symbols=100
    )
    assert result.parse_status == "parsed"
    record, constructor = result.symbols
    assert (record.name, record.start_line, record.end_line) == ("使用者", 1, 3)
    assert (constructor.kind, constructor.parent_local_id, constructor.start_column) == (
        "constructor",
        record.local_id,
        2,
    )
    assert constructor.end_column == len("  使用者 { }".encode())
