"""C# 結構抽取器（tree-sitter，語法層級，規格 7.3）。

純函式：`extract_csharp(path, content, max_symbols=…, include_fields=…)` 把一個檔案的文字內容轉成
符號、`using` 與關係，不碰資料庫、不執行受分析的程式、不呼叫 MSBuild 或 .NET SDK。

重點（細節見規格 7.1、7.3 與 architecture D46）：

- **語法層級，不含 Roslyn 語意**：型別推斷、多載解析、跨檔參考、partial 合併、`#if` 條件求值、
  source generator 全都不做。基底型別只做同檔、以名稱精確比對的解析，其餘 `unresolved`。
- **容錯**：tree-sitter 在語法錯誤時仍產生樹。根節點有錯誤時，檔案標為 `parsed_with_errors`／
  `tree_error`，記錄錯誤節點數與第一個錯誤的位置；子樹含錯誤的符號標 `has_errors`——這些符號
  **可能不正確，標示而不隱藏**。一律稱「部分解析」，不宣稱完整。
- **位置**：行號 1-based、起訖行皆包含；欄位是從行首起算的 UTF-8 位元組數，**含 BOM 位元組**
  （tree-sitter 本來就是這樣算）。tree-sitter 只把 `\\n` 當換行：單獨的 `\\r` 不換行（與 Python 的
  `ast` 不同，測試固定這個行為）。
- **file-scoped namespace**：`namespace X;` 的節點只涵蓋那一行，其後的頂層宣告是 `compilation_unit`
  的同層兄弟，所以走訪時記住目前的 file-scoped namespace 並套用到之後的兄弟；它的 `end_line` 取檔案
  最後一行（語意範圍，不是節點範圍）。
- **`#if`**：文法把各分支都解析成節點，**不求值條件**；分支裡的符號標 `conditional`，可能出現重複的
  `qualified_name`。
- **partial**：同一型別在多個檔案各自記錄、不合併，`modifiers` 含 `partial`。
- **不抽取（v1）**：解構函式、事件、索引子、運算子與轉換運算子、區域函式、lambda、屬性存取子、頂層
  陳述式、`extern alias`、`global` 屬性。欄位預設不抽取（`include_fields`）。
- 屬性（attribute）只存名稱，不存引數；不保存簽章、型別參數文字或約束。

.NET 對照：像用 Roslyn 的 `CSharpSyntaxTree.ParseText` 取宣告節點，但 tree-sitter 沒有語意模型，
也沒有 `SemanticModel`；它換來的是不需要 .NET SDK、對不完整的程式碼也能產生樹。
"""

from functools import lru_cache

import tree_sitter_c_sharp
from tree_sitter import Language, Node, Parser

from repopilot.indexing.types import (
    MAX_NAME_CHARS,
    ExtractedImport,
    ExtractedRelation,
    ExtractedSymbol,
    FileExtraction,
    ImportKind,
    SymbolKind,
)

_TYPE_NODES: dict[str, SymbolKind] = {
    "class_declaration": "class",
    "struct_declaration": "struct",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
}
_MEMBER_NODES: dict[str, SymbolKind] = {
    "method_declaration": "method",
    "constructor_declaration": "constructor",
    "property_declaration": "property",
    "delegate_declaration": "delegate",
    "enum_member_declaration": "enum_member",
}
# 這些節點本身不是宣告，但可能在裡面包著宣告（`#if` 分支）
_PREPROCESSOR_NODES = frozenset({"preproc_if", "preproc_elif", "preproc_else"})
MAX_ERROR_COUNT = 1000


@lru_cache(maxsize=1)
def _parser() -> Parser:
    return Parser(Language(tree_sitter_c_sharp.language()))


def extract_csharp(
    path: str, content: str, *, max_symbols: int, include_fields: bool = False
) -> FileExtraction:
    data = content.encode("utf-8")
    root = _parser().parse(data).root_node
    walker = _Walker(data, max_symbols, include_fields)
    try:
        walker.visit_container(root, None, False)
        walker.resolve_bases()
    except RecursionError:
        return FileExtraction("csharp", "failed", "too_deep", 1)
    count, first = _find_errors(root) if root.has_error else (0, None)
    return FileExtraction(
        "csharp",
        "parsed_with_errors" if count else "parsed",
        "tree_error" if count else None,
        count,
        first[0] if first else None,
        first[1] if first else None,
        None,
        walker.truncated,
        walker.symbols,
        walker.imports,
        walker.relations,
    )


def _find_errors(root: Node) -> tuple[int, tuple[int, int] | None]:
    """最上層錯誤節點數與第一個錯誤的 (行, 欄)；只進入 `has_error` 的子樹，不遞迴。"""
    count = 0
    first: tuple[int, int] | None = None
    stack = [root]
    while stack and count < MAX_ERROR_COUNT:
        node = stack.pop()
        if node.is_error or node.is_missing:
            count += 1
            point = (node.start_point.row + 1, node.start_point.column)
            if first is None or point < first:
                first = point
        elif node.has_error:
            stack.extend(node.children)
    if count == 0:
        # 根節點說有錯誤但找不到錯誤節點：仍算一個，位置用根節點
        return 1, (root.start_point.row + 1, root.start_point.column)
    return count, first


def _text(node: Node | None) -> str:
    return "" if node is None or node.text is None else node.text.decode("utf-8", "replace")


def _compact(text: str) -> str:
    """去掉空白與換行（`A . B` 與 `A.B` 是同一個名稱）。"""
    return "".join(text.split())


class _Walker:
    def __init__(self, data: bytes, max_symbols: int, include_fields: bool) -> None:
        self._max_symbols = max_symbols
        self._include_fields = include_fields
        self._lines = data.split(b"\n")
        self.symbols: list[ExtractedSymbol] = []
        self.imports: list[ExtractedImport] = []
        self.relations: list[ExtractedRelation] = []
        self.truncated = False
        # (來源型別 local_id, 基底的原始文字, 用來比對的簡單名稱或 None, 行號)
        self._pending_bases: list[tuple[int, str, str | None, int]] = []

    # --- 符號建立 ---

    def _add(
        self,
        node: Node,
        kind: SymbolKind,
        parent: int | None,
        name: str,
        qualified_name: str,
        conditional: bool,
        *,
        modifiers: str | None = None,
        is_async: bool = False,
        type_parameter_count: int = 0,
        leading_start_line: int | None = None,
        end_line: int | None = None,
        end_column: int | None = None,
    ) -> int | None:
        if len(self.symbols) >= self._max_symbols:
            self.truncated = True
            return None
        end_row = node.end_point.row + 1
        end_col = node.end_point.column
        # 結束點落在下一行的行首（節點含結尾換行）：維持「含」語意，結束行取上一行
        if end_col == 0 and node.end_point.row > node.start_point.row:
            end_row = node.end_point.row
            end_col = (
                len(self._lines[end_row - 1].rstrip(b"\r")) if end_row <= len(self._lines) else 0
            )
        local_id = len(self.symbols)
        self.symbols.append(
            ExtractedSymbol(
                local_id=local_id,
                parent_local_id=parent,
                kind=kind,
                name=name,
                qualified_name=qualified_name,
                start_line=node.start_point.row + 1,
                end_line=end_line if end_line is not None else end_row,
                start_column=node.start_point.column,
                end_column=end_column if end_column is not None else end_col,
                leading_start_line=leading_start_line,
                modifiers=modifiers,
                is_async=is_async,
                type_parameter_count=type_parameter_count,
                has_errors=node.has_error,
                conditional=conditional,
            )
        )
        return local_id

    def _qualified(self, parent: int | None, name: str) -> str:
        return name if parent is None else f"{self.symbols[parent].qualified_name}.{name}"

    # --- 走訪 ---

    def visit_container(self, container: Node, parent: int | None, conditional: bool) -> None:
        """走訪 compilation_unit、namespace 本體、型別本體的子節點。"""
        file_scoped = parent
        for child in container.children:
            if self.truncated:
                return
            kind = child.type
            if kind == "file_scoped_namespace_declaration":
                last_row = len(self._lines)
                if self._lines and self._lines[-1] == b"":
                    last_row -= 1  # 結尾換行之後的空行不算
                last = self._lines[max(last_row, 1) - 1].rstrip(b"\r")
                file_scoped = self._namespace(
                    child, parent, conditional, end_line=max(last_row, 1), end_column=len(last)
                )
                continue
            self._visit_child(child, file_scoped, conditional)

    def _visit_child(self, child: Node, parent: int | None, conditional: bool) -> None:
        kind = child.type
        if kind == "using_directive":
            self._using(child, parent)
        elif kind == "namespace_declaration":
            self._namespace(child, parent, conditional)
        elif kind in _TYPE_NODES:
            self._type(child, parent, conditional)
        elif kind in _MEMBER_NODES:
            self._member(child, parent, conditional)
        elif kind == "field_declaration":
            if self._include_fields:
                self._fields(child, parent, conditional)
        elif kind in _PREPROCESSOR_NODES:
            # `#if`／`#elif`／`#else` 各分支都解析：不求值條件，裡面的符號標 conditional
            for inner in child.children:
                self._visit_child(inner, parent, True)

    def _namespace(
        self,
        node: Node,
        parent: int | None,
        conditional: bool,
        *,
        end_line: int | None = None,
        end_column: int | None = None,
    ) -> int | None:
        name = _compact(_text(node.child_by_field_name("name")))
        if not name:
            return None
        local_id = self._add(
            node,
            "namespace",
            parent,
            name,
            self._qualified(parent, name),
            conditional,
            end_line=end_line,
            end_column=end_column,
        )
        body = node.child_by_field_name("body")
        if local_id is not None and body is not None:
            self.visit_container(body, local_id, conditional)
        return local_id

    def _modifiers(self, node: Node) -> tuple[str | None, bool]:
        names = {_text(c) for c in node.children if c.type == "modifier"}
        is_async = "async" in names
        if node.type == "record_declaration" and any(c.type == "struct" for c in node.children):
            names.add("struct")  # record struct：kind 仍是 record，用修飾詞區分
        return (" ".join(sorted(names)) or None), is_async

    def _type_parameter_count(self, node: Node) -> int:
        params = node.child_by_field_name("type_parameters")
        return (
            0 if params is None else sum(1 for c in params.children if c.type == "type_parameter")
        )

    def _attributes(self, node: Node) -> tuple[int | None, list[tuple[str, int]]]:
        leading: int | None = None
        found: list[tuple[str, int]] = []
        for child in node.children:
            if child.type != "attribute_list":
                continue
            if leading is None:
                leading = child.start_point.row + 1
            for attribute in child.children:
                if attribute.type == "attribute":
                    name = _compact(_text(attribute.child_by_field_name("name")))
                    if name:
                        found.append((name[:MAX_NAME_CHARS], attribute.start_point.row + 1))
        return leading, found

    def _declare(
        self, node: Node, kind: SymbolKind, parent: int | None, name: str, conditional: bool
    ) -> int | None:
        modifiers, is_async = self._modifiers(node)
        leading, attributes = self._attributes(node)
        local_id = self._add(
            node,
            kind,
            parent,
            name,
            self._qualified(parent, name),
            conditional,
            modifiers=modifiers,
            is_async=is_async,
            type_parameter_count=self._type_parameter_count(node),
            leading_start_line=leading,
        )
        if local_id is not None:
            for attribute_name, line in attributes:
                self.relations.append(
                    ExtractedRelation(
                        local_id, "attribute", attribute_name, None, "unresolved", line
                    )
                )
        return local_id

    def _type(self, node: Node, parent: int | None, conditional: bool) -> None:
        name = _text(node.child_by_field_name("name"))
        if not name:
            return
        local_id = self._declare(node, _TYPE_NODES[node.type], parent, name, conditional)
        if local_id is None:
            return
        for child in node.children:
            if child.type == "base_list":
                self._bases(child, local_id)
        body = node.child_by_field_name("body")
        if body is not None:
            self.visit_container(body, local_id, conditional)

    def _bases(self, base_list: Node, source: int) -> None:
        for child in base_list.named_children:
            target = child
            if child.type == "primary_constructor_base_type":
                inner = child.child_by_field_name("type")
                if inner is None:
                    continue
                target = inner
            text = _compact(_text(target))
            if not text:
                continue
            simple: str | None = None
            if target.type == "identifier":
                simple = text
            elif target.type == "generic_name" and target.children:
                simple = _text(target.children[0])
            self._pending_bases.append(
                (source, text[:MAX_NAME_CHARS], simple, child.start_point.row + 1)
            )

    def _member(self, node: Node, parent: int | None, conditional: bool) -> None:
        name = _text(node.child_by_field_name("name"))
        if name:
            self._declare(node, _MEMBER_NODES[node.type], parent, name, conditional)

    def _fields(self, node: Node, parent: int | None, conditional: bool) -> None:
        modifiers, _ = self._modifiers(node)
        leading, _attributes = self._attributes(node)
        for declaration in node.children:
            if declaration.type != "variable_declaration":
                continue
            for declarator in declaration.children:
                if declarator.type != "variable_declarator":
                    continue
                name = _text(declarator.child_by_field_name("name"))
                if name:
                    self._add(
                        declarator,
                        "field",
                        parent,
                        name,
                        self._qualified(parent, name),
                        conditional,
                        modifiers=modifiers,
                        leading_start_line=leading,
                    )

    def _using(self, node: Node, source: int | None) -> None:
        named = list(node.named_children)
        if not named:
            return
        tokens = {c.type for c in node.children if not c.is_named}
        alias: str | None = None
        target_node = named[-1]
        kind: ImportKind = "using"
        if "=" in tokens and len(named) >= 2:
            alias = _text(named[0])
            kind = "using_alias"
        elif "static" in tokens:
            kind = "using_static"
        if "global" in tokens:
            kind = "global_using"
        self.imports.append(
            ExtractedImport(
                source,
                kind,
                _compact(_text(target_node))[:MAX_NAME_CHARS],
                None,
                alias,
                None,
                node.start_point.row + 1,
            )
        )

    # --- 基底型別的同檔解析 ---

    def resolve_bases(self) -> None:
        types_by_name: dict[str, list[int]] = {}
        for symbol in self.symbols:
            if symbol.kind in ("class", "struct", "interface", "enum", "record"):
                types_by_name.setdefault(symbol.name, []).append(symbol.local_id)
        for source, text, simple, line in self._pending_bases:
            candidates = [c for c in types_by_name.get(simple, []) if c != source] if simple else []
            if len(candidates) == 1:
                self.relations.append(
                    ExtractedRelation(
                        source, "base_type", text, candidates[0], "resolved_same_file", line
                    )
                )
            else:
                self.relations.append(
                    ExtractedRelation(
                        source,
                        "base_type",
                        text,
                        None,
                        "ambiguous" if len(candidates) > 1 else "unresolved",
                        line,
                    )
                )
