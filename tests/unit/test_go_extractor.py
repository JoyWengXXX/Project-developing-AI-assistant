from repopilot.indexing.go_extractor import extract_go


def test_go_declarations_imports_and_positions() -> None:
    source = (
        "package demo\n"
        'import "fmt"\n'
        'import ( alias "net/http"; . "math"; _ "embed" )\n'
        "type User struct { Name string }\n"
        "type Worker interface { Run() }\n"
        "func Run() {}\n"
        "func (u *User) Hello() {}\n"
    )
    result = extract_go("main.go", source, max_symbols=100)
    assert result.parse_status == "parsed"
    assert result.module_name == "demo"
    assert [(s.name, s.kind, s.start_line) for s in result.symbols] == [
        ("User", "struct", 4),
        ("Worker", "interface", 5),
        ("Run", "function", 6),
        ("Hello", "method", 7),
    ]
    assert [(i.target, i.alias) for i in result.imports] == [
        ("fmt", None),
        ("net/http", "alias"),
        ("math", "."),
        ("embed", "_"),
    ]


def test_go_errors_and_symbol_limit() -> None:
    partial = extract_go("bad.go", "package demo\nfunc Good() {}\nfunc Bad( {\n", max_symbols=10)
    assert partial.parse_status == "parsed_with_errors"
    assert any(s.name == "Good" for s in partial.symbols)
    limited = extract_go("main.go", "package demo\nfunc A() {}\nfunc B() {}\n", max_symbols=1)
    assert [s.name for s in limited.symbols] == ["A"]
    assert limited.symbols_truncated
