"""Java 語法層級抽取器；不執行程式或解析跨檔關係。"""

from functools import lru_cache

import tree_sitter_java
from tree_sitter import Language as TreeLanguage
from tree_sitter import Node, Parser

from repopilot.indexing.tree_utils import find_errors
from repopilot.indexing.types import (
    MAX_NAME_CHARS,
    ExtractedImport,
    ExtractedSymbol,
    FileExtraction,
    SymbolKind,
)

_KINDS: dict[str, SymbolKind] = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "enum_constant": "enum_member",
    "record_declaration": "record",
    "constructor_declaration": "constructor",
    "compact_constructor_declaration": "constructor",
    "method_declaration": "method",
}


@lru_cache(maxsize=1)
def _parser() -> Parser:
    return Parser(TreeLanguage(tree_sitter_java.language()))


def _text(node: Node | None) -> str:
    return "" if node is None or node.text is None else node.text.decode("utf-8", "replace")


def extract_java(path: str, content: str, *, max_symbols: int) -> FileExtraction:
    if not path.lower().endswith(".java"):
        raise ValueError("Unsupported Java extension")
    root = _parser().parse(content.encode("utf-8")).root_node
    walker = _Walker(max_symbols)
    try:
        walker.visit(root, None)
    except RecursionError:
        return FileExtraction("java", "failed", "too_deep", 1)
    count, first = find_errors(root) if root.has_error else (0, None)
    return FileExtraction(
        language="java",
        parse_status="parsed_with_errors" if count else "parsed",
        error_kind="tree_error" if count else None,
        error_count=count,
        first_error_line=first[0] if first else None,
        first_error_column=first[1] if first else None,
        module_name=walker.package or None,
        symbols_truncated=walker.truncated,
        symbols=walker.symbols,
        imports=walker.imports,
    )


class _Walker:
    def __init__(self, max_symbols: int) -> None:
        self.max_symbols = max_symbols
        self.symbols: list[ExtractedSymbol] = []
        self.imports: list[ExtractedImport] = []
        self.package = ""
        self.truncated = False

    def visit(self, node: Node, parent: int | None) -> None:
        if node.type in {
            "program",
            "class_body",
            "interface_body",
            "enum_body",
            "enum_body_declarations",
        }:
            for child in node.named_children:
                self.visit(child, parent)
        elif node.type == "package_declaration":
            name_node = next(
                (
                    child
                    for child in node.named_children
                    if child.type in {"identifier", "scoped_identifier"}
                ),
                None,
            )
            self.package = _text(name_node)[:MAX_NAME_CHARS]
        elif node.type == "import_declaration":
            target = next(
                (
                    child
                    for child in node.named_children
                    if child.type in {"identifier", "scoped_identifier"}
                ),
                None,
            )
            if target is not None:
                suffix = (
                    ".*" if any(child.type == "asterisk" for child in node.named_children) else ""
                )
                self.imports.append(
                    ExtractedImport(
                        source_local_id=None,
                        kind="java_static_import"
                        if any(child.type == "static" for child in node.children)
                        else "java_import",
                        target=(_text(target) + suffix)[:MAX_NAME_CHARS],
                        imported_name=None,
                        alias=None,
                        relative_level=None,
                        line=node.start_point.row + 1,
                    )
                )
        elif node.type in _KINDS:
            name = _text(node.child_by_field_name("name"))
            if not name:
                return
            if len(self.symbols) >= self.max_symbols:
                self.truncated = True
                return
            local = len(self.symbols)
            qualified = name if parent is None else f"{self.symbols[parent].qualified_name}.{name}"
            end_line = node.end_point.row + 1
            if node.end_point.column == 0 and node.end_point.row > node.start_point.row:
                end_line -= 1
            self.symbols.append(
                ExtractedSymbol(
                    local_id=local,
                    parent_local_id=parent,
                    kind=_KINDS[node.type],
                    name=name,
                    qualified_name=qualified,
                    start_line=node.start_point.row + 1,
                    end_line=end_line,
                    start_column=node.start_point.column,
                    end_column=node.end_point.column,
                    has_errors=node.has_error,
                )
            )
            body = node.child_by_field_name("body")
            if body is not None and node.type not in {
                "constructor_declaration",
                "compact_constructor_declaration",
                "method_declaration",
            }:
                self.visit(body, local)
