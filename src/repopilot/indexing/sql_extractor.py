"""SQL 宣告的語法層級抽取；不執行模板或 SQL。"""

from bisect import bisect_right
from functools import lru_cache

from sqlfluff.core import FluffConfig
from sqlfluff.core.errors import SQLParseError
from sqlfluff.core.parser import Lexer, Parser
from sqlfluff.core.parser.segments import BaseSegment

from repopilot.indexing.types import ExtractedSymbol, FileExtraction, SymbolKind

_DIALECTS = frozenset({"postgres", "tsql", "mysql", "sqlite"})
_MAX_QUALIFIED_NAME_BYTES = 2_000
_KINDS: dict[str, SymbolKind] = {
    "create_table_statement": "table",
    "create_view_statement": "view",
    "create_function_statement": "function",
    "create_procedure_statement": "procedure",
}


@lru_cache(maxsize=4)
def _config(dialect: str) -> FluffConfig:
    return FluffConfig(ignore_local_config=True, overrides={"dialect": dialect, "templater": "raw"})


def _identifiers(segment: BaseSegment) -> list[str]:
    result: list[str] = []
    for token in segment.raw_segments:
        if not token.is_type("identifier", "function_name_identifier"):
            continue
        value = token.raw
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "`"}:
            value = value[1:-1].replace(value[0] * 2, value[0])
        elif value.startswith("[") and value.endswith("]"):
            value = value[1:-1].replace("]]", "]")
        result.append(value)
    return result


def _position(source: str, line_starts: list[int], offset: int) -> tuple[int, int]:
    line_index = bisect_right(line_starts, offset) - 1
    return line_index + 1, len(source[line_starts[line_index] : offset].encode("utf-8"))


def extract_sql(
    path: str, content: str, *, dialect: str | None, max_symbols: int
) -> FileExtraction:
    if not path.lower().endswith(".sql"):
        raise ValueError("Unsupported SQL extension")
    if dialect is None:
        return FileExtraction("sql", "failed", "dialect_unspecified", 1)
    if dialect not in _DIALECTS:
        raise ValueError("Unsupported SQL dialect")
    config = _config(dialect)
    try:
        tokens, lex_errors = Lexer(config=config).lex(content)
        tree = Parser(config=config).parse(tokens)
    except SQLParseError:
        return FileExtraction("sql", "failed", "tree_error", 1)
    except RecursionError:
        return FileExtraction("sql", "failed", "too_deep", 1)
    if tree is None:
        return FileExtraction("sql", "failed", "tree_error", 1)
    line_starts = [0] + [index + 1 for index, char in enumerate(content) if char == "\n"]
    errors = list(tree.recursive_crawl("unparsable"))
    positions = [
        _position(content, line_starts, error.pos_marker.source_slice.start)
        for error in errors
        if error.pos_marker is not None
    ]
    for error in lex_errors:
        if error.line_no and error.line_pos:
            line_index = error.line_no - 1
            start = line_starts[line_index]
            positions.append(_position(content, line_starts, start + error.line_pos - 1))
    symbols: list[ExtractedSymbol] = []
    truncated = False
    for statement in tree.recursive_crawl(*_KINDS):
        names = [
            child
            for child in statement.segments
            if child.is_type("table_reference", "object_reference", "function_name")
        ]
        if not names or statement.pos_marker is None:
            continue
        name_parts = _identifiers(names[0])
        if not name_parts:
            continue
        qualified_name = ".".join(name_parts)
        if len(qualified_name.encode("utf-8")) > _MAX_QUALIFIED_NAME_BYTES:
            return FileExtraction("sql", "failed", "sql_name_too_long", 1)
        if len(symbols) >= max_symbols:
            truncated = True
            break
        start = statement.pos_marker.source_slice.start
        end = statement.pos_marker.source_slice.stop
        start_line, start_column = _position(content, line_starts, start)
        end_line, end_column = _position(content, line_starts, end)
        if end_column == 0 and end_line > start_line:
            end_line -= 1
        symbols.append(
            ExtractedSymbol(
                local_id=len(symbols),
                parent_local_id=None,
                kind=_KINDS[statement.type],
                name=name_parts[-1],
                qualified_name=qualified_name,
                start_line=start_line,
                end_line=end_line,
                start_column=start_column,
                end_column=end_column,
                has_errors=any(statement.recursive_crawl("unparsable")),
            )
        )
    count = len(errors) + len(lex_errors)
    first = min(positions) if positions else None
    return FileExtraction(
        "sql",
        "parsed_with_errors" if count else "parsed",
        "tree_error" if count else None,
        count,
        first[0] if first else None,
        first[1] if first else None,
        symbols_truncated=truncated,
        symbols=symbols,
    )
