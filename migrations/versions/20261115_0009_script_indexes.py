"""擴充結構索引以支援 TypeScript／JavaScript。

降版不刪除資料；若仍有新語言或新種類的索引資料，舊約束會拒絕降版。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_LANGUAGES = "language IN ('python', 'csharp')"
_NEW_LANGUAGES = "language IN ('python', 'csharp', 'typescript', 'javascript')"
_OLD_KINDS = (
    "kind IN ('module', 'namespace', 'class', 'struct', 'record', 'interface', 'enum', "
    "'enum_member', 'delegate', 'function', 'method', 'constructor', 'property', 'field')"
)
_NEW_KINDS = _OLD_KINDS.replace("'interface',", "'interface', 'type_alias',")
_OLD_IMPORTS = (
    "kind IN ('import', 'from_import', 'using', 'using_static', 'using_alias', 'global_using')"
)
_NEW_IMPORTS = _OLD_IMPORTS.replace("'global_using')", "'global_using', 'script_import')")


def _replace(table: str, name: str, condition: str) -> None:
    constraint = op.f(f"ck_{table}_{name}")
    op.drop_constraint(constraint, table, type_="check")
    op.create_check_constraint(constraint, table, condition)


def upgrade() -> None:
    for table in ("indexed_files", "symbols", "import_references"):
        _replace(table, "language_valid", _NEW_LANGUAGES)
    _replace("symbols", "kind_valid", _NEW_KINDS)
    _replace("import_references", "kind_valid", _NEW_IMPORTS)


def downgrade() -> None:
    _replace("import_references", "kind_valid", _OLD_IMPORTS)
    _replace("symbols", "kind_valid", _OLD_KINDS)
    for table in ("import_references", "symbols", "indexed_files"):
        _replace(table, "language_valid", _OLD_LANGUAGES)
