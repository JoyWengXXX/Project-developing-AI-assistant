"""擴充結構索引以支援 SQL；降版不刪除新索引資料。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_LANGUAGES = (
    "language IN ('python', 'csharp', 'typescript', 'javascript', 'java', 'go', 'rust')"
)
_NEW_LANGUAGES = _OLD_LANGUAGES.replace("'rust')", "'rust', 'sql')")
_OLD_KINDS = (
    "kind IN ('module', 'namespace', 'class', 'struct', 'record', 'interface', "
    "'type_alias', 'enum', 'enum_member', 'delegate', 'function', 'method', "
    "'constructor', 'property', 'field')"
)
_NEW_KINDS = _OLD_KINDS.replace("'field')", "'field', 'table', 'view', 'procedure')")
_OLD_ERRORS = (
    "error_kind IS NULL OR error_kind IN ('syntax_error', 'tree_error', 'too_deep', 'parser_crash')"
)
_NEW_ERRORS = _OLD_ERRORS.replace(
    "'parser_crash')",
    "'parser_crash', 'dialect_unspecified', 'sql_too_large', 'sql_name_too_long')",
)


def _replace(table: str, name: str, condition: str) -> None:
    constraint = op.f(f"ck_{table}_{name}")
    op.drop_constraint(constraint, table, type_="check")
    op.create_check_constraint(constraint, table, condition)


def upgrade() -> None:
    for table in ("indexed_files", "symbols", "import_references"):
        _replace(table, "language_valid", _NEW_LANGUAGES)
    _replace("symbols", "kind_valid", _NEW_KINDS)
    _replace("indexed_files", "error_kind_valid", _NEW_ERRORS)


def downgrade() -> None:
    _replace("indexed_files", "error_kind_valid", _OLD_ERRORS)
    _replace("symbols", "kind_valid", _OLD_KINDS)
    for table in ("import_references", "symbols", "indexed_files"):
        _replace(table, "language_valid", _OLD_LANGUAGES)
