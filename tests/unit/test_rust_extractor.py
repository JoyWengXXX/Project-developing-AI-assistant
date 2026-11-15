from repopilot.indexing.rust_extractor import extract_rust


def test_rust_declarations_use_and_positions() -> None:
    source = (
        "use std::io::{self, Read};\n"
        "mod outer {\n"
        "  pub struct Item { x: i32 }\n"
        "  enum State { Ready, Done }\n"
        "  trait Work { fn run(&self); }\n"
        "  impl Item { fn work(&self) {} }\n"
        "  pub fn make() {}\n"
        "}\n"
    )
    result = extract_rust("lib.rs", source, max_symbols=100)
    assert result.parse_status == "parsed"
    assert [(s.qualified_name, s.kind, s.start_line) for s in result.symbols] == [
        ("outer", "module", 2),
        ("outer.Item", "struct", 3),
        ("outer.State", "enum", 4),
        ("outer.State.Ready", "enum_member", 4),
        ("outer.State.Done", "enum_member", 4),
        ("outer.Work", "interface", 5),
        ("outer.Work.run", "method", 5),
        ("outer.work", "method", 6),
        ("outer.make", "function", 7),
    ]
    assert [(i.kind, i.target, i.line) for i in result.imports] == [
        ("rust_use", "std::io::{self, Read}", 1)
    ]


def test_rust_errors_and_symbol_limit() -> None:
    broken = extract_rust("bad.rs", "fn good() {}\nfn broken( {\n", max_symbols=100)
    assert broken.parse_status == "parsed_with_errors"
    assert any(symbol.name == "good" for symbol in broken.symbols)
    limited = extract_rust("lib.rs", "fn one() {}\nfn two() {}\n", max_symbols=1)
    assert [symbol.name for symbol in limited.symbols] == ["one"]
    assert limited.symbols_truncated


def test_rust_associated_functions_without_self_are_not_methods() -> None:
    result = extract_rust(
        "lib.rs",
        "trait Factory { fn create() -> Self; }\nimpl Factory for X { fn create() -> X {} }\n",
        max_symbols=100,
    )
    assert [(symbol.name, symbol.kind) for symbol in result.symbols] == [
        ("Factory", "interface"),
        ("create", "function"),
        ("create", "function"),
    ]
