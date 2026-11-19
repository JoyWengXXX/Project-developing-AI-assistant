import logging
import os

import pytest
from sqlfluff.core.errors import SQLParseError
from sqlfluff.core.parser import Parser

from repopilot.config import Settings
from repopilot.indexing.profile import IndexProfile
from repopilot.indexing.sql_extractor import extract_sql
from repopilot.logging_config import configure_logging
from repopilot.services.snapshot_indexing import _extract


@pytest.mark.parametrize(
    ("dialect", "source", "expected"),
    [
        (
            "postgres",
            "CREATE TABLE public.items (id INTEGER PRIMARY KEY);\n"
            "CREATE FUNCTION public.f(i integer) RETURNS integer LANGUAGE sql "
            "AS $$ SELECT i + 1 $$;",
            [("items", "public.items", "table", 1), ("f", "public.f", "function", 2)],
        ),
        (
            "tsql",
            "CREATE TABLE [dbo].[items] ([id] int PRIMARY KEY);\nGO\n"
            "CREATE PROCEDURE [dbo].[get_items] AS SELECT TOP (1) [id] FROM [dbo].[items];\nGO",
            [("items", "dbo.items", "table", 1), ("get_items", "dbo.get_items", "procedure", 3)],
        ),
        (
            "mysql",
            "CREATE TABLE `items` (`id` INT PRIMARY KEY AUTO_INCREMENT);\n"
            "CREATE VIEW `v_items` AS SELECT `id` FROM `items`;",
            [("items", "items", "table", 1), ("v_items", "v_items", "view", 2)],
        ),
        (
            "sqlite",
            'CREATE TABLE "items" (id INTEGER PRIMARY KEY AUTOINCREMENT);\n'
            "CREATE VIEW v_items AS SELECT id FROM items;",
            [("items", "items", "table", 1), ("v_items", "v_items", "view", 2)],
        ),
    ],
)
def test_sql_dialects_extract_declared_objects(
    dialect: str, source: str, expected: list[tuple[str, str, str, int]]
) -> None:
    result = extract_sql("schema.sql", source, dialect=dialect, max_symbols=100)
    assert result.parse_status == "parsed"
    assert [(s.name, s.qualified_name, s.kind, s.start_line) for s in result.symbols] == expected


def test_sql_unknown_dialect_is_not_guessed() -> None:
    result = extract_sql(
        "schema.sql", "CREATE TABLE users (id INTEGER);", dialect=None, max_symbols=10
    )
    assert result.parse_status == "failed"
    assert result.error_kind == "dialect_unspecified"
    assert result.symbols == []


def test_sql_bad_syntax_and_limit() -> None:
    result = extract_sql(
        "schema.sql",
        "CREATE TABLE first (id INTEGER);\nCREATE TABLE broken (id INTEGER;",
        dialect="postgres",
        max_symbols=10,
    )
    assert result.parse_status == "parsed_with_errors"
    assert result.error_count > 0
    assert [symbol.name for symbol in result.symbols] == ["first"]
    limited = extract_sql(
        "schema.sql",
        "CREATE TABLE first (id INTEGER);\nCREATE TABLE second (id INTEGER);",
        dialect="postgres",
        max_symbols=1,
    )
    assert [symbol.name for symbol in limited.symbols] == ["first"]
    assert limited.symbols_truncated


def test_sql_does_not_evaluate_template_or_inline_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_execution(_command: str) -> int:
        raise AssertionError("template executed")

    monkeypatch.setattr(os, "system", reject_execution)
    source = (
        "-- sqlfluff:templater:jinja\n"
        "{{ cycler.__init__.__globals__.os.system('echo BAD') }}\n"
        "CREATE TABLE safe (id INTEGER);"
    )
    result = extract_sql("schema.sql", source, dialect="postgres", max_symbols=10)
    assert result.parse_status == "parsed_with_errors"


def test_sql_dialect_selection_limit_and_profile() -> None:
    settings = Settings(index_sql_dialect="tsql", index_sql_max_file_bytes=100)
    tsql = "CREATE TABLE [dbo].[items] ([id] int);"
    assert _extract(1, "schema.sql", tsql, settings).parse_status == "parsed"
    assert (
        _extract(2, "schema.sqlite.sql", "CREATE VIEW v AS SELECT 1;", settings).parse_status
        == "parsed"
    )
    too_large = _extract(3, "schema.sql", tsql * 3, settings)
    assert too_large.error_kind == "sql_too_large"
    assert (
        IndexProfile.from_settings(settings).hash()
        != IndexProfile.from_settings(
            Settings(index_sql_dialect="postgres", index_sql_max_file_bytes=100)
        ).hash()
    )


def test_sql_utf8_columns_and_quoted_identifiers() -> None:
    result = extract_sql(
        "schema.sql",
        '/* 中文 */ CREATE TABLE "weird.name" (id INTEGER);\n',
        dialect="postgres",
        max_symbols=10,
    )
    assert result.parse_status == "parsed"
    symbol = result.symbols[0]
    assert symbol.name == "weird.name"
    assert symbol.start_column == len("/* 中文 */ ".encode())


def test_sql_source_is_not_logged_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    configure_logging("DEBUG")
    extract_sql(
        "schema.sql",
        "CREATE TABLE private_source_marker (id INTEGER);",
        dialect="postgres",
        max_symbols=10,
    )
    assert "private_source_marker" not in caplog.text


def test_sql_parser_limit_is_not_reported_as_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_parse(*_args: object, **_kwargs: object) -> None:
        raise SQLParseError("parse limit")

    monkeypatch.setattr(Parser, "parse", reject_parse)
    result = extract_sql("schema.sql", "SELECT 1;", dialect="postgres", max_symbols=10)
    assert (result.parse_status, result.error_kind) == ("failed", "tree_error")


def test_sql_oversized_identifier_is_explicitly_rejected() -> None:
    source = f'CREATE TABLE "{"x" * 2001}" (id INTEGER);'
    result = extract_sql("schema.sql", source, dialect="postgres", max_symbols=10)
    assert (result.parse_status, result.error_kind) == ("failed", "sql_name_too_long")
    assert result.symbols == []
