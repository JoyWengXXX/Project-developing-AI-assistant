"""TypeScript／JavaScript 語法層級抽取器；不執行程式或解析跨檔關係。"""

from functools import lru_cache
from typing import Literal

import tree_sitter_javascript
import tree_sitter_typescript
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

ScriptLanguage = Literal["typescript", "javascript"]
_DECLARATIONS: dict[str, SymbolKind] = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "type_alias_declaration": "type_alias",
    "enum_declaration": "enum",
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "function_signature": "function",
}


@lru_cache(maxsize=3)
def _parser(grammar: str) -> Parser:
    if grammar == "typescript":
        language = tree_sitter_typescript.language_typescript()
    elif grammar == "tsx":
        language = tree_sitter_typescript.language_tsx()
    elif grammar == "javascript":
        language = tree_sitter_javascript.language()
    else:
        raise ValueError(f"Unknown grammar: {grammar}")
    return Parser(TreeLanguage(language))


def _text(node: Node | None) -> str:
    return "" if node is None or node.text is None else node.text.decode("utf-8", "replace")


def extract_script(path: str, content: str, *, max_symbols: int) -> FileExtraction:
    lowered = path.lower()
    if lowered.endswith((".ts", ".tsx", ".mts", ".cts")):
        language: ScriptLanguage = "typescript"
    elif lowered.endswith((".js", ".jsx", ".mjs", ".cjs")):
        language = "javascript"
    else:
        raise ValueError("Unsupported script extension")
    grammar = "tsx" if lowered.endswith(".tsx") else language
    root = _parser(grammar).parse(content.encode("utf-8")).root_node
    walker = _Walker(max_symbols)
    try:
        walker.visit(root, None)
    except RecursionError:
        return FileExtraction(language, "failed", "too_deep", 1)
    count, first = find_errors(root) if root.has_error else (0, None)
    return FileExtraction(
        language=language,
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

    def add(
        self, node: Node, kind: SymbolKind, parent: int | None, name: str, *, is_async: bool = False
    ) -> int | None:
        if len(self.symbols) >= self.max_symbols:
            self.truncated = True
            return None
        qualified = name if parent is None else f"{self.symbols[parent].qualified_name}.{name}"
        local_id = len(self.symbols)
        end_line = node.end_point.row + 1
        if node.end_point.column == 0 and node.end_point.row > node.start_point.row:
            end_line -= 1
        self.symbols.append(
            ExtractedSymbol(
                local_id=local_id,
                parent_local_id=parent,
                kind=kind,
                name=name,
                qualified_name=qualified,
                start_line=node.start_point.row + 1,
                end_line=end_line,
                start_column=node.start_point.column,
                end_column=node.end_point.column,
                is_async=is_async or any(child.type == "async" for child in node.children),
                has_errors=node.has_error,
            )
        )
        return local_id

    def visit(self, node: Node, parent: int | None) -> None:
        if node.type in {
            "program",
            "export_statement",
            "ambient_declaration",
            "statement_block",
            "if_statement",
            "else_clause",
            "for_statement",
            "for_in_statement",
            "while_statement",
            "try_statement",
            "catch_clause",
            "finally_clause",
        }:
            for child in node.named_children:
                self.visit(child, parent)
        elif node.type == "import_statement":
            self.import_statement(node)
        elif node.type in _DECLARATIONS:
            name_node = node.child_by_field_name("name")
            name = _text(name_node)
            if not name:
                return
            local = self.add(node, _DECLARATIONS[node.type], parent, name)
            if local is None:
                return
            body = node.child_by_field_name("body")
            if body is not None:
                for child in body.named_children:
                    self.visit(child, local)
        elif node.type in {"method_definition", "method_signature"}:
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.type not in {
                "property_identifier",
                "identifier",
                "private_property_identifier",
            }:
                return
            name = _text(name_node)
            local = self.add(
                node, "constructor" if name == "constructor" else "method", parent, name
            )
            body = node.child_by_field_name("body")
            if local is not None and body is not None:
                for child in body.named_children:
                    self.visit(child, local)
        elif node.type in {"lexical_declaration", "variable_declaration"}:
            for child in node.named_children:
                if child.type != "variable_declarator":
                    continue
                variable_name = child.child_by_field_name("name")
                value = child.child_by_field_name("value")
                if (
                    variable_name is not None
                    and variable_name.type == "identifier"
                    and value is not None
                    and value.type
                    in {"arrow_function", "function_expression", "generator_function"}
                ):
                    local = self.add(
                        child,
                        "function",
                        parent,
                        _text(variable_name),
                        is_async=any(part.type == "async" for part in value.children),
                    )
                    body = value.child_by_field_name("body")
                    if local is not None and body is not None and body.type == "statement_block":
                        for nested in body.named_children:
                            self.visit(nested, local)
        elif node.type in {"property_identifier", "enum_assignment"} and parent is not None:
            name_node = node.child_by_field_name("name") if node.type == "enum_assignment" else node
            name = _text(name_node)
            if name:
                self.add(node, "enum_member", parent, name)

    def import_statement(self, node: Node) -> None:
        source = node.child_by_field_name("source")
        if source is None:
            return
        target = _text(source)[1:-1][:MAX_NAME_CHARS]
        line = node.start_point.row + 1
        clause = next(
            (child for child in node.named_children if child.type == "import_clause"), None
        )
        entries: list[tuple[str | None, str | None]] = []
        if clause is not None:
            for child in clause.named_children:
                if child.type == "identifier":
                    entries.append(("default", _text(child)))
                elif child.type == "namespace_import":
                    entries.append(("*", _text(child.named_children[-1])))
                elif child.type == "named_imports":
                    for specifier in child.named_children:
                        imported_name = _text(specifier.child_by_field_name("name"))
                        alias = _text(specifier.child_by_field_name("alias")) or None
                        if imported_name:
                            entries.append((imported_name, alias))
        if not entries:
            entries.append((None, None))
        for name, alias in entries:
            self.imports.append(
                ExtractedImport(
                    source_local_id=None,
                    kind="script_import",
                    target=target,
                    imported_name=name[:MAX_NAME_CHARS] if name else None,
                    alias=alias[:MAX_NAME_CHARS] if alias else None,
                    relative_level=None,
                    line=line,
                )
            )
