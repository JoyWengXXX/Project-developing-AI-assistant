"""結構索引抽取器的輸出（純資料，不碰資料庫）。

符號以 `local_id`（在同一檔案內從 0 起算的序號）互相參照；寫入資料庫時由索引服務換成真正的
`symbols.id`。位置語意見規格 7.1：行號 1-based、起訖行皆包含；欄位是 0-based UTF-8 位元組位移，
以儲存內容為準（含 BOM 位元組）。
"""

from dataclasses import dataclass, field
from typing import Literal

Language = Literal["python", "csharp", "typescript", "javascript"]
ParseStatus = Literal["parsed", "parsed_with_errors", "failed"]
ErrorKind = Literal["syntax_error", "tree_error", "too_deep", "parser_crash"]
SymbolKind = Literal[
    "module",
    "namespace",
    "class",
    "struct",
    "record",
    "interface",
    "type_alias",
    "enum",
    "enum_member",
    "delegate",
    "function",
    "method",
    "constructor",
    "property",
    "field",
]
ImportKind = Literal[
    "import", "from_import", "using", "using_static", "using_alias", "global_using", "script_import"
]
RelationType = Literal["base_class", "base_type", "decorator", "attribute"]
Resolution = Literal["resolved_same_file", "ambiguous", "unresolved"]

# 名稱類欄位的截斷長度（規格 6.1：target、target_name 截斷 500 字元）
MAX_NAME_CHARS = 500


@dataclass(frozen=True)
class ExtractedSymbol:
    local_id: int
    parent_local_id: int | None
    kind: SymbolKind
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    leading_start_line: int | None = None
    modifiers: str | None = None
    is_async: bool = False
    type_parameter_count: int = 0
    has_errors: bool = False
    conditional: bool = False


@dataclass(frozen=True)
class ExtractedImport:
    source_local_id: int | None
    kind: ImportKind
    target: str
    imported_name: str | None
    alias: str | None
    relative_level: int | None
    line: int


@dataclass(frozen=True)
class ExtractedRelation:
    source_local_id: int
    relation_type: RelationType
    target_name: str
    target_local_id: int | None
    resolution: Resolution
    line: int


@dataclass(frozen=True)
class FileExtraction:
    language: Language
    parse_status: ParseStatus
    error_kind: ErrorKind | None = None
    error_count: int = 0
    first_error_line: int | None = None
    first_error_column: int | None = None
    module_name: str | None = None
    symbols_truncated: bool = False
    symbols: list[ExtractedSymbol] = field(default_factory=list)
    imports: list[ExtractedImport] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)
