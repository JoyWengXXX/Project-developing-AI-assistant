"""結構索引的 ORM → 回應模型轉換（路由只轉換，不含業務規則）。"""

from repopilot.api.schemas import (
    FirstErrorResponse,
    ImportReferenceResponse,
    IndexedFileResponse,
    SymbolDetailResponse,
    SymbolRelationResponse,
    SymbolSummaryResponse,
)
from repopilot.db.models import ImportReference, IndexedFile, SymbolRelation
from repopilot.services.index_queries import SymbolRow


def to_indexed_file_item(
    file: IndexedFile, path: str, snapshot_file_id: int
) -> IndexedFileResponse:
    first_error = (
        FirstErrorResponse(line=file.first_error_line, column=file.first_error_column or 0)
        if file.first_error_line is not None
        else None
    )
    return IndexedFileResponse.model_validate(
        {
            "indexed_file_id": file.id,
            "file_id": snapshot_file_id,
            "path": path,
            "language": file.language,
            "parse_status": file.parse_status,
            "error_count": file.error_count,
            "first_error": first_error,
            "symbol_count": file.symbol_count,
            "symbols_truncated": file.symbols_truncated,
        }
    )


def _summary_fields(row: SymbolRow) -> dict[str, object]:
    symbol = row.symbol
    return {
        "symbol_id": symbol.id,
        "kind": symbol.kind,
        "language": symbol.language,
        "name": symbol.name,
        "qualified_name": symbol.qualified_name,
        "file_id": row.file_id,
        "path": row.path,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
        "parent_id": symbol.parent_symbol_id,
        "modifiers": symbol.modifiers,
        "is_async": symbol.is_async,
        "has_errors": symbol.has_errors,
        "conditional": symbol.conditional,
        "has_children": row.has_children,
    }


def to_symbol_summary(row: SymbolRow) -> SymbolSummaryResponse:
    return SymbolSummaryResponse.model_validate(_summary_fields(row))


def to_symbol_detail(
    row: SymbolRow, imports: list[ImportReference], relations: list[SymbolRelation]
) -> SymbolDetailResponse:
    symbol = row.symbol
    return SymbolDetailResponse.model_validate(
        {
            **_summary_fields(row),
            "start_column": symbol.start_column,
            "end_column": symbol.end_column,
            "leading_start_line": symbol.leading_start_line,
            "type_parameter_count": symbol.type_parameter_count,
            "imports": [
                ImportReferenceResponse(
                    kind=ref.kind,
                    target=ref.target,
                    imported_name=ref.imported_name,
                    alias=ref.alias,
                    relative_level=ref.relative_level,
                    line=ref.line,
                )
                for ref in imports
            ],
            "relations": [
                SymbolRelationResponse(
                    relation_type=relation.relation_type,
                    target_name=relation.target_name,
                    target_symbol_id=relation.target_symbol_id,
                    resolution=relation.resolution,
                    line=relation.line,
                )
                for relation in relations
            ],
        }
    )
