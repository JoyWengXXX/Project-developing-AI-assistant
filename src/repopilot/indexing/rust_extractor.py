"""Rust 語法層級抽取器；不執行程式或展開巨集。"""

from functools import lru_cache

import tree_sitter_rust
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
    "mod_item": "module",
    "struct_item": "struct",
    "enum_item": "enum",
    "enum_variant": "enum_member",
    "trait_item": "interface",
    "function_item": "function",
    "function_signature_item": "method",
}


@lru_cache(maxsize=1)
def _parser() -> Parser:
    return Parser(TreeLanguage(tree_sitter_rust.language()))


def _text(node: Node | None) -> str:
    return "" if node is None or node.text is None else node.text.decode("utf-8", "replace")


def extract_rust(path: str, content: str, *, max_symbols: int) -> FileExtraction:
    if not path.lower().endswith(".rs"):
        raise ValueError("Unsupported Rust extension")
    root = _parser().parse(content.encode("utf-8")).root_node
    walker = _Walker(max_symbols)
    try:
        walker.visit(root, None)
    except RecursionError:
        return FileExtraction("rust", "failed", "too_deep", 1)
    count, first = find_errors(root) if root.has_error else (0, None)
    return FileExtraction(
        language="rust",
        parse_status="parsed_with_errors" if count else "parsed",
        error_kind="tree_error" if count else None,
        error_count=count,
        first_error_line=first[0] if first else None,
        first_error_column=first[1] if first else None,
        symbols_truncated=walker.truncated,
        symbols=walker.symbols,
        imports=walker.imports,
    )


class _Walker:
    def __init__(self, max_symbols: int) -> None:
        self.max_symbols = max_symbols
        self.symbols: list[ExtractedSymbol] = []
        self.imports: list[ExtractedImport] = []
        self.truncated = False

    def visit(self, node: Node, parent: int | None) -> None:
        if node.type in {"source_file", "declaration_list", "enum_variant_list"}:
            for child in node.named_children:
                self.visit(child, parent)
        elif node.type == "impl_item":
            body = node.child_by_field_name("body")
            if body is not None:
                self.visit(body, parent)
        elif node.type == "use_declaration":
            argument = _text(node.child_by_field_name("argument"))
            if argument:
                self.imports.append(
                    ExtractedImport(
                        parent,
                        "rust_use",
                        argument[:MAX_NAME_CHARS],
                        None,
                        None,
                        None,
                        node.start_point.row + 1,
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
            kind = _KINDS[node.type]
            if node.type in {"function_item", "function_signature_item"}:
                parameters = node.child_by_field_name("parameters")
                has_self = parameters is not None and any(
                    child.type == "self_parameter" for child in parameters.named_children
                )
                kind = "method" if has_self else "function"
            qualified = name if parent is None else f"{self.symbols[parent].qualified_name}.{name}"
            end_line = node.end_point.row + 1
            if node.end_point.column == 0 and node.end_point.row > node.start_point.row:
                end_line -= 1
            self.symbols.append(
                ExtractedSymbol(
                    local_id=local,
                    parent_local_id=parent,
                    kind=kind,
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
            if body is not None and node.type in {"mod_item", "enum_item", "trait_item"}:
                self.visit(body, local)
