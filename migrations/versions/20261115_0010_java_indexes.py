"""擴充結構索引以支援 Java；降版不刪除新索引資料。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_LANGUAGES = "language IN ('python', 'csharp', 'typescript', 'javascript')"
_NEW_LANGUAGES = "language IN ('python', 'csharp', 'typescript', 'javascript', 'java')"
_OLD_IMPORTS = (
    "kind IN ('import', 'from_import', 'using', 'using_static', 'using_alias', "
    "'global_using', 'script_import')"
)
_NEW_IMPORTS = _OLD_IMPORTS.replace(
    "'script_import')", "'script_import', 'java_import', 'java_static_import')"
)


def _replace(table: str, name: str, condition: str) -> None:
    constraint = op.f(f"ck_{table}_{name}")
    op.drop_constraint(constraint, table, type_="check")
    op.create_check_constraint(constraint, table, condition)


def upgrade() -> None:
    for table in ("indexed_files", "symbols", "import_references"):
        _replace(table, "language_valid", _NEW_LANGUAGES)
    _replace("import_references", "kind_valid", _NEW_IMPORTS)


def downgrade() -> None:
    _replace("import_references", "kind_valid", _OLD_IMPORTS)
    for table in ("import_references", "symbols", "indexed_files"):
        _replace(table, "language_valid", _OLD_LANGUAGES)
