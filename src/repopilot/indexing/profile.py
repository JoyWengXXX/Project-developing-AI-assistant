"""索引設定雜湊（規格 6.4）：影響索引結果的設定與解析器版本。

作法同 `import_profile.py`（D26）：標準 JSON（鍵排序、緊湊）的 SHA-256。任何一項改變就得到不同的
索引，而不是誤用舊結果；API 與 worker 各自計算，不一致（例如 API 在主機的 Python 次版本或
tree-sitter 版本與容器不同）即 `profile_mismatch`，大聲失敗而非默默產生不同結果。

C# 抽取器（切片 7）啟用後，`csharp` 區塊含 tree-sitter 與文法套件的版本（執行期以
`importlib.metadata.version()` 取得，不寫死）；切片 6 建立的索引（`csharp.enabled=false`）因此會被
視為「目前設定下尚未建立」，由 `index-jobs` 重建，而不是被當成已完成。
"""

import hashlib
import json
import sys
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any

from repopilot.config import Settings

# 整體 JSON 結構改變時遞增
PROFILE_SCHEMA = 1
# Python 抽取規則改變（輸出的符號、關係、位置語意）時遞增
PYTHON_ALGORITHM = 1
# C# 抽取規則改變時遞增
CSHARP_ALGORITHM = 1
SCRIPT_ALGORITHM = 1
JAVA_ALGORITHM = 1


@dataclass(frozen=True)
class IndexProfile:
    python_major_minor: str
    max_symbols_per_file: int
    tree_sitter: str
    csharp_grammar: str
    csharp_include_fields: bool
    typescript_grammar: str
    javascript_grammar: str
    java_grammar: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "IndexProfile":
        # 執行期取得（不寫死）：worker 容器的直譯器與解析器版本決定能解析什麼（規格 7.2、7.3）
        return cls(
            python_major_minor=f"{sys.version_info.major}.{sys.version_info.minor}",
            max_symbols_per_file=settings.index_max_symbols_per_file,
            tree_sitter=version("tree-sitter"),
            csharp_grammar=version("tree-sitter-c-sharp"),
            csharp_include_fields=settings.index_csharp_include_fields,
            typescript_grammar=version("tree-sitter-typescript"),
            javascript_grammar=version("tree-sitter-javascript"),
            java_grammar=version("tree-sitter-java"),
        )

    def analyzers(self) -> dict[str, Any]:
        """各語言抽取器的版本資訊（納入雜湊）。"""
        return {
            "python": {
                "enabled": True,
                "python_major_minor": self.python_major_minor,
                "algorithm": PYTHON_ALGORITHM,
            },
            "csharp": {
                "enabled": True,
                "tree_sitter": self.tree_sitter,
                "grammar": self.csharp_grammar,
                "algorithm": CSHARP_ALGORITHM,
                "include_fields": self.csharp_include_fields,
            },
            "typescript": {
                "enabled": True,
                "tree_sitter": self.tree_sitter,
                "grammar": self.typescript_grammar,
                "algorithm": SCRIPT_ALGORITHM,
            },
            "javascript": {
                "enabled": True,
                "tree_sitter": self.tree_sitter,
                "grammar": self.javascript_grammar,
                "algorithm": SCRIPT_ALGORITHM,
            },
            "java": {
                "enabled": True,
                "tree_sitter": self.tree_sitter,
                "grammar": self.java_grammar,
                "algorithm": JAVA_ALGORITHM,
            },
        }

    def describe(self) -> dict[str, Any]:
        """存入 `snapshot_indexes.analyzers` 並由索引狀態端點回傳：`analyzers()` 加上 ABI 版本。

        ABI 版本只供除錯，不納入雜湊（它由兩個套件版本決定）。
        """
        info = self.analyzers()
        info["csharp"] = {**info["csharp"], "abi_version": _csharp_abi_version()}
        return info

    def _canonical(self) -> dict[str, Any]:
        return {
            "schema": PROFILE_SCHEMA,
            **self.analyzers(),
            "limits": {"max_symbols_per_file": self.max_symbols_per_file},
        }

    def hash(self) -> str:
        payload = json.dumps(self._canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _csharp_abi_version() -> int:
    import tree_sitter_c_sharp
    from tree_sitter import Language

    return int(Language(tree_sitter_c_sharp.language()).abi_version)
