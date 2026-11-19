import pytest

from repopilot.indexing.script_extractor import extract_script
from repopilot.indexing.types import FileExtraction


def _extract(path: str, source: str, max_symbols: int = 100) -> FileExtraction:
    return extract_script(path, source, max_symbols=max_symbols)


@pytest.mark.parametrize("extension", [".ts", ".mts", ".cts", ".tsx"])
def test_typescript_declarations_imports_and_positions(extension: str) -> None:
    source = (
        'import Service, { helper as assist } from "pkg";\n'
        "export interface User { name: string }\n"
        "export type UserId = string;\n"
        "export enum Status { Active, Disabled }\n"
        "export class Auth {\n"
        "  constructor() {}\n"
        "  async login(): Promise<void> {}\n"
        "}\n"
        "export const verify = (value: string) => value;\n"
        "export function greet(name: string) { return name; }\n"
    )
    result = _extract(f"src/auth{extension}", source)
    symbols = {symbol.qualified_name: symbol for symbol in result.symbols}
    assert result.language == "typescript" and result.parse_status == "parsed"
    assert {name: symbols[name].kind for name in symbols} == {
        "User": "interface",
        "UserId": "type_alias",
        "Status": "enum",
        "Status.Active": "enum_member",
        "Status.Disabled": "enum_member",
        "Auth": "class",
        "Auth.constructor": "constructor",
        "Auth.login": "method",
        "verify": "function",
        "greet": "function",
    }
    assert symbols["Auth.login"].is_async
    assert symbols["Auth.login"].parent_local_id == symbols["Auth"].local_id
    assert (symbols["Auth"].start_line, symbols["Auth"].end_line) == (5, 8)
    assert {(item.target, item.imported_name, item.alias) for item in result.imports} == {
        ("pkg", "default", "Service"),
        ("pkg", "helper", "assist"),
    }


@pytest.mark.parametrize("extension", [".js", ".mjs", ".cjs", ".jsx"])
def test_javascript_functions_class_and_static_import(extension: str) -> None:
    result = _extract(
        f"src/app{extension}",
        'import "polyfill";\n'
        "export function greet(name) { return name; }\n"
        "export const render = () => <div>Hello</div>;\n"
        "class Widget { constructor() {} show() {} }\n"
        if extension == ".jsx"
        else 'import "polyfill";\n'
        "export function greet(name) { return name; }\n"
        "export const render = () => 1;\n"
        "class Widget { constructor() {} show() {} }\n",
    )
    symbols = {symbol.qualified_name: symbol.kind for symbol in result.symbols}
    assert result.language == "javascript" and result.parse_status == "parsed"
    assert symbols == {
        "greet": "function",
        "render": "function",
        "Widget": "class",
        "Widget.constructor": "constructor",
        "Widget.show": "method",
    }
    assert [(item.target, item.imported_name) for item in result.imports] == [("polyfill", None)]


def test_tsx_component_and_syntax_error_are_reported() -> None:
    good = _extract("Card.tsx", "export const Card = () => <div>Hello</div>;\n")
    broken = _extract("broken.ts", "function fine() {}\nfunction bad( {\n")
    assert good.parse_status == "parsed"
    assert [(symbol.name, symbol.kind) for symbol in good.symbols] == [("Card", "function")]
    assert broken.parse_status == "parsed_with_errors"
    assert broken.error_kind == "tree_error" and broken.error_count > 0
    assert broken.first_error_line is not None
    assert any(symbol.name == "fine" for symbol in broken.symbols)


def test_symbol_limit_marks_truncation() -> None:
    result = _extract("app.js", "function one() {}\nfunction two() {}\n", max_symbols=1)
    assert [symbol.name for symbol in result.symbols] == ["one"]
    assert result.symbols_truncated


def test_async_arrow_nested_function_and_unicode_positions() -> None:
    result = _extract(
        "app.ts",
        "export const 登入 = async () => {\n"
        "  function check() { return true; }\n"
        "  return check();\n"
        "};\n",
    )
    symbols = {symbol.qualified_name: symbol for symbol in result.symbols}
    assert result.parse_status == "parsed"
    assert symbols["登入"].is_async
    assert symbols["登入.check"].parent_local_id == symbols["登入"].local_id
    assert symbols["登入.check"].start_line == 2


def test_unknown_extension_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported script extension"):
        _extract("example.txt", "function example() {}")
