import hashlib
import json
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from repopilot.config import Settings

# 匯入行為（解碼規則、分類方式）改變時遞增，使舊快照不被誤認為相同設定
PROFILE_VERSION = 1


@dataclass(frozen=True)
class ImportProfile:
    """影響匯入結果的設定；其雜湊是快照唯一性的一部分。"""

    excluded_patterns: tuple[str, ...]
    excluded_exceptions: tuple[str, ...]
    max_file_bytes: int
    max_files: int
    max_snapshot_bytes: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "ImportProfile":
        return cls(
            excluded_patterns=tuple(settings.import_excluded_patterns),
            excluded_exceptions=tuple(settings.import_excluded_exceptions),
            max_file_bytes=settings.import_max_file_bytes,
            max_files=settings.import_max_files,
            max_snapshot_bytes=settings.import_max_snapshot_bytes,
        )

    def _canonical(self) -> dict[str, Any]:
        return {
            "version": PROFILE_VERSION,
            "excluded_patterns": sorted({p.lower() for p in self.excluded_patterns}),
            "excluded_exceptions": sorted({p.lower() for p in self.excluded_exceptions}),
            "max_file_bytes": self.max_file_bytes,
            "max_files": self.max_files,
            "max_snapshot_bytes": self.max_snapshot_bytes,
        }

    def hash(self) -> str:
        payload = json.dumps(self._canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def is_excluded(self, path: str) -> bool:
        """以檔名（不分大小寫）比對排除規則；exceptions 優先於 patterns。"""
        name = path.rsplit("/", 1)[-1].lower()
        if any(fnmatchcase(name, pattern.lower()) for pattern in self.excluded_exceptions):
            return False
        return any(fnmatchcase(name, pattern.lower()) for pattern in self.excluded_patterns)
