"""Go 語法層級抽取器；不執行程式或解析跨檔關係。"""

from functools import lru_cache

import tree_sitter_go
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


@lru_cache(maxsize=1)
def _parser() -> Parser:
    return Parser(TreeLanguage(tree_sitter_go.language()))


def _text(node: Node | None) -> str:
    return "" if node is None or node.text is None else node.text.decode("utf-8", "replace")


def extract_go(path: str, content: str, *, max_symbols: int) -> FileExtraction:
    if not path.lower().endswith(".go"):
        raise ValueError("Unsupported Go extension")
    root = _parser().parse(content.encode("utf-8")).root_node
    walker = _Walker(max_symbols)
    try:
        walker.visit(root)
    except RecursionError:
        return FileExtraction("go", "failed", "too_deep", 1)
    count, first = find_errors(root) if root.has_error else (0, None)
    return FileExtraction(
        language="go",
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
        self.package = ""
        self.symbols: list[ExtractedSymbol] = []
        self.imports: list[ExtractedImport] = []
        self.truncated = False

    def visit(self, node: Node) -> None:
        if node.type in {
            "source_file",
            "type_declaration",
            "import_declaration",
            "import_spec_list",
        }:
            for child in node.named_children:
                self.visit(child)
        elif node.type == "package_clause":
            package_node = next(
                (child for child in node.named_children if child.type == "package_identifier"), None
            )
            self.package = _text(package_node)[:MAX_NAME_CHARS]
        elif node.type == "import_spec":
            path = _text(node.child_by_field_name("path"))
            if len(path) >= 2:
                self.imports.append(
                    ExtractedImport(
                        None,
                        "go_import",
                        path[1:-1][:MAX_NAME_CHARS],
                        None,
                        _text(node.child_by_field_name("name")) or None,
                        None,
                        node.start_point.row + 1,
                    )
                )
        elif node.type in {"type_spec", "function_declaration", "method_declaration"}:
            kind: SymbolKind
            if node.type == "type_spec":
                value = node.child_by_field_name("type")
                if value is None or value.type not in {"struct_type", "interface_type"}:
                    return
                kind = "struct" if value.type == "struct_type" else "interface"
            else:
                kind = "method" if node.type == "method_declaration" else "function"
            name = _text(node.child_by_field_name("name"))
            if not name:
                return
            if len(self.symbols) >= self.max_symbols:
                self.truncated = True
                return
            end_line = node.end_point.row + 1
            if node.end_point.column == 0 and node.end_point.row > node.start_point.row:
                end_line -= 1
            self.symbols.append(
                ExtractedSymbol(
                    local_id=len(self.symbols),
                    parent_local_id=None,
                    kind=kind,
                    name=name,
                    qualified_name=name,
                    start_line=node.start_point.row + 1,
                    end_line=end_line,
                    start_column=node.start_point.column,
                    end_column=node.end_point.column,
                    has_errors=node.has_error,
                )
            )
