"""Python 結構抽取器（`ast`，規格 7.2）。

純函式：`extract_python(path, content, max_symbols=…)` 把一個檔案的文字內容轉成符號、import 與
關係，不碰資料庫、不 import／執行／編譯受分析的程式（只用 `ast.parse`，即 `PyCF_ONLY_AST`）。

重點（細節見規格 7.1、7.2）：

- `ast` 是全有或全無：`SyntaxError` 沒有 AST，所以整個檔案 `failed`／`syntax_error`，沒有任何符號；
  不保存錯誤訊息或程式片段，只存位置。
- 位置：行號 1-based、起訖行皆包含；欄位是 UTF-8 位元組位移（`ast` 本來就是），且以**儲存內容為準、
  含 BOM**。`ast.parse` 拒絕含 BOM 的 `str`，所以解析前去除 BOM，再把第 1 行的欄位加 3。
- `module_name` 只由路徑推得，不偵測 source root，不是匯入系統的解析結果。
- 裝飾器只存被呼叫者名稱，不存引數；基底類別只存名稱（`resolved_same_file` 只比對同檔、
  同名且恰好一個的類別，其餘是 `ambiguous`／`unresolved`）。裝飾器一律 `unresolved`。
- 單檔符號數上限 `max_symbols`：達上限即停止走訪並設 `symbols_truncated`（之後的 import 與關係
  不再記錄），不使 job 失敗。
- 位於 `if`／`try`／`with`／`match`／迴圈內的定義標記 `conditional`，巢狀符號繼承父符號的標記。

.NET 對照：像用 Roslyn 的 `CSharpSyntaxTree.ParseText` 只取宣告節點，但 Python 的 `ast` 沒有
錯誤復原，語法錯誤時整棵樹都沒有。
"""

import ast
import re
import warnings

from repopilot.indexing.types import (
    MAX_NAME_CHARS,
    ExtractedImport,
    ExtractedRelation,
    ExtractedSymbol,
    FileExtraction,
    RelationType,
    SymbolKind,
)

_BOM = "\ufeff"
_BOM_BYTES = len(_BOM.encode("utf-8"))
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_CONDITIONAL_BLOCKS = (
    ast.If,
    ast.Try,
    ast.TryStar,
    ast.With,
    ast.AsyncWith,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Match,
)


def module_name_from_path(path: str) -> str:
    """快照路徑 → 點分模組名稱（`src/pkg/a.py`→`src.pkg.a`，`pkg/__init__.py`→`pkg`）。

    不偵測 source root、不驗證是否為合法識別字；根目錄的 `__init__.py` 沒有套件路徑，退回
    `__init__`。
    """
    parts = [p for p in path.split("/") if p]
    if parts:
        last = parts[-1]
        parts[-1] = last[:-3] if last.lower().endswith(".py") else last
    if parts and parts[-1] == "__init__" and len(parts) > 1:
        parts.pop()
    return ".".join(parts) or "__init__"


def extract_python(path: str, content: str, *, max_symbols: int) -> FileExtraction:
    module_name = module_name_from_path(path)
    has_bom = content.startswith(_BOM)
    source = content[1:] if has_bom else content
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # 例如無效逸出序列的 SyntaxWarning
            tree = ast.parse(source)
        walker = _Walker(content, module_name, has_bom, max_symbols)
        walker.run(tree)
    except RecursionError:
        return FileExtraction("python", "failed", "too_deep", 1, module_name=module_name)
    except (SyntaxError, ValueError) as exc:
        line, column = _error_position(source, exc, has_bom)
        return FileExtraction(
            "python", "failed", "syntax_error", 1, line, column, module_name=module_name
        )
    return FileExtraction(
        "python",
        "parsed",
        module_name=module_name,
        symbols_truncated=walker.truncated,
        symbols=walker.symbols,
        imports=walker.imports,
        relations=walker.relations,
    )


def _error_position(source: str, exc: Exception, has_bom: bool) -> tuple[int, int]:
    line = getattr(exc, "lineno", None)
    offset = getattr(exc, "offset", None)
    if not isinstance(line, int) or line < 1:
        return 1, 0
    column = 0
    if isinstance(offset, int) and offset > 1:
        lines = _LINE_BREAK.split(source)
        text = lines[line - 1] if line <= len(lines) else ""
        # SyntaxError.offset 是 1-based 的字元位移；轉成位元組位移以符合欄位語意
        column = len(text[: offset - 1].encode("utf-8"))
    if has_bom and line == 1:
        column += _BOM_BYTES
    return line, column


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted_name(node.value)
        return None if head is None else f"{head}.{node.attr}"
    return None


def _truncate(text: str) -> str:
    return text[:MAX_NAME_CHARS]


class _Walker:
    def __init__(self, content: str, module_name: str, has_bom: bool, max_symbols: int) -> None:
        self._content = content
        self._module_name = module_name
        self._bom_bytes = _BOM_BYTES if has_bom else 0
        self._max_symbols = max_symbols
        self.symbols: list[ExtractedSymbol] = []
        self.imports: list[ExtractedImport] = []
        self.relations: list[ExtractedRelation] = []
        self.truncated = False
        # (來源類別 local_id, 基底名稱, 是否為單一名稱, 行號)：走訪完才知道同檔有哪些類別
        self._pending_bases: list[tuple[int, str, bool, int]] = []

    def run(self, tree: ast.Module) -> None:
        module_id = self._add_module()
        if module_id is None:
            return
        self._visit_body(tree.body, module_id, False)
        self._resolve_bases()

    def _col(self, line: int, column: int) -> int:
        return column + self._bom_bytes if line == 1 else column

    def _add_module(self) -> int | None:
        parts = _LINE_BREAK.split(self._content)
        if len(parts) > 1 and parts[-1] == "":
            parts.pop()
        last = parts[-1]
        name = self._module_name.rsplit(".", 1)[-1]
        return self._add(
            kind="module",
            parent=None,
            name=name,
            qualified_name=self._module_name,
            start_line=1,
            end_line=len(parts),
            start_column=0,
            end_column=len(last.encode("utf-8")),
        )

    def _add(
        self,
        *,
        kind: SymbolKind,
        parent: int | None,
        name: str,
        qualified_name: str,
        start_line: int,
        end_line: int,
        start_column: int,
        end_column: int,
        leading_start_line: int | None = None,
        is_async: bool = False,
        type_parameter_count: int = 0,
        conditional: bool = False,
    ) -> int | None:
        if len(self.symbols) >= self._max_symbols:
            self.truncated = True
            return None
        local_id = len(self.symbols)
        self.symbols.append(
            ExtractedSymbol(
                local_id=local_id,
                parent_local_id=parent,
                kind=kind,
                name=name,
                qualified_name=qualified_name,
                start_line=start_line,
                end_line=end_line,
                start_column=start_column,
                end_column=end_column,
                leading_start_line=leading_start_line,
                is_async=is_async,
                type_parameter_count=type_parameter_count,
                conditional=conditional,
            )
        )
        return local_id

    def _visit_body(self, body: list[ast.stmt], parent: int, conditional: bool) -> None:
        for stmt in body:
            if self.truncated:
                return
            if isinstance(stmt, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                self._visit_definition(stmt, parent, conditional)
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    self.imports.append(
                        ExtractedImport(
                            parent,
                            "import",
                            _truncate(alias.name),
                            None,
                            alias.asname,
                            None,
                            stmt.lineno,
                        )
                    )
            elif isinstance(stmt, ast.ImportFrom):
                for alias in stmt.names:
                    self.imports.append(
                        ExtractedImport(
                            parent,
                            "from_import",
                            _truncate(stmt.module or ""),
                            alias.name,
                            alias.asname,
                            stmt.level,
                            stmt.lineno,
                        )
                    )
            elif isinstance(stmt, _CONDITIONAL_BLOCKS):
                for block in self._nested_blocks(stmt):
                    self._visit_body(block, parent, True)

    @staticmethod
    def _nested_blocks(stmt: ast.stmt) -> list[list[ast.stmt]]:
        if isinstance(stmt, ast.Match):
            return [case.body for case in stmt.cases]
        blocks: list[list[ast.stmt]] = []
        for attr in ("body", "orelse", "finalbody"):
            block = getattr(stmt, attr, None)
            if block:
                blocks.append(block)
        for handler in getattr(stmt, "handlers", []):
            blocks.append(handler.body)
        return blocks

    def _visit_definition(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        parent: int,
        conditional: bool,
    ) -> None:
        parent_symbol = self.symbols[parent]
        kind: SymbolKind
        if isinstance(node, ast.ClassDef):
            kind = "class"
        else:
            kind = "method" if parent_symbol.kind == "class" else "function"
        start_line = node.lineno
        end_line = node.end_lineno or start_line
        decorator_lines = [d.lineno for d in node.decorator_list]
        local_id = self._add(
            kind=kind,
            parent=parent,
            name=node.name,
            qualified_name=f"{parent_symbol.qualified_name}.{node.name}",
            start_line=start_line,
            end_line=end_line,
            start_column=self._col(start_line, node.col_offset),
            end_column=self._col(end_line, node.end_col_offset or 0),
            leading_start_line=min(decorator_lines) if decorator_lines else None,
            is_async=isinstance(node, ast.AsyncFunctionDef),
            type_parameter_count=len(node.type_params),
            conditional=conditional or parent_symbol.conditional,
        )
        if local_id is None:
            return
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = _dotted_name(base)
                simple = isinstance(base, ast.Name)
                self._pending_bases.append(
                    (
                        local_id,
                        _truncate(name if name is not None else ast.unparse(base)),
                        simple,
                        base.lineno,
                    )
                )
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            name = _dotted_name(target)
            self._add_relation(
                local_id,
                "decorator",
                name if name is not None else ast.unparse(target),
                decorator.lineno,
            )
        self._visit_body(node.body, local_id, self.symbols[local_id].conditional)

    def _add_relation(
        self,
        source: int,
        relation_type: RelationType,
        target_name: str,
        line: int,
        target_local_id: int | None = None,
    ) -> None:
        self.relations.append(
            ExtractedRelation(
                source_local_id=source,
                relation_type=relation_type,
                target_name=_truncate(target_name),
                target_local_id=target_local_id,
                resolution="resolved_same_file" if target_local_id is not None else "unresolved",
                line=line,
            )
        )

    def _resolve_bases(self) -> None:
        classes_by_name: dict[str, list[int]] = {}
        for symbol in self.symbols:
            if symbol.kind == "class":
                classes_by_name.setdefault(symbol.name, []).append(symbol.local_id)
        for source, name, simple, line in self._pending_bases:
            # 排除自己：`from x import Foo` 之後的 `class Foo(Foo)` 指的不是它自己
            candidates = [c for c in classes_by_name.get(name, []) if c != source] if simple else []
            if len(candidates) == 1:
                self._add_relation(source, "base_class", name, line, candidates[0])
            else:
                self.relations.append(
                    ExtractedRelation(
                        source_local_id=source,
                        relation_type="base_class",
                        target_name=name,
                        target_local_id=None,
                        resolution="ambiguous" if len(candidates) > 1 else "unresolved",
                        line=line,
                    )
                )
