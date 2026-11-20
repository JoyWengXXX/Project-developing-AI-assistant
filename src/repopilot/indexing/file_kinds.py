"""依路徑把檔案粗分為原始碼、測試、文件、資料與建置產物。

這是啟發式規則（只看路徑），用在兩處：檢索搜尋的篩選，以及決定哪些檔案不進檢索索引。
Python 與 SQL 兩種判斷都由同一份 `_RULES` 產生，避免兩邊不一致。
"""

from typing import Literal

from sqlalchemy import ColumnElement, case, func, or_
from sqlalchemy.orm import InstrumentedAttribute

FileKind = Literal["source", "test", "docs", "data", "build"]
FILE_KINDS: tuple[FileKind, ...] = ("source", "test", "docs", "data", "build")

# 不進檢索索引的種類：建置產物（壓縮的 bundle 等）與資料檔幾乎只增加雜訊與 embedding 時間。
RETRIEVAL_EXCLUDED_KINDS: tuple[FileKind, ...] = ("data", "build")

# 規則作用在 "/" + 小寫路徑：("contains", x) 為子字串，("endswith", x) 為結尾。
# 以 "/" 包住的片段代表完整的目錄名稱，所以 "/build/" 不會比對到 "rebuild.js"。
# 排序即優先順序：先比對到的種類勝出；都沒有則為 source。
_RULES: tuple[tuple[FileKind, tuple[tuple[str, str], ...]], ...] = (
    (
        "build",
        (
            ("contains", "/wwwroot/"),
            ("contains", "/dist/"),
            ("contains", "/build/"),
            ("contains", "/obj/"),
            ("contains", "/target/"),
            ("contains", "/node_modules/"),
            ("contains", "/vendor/"),
            ("contains", "/.next/"),
            ("contains", "/.nuxt/"),
            ("contains", "/coverage/"),
            ("contains", "/__pycache__/"),
            ("endswith", ".min.js"),
            ("endswith", ".min.css"),
            ("endswith", ".bundle.js"),
            ("endswith", ".map"),
        ),
    ),
    (
        "data",
        (
            ("endswith", ".csv"),
            ("endswith", ".tsv"),
            ("endswith", ".jsonl"),
            ("endswith", ".ndjson"),
            ("endswith", ".parquet"),
            ("endswith", ".svg"),
            ("endswith", ".lock"),
            ("endswith", "/package-lock.json"),
            ("endswith", "/pnpm-lock.yaml"),
        ),
    ),
    (
        "test",
        (
            ("contains", "/test/"),
            ("contains", "/tests/"),
            ("contains", "/__tests__/"),
            ("contains", "/spec/"),
            ("contains", "/specs/"),
            ("contains", "/e2e/"),
            ("contains", ".tests/"),
            ("contains", ".test/"),
            ("contains", "/test_"),
            ("contains", ".test."),
            ("contains", ".spec."),
            ("endswith", "_test.py"),
            ("endswith", "_test.go"),
            ("endswith", "tests.cs"),
            ("endswith", "tests.java"),
            ("endswith", "_spec.rb"),
        ),
    ),
    (
        "docs",
        (
            ("contains", "/docs/"),
            ("contains", "/doc/"),
            ("contains", "/documentation/"),
            ("endswith", ".md"),
            ("endswith", ".mdx"),
            ("endswith", ".rst"),
            ("endswith", ".adoc"),
        ),
    ),
)


def _matches(text: str, rules: tuple[tuple[str, str], ...]) -> bool:
    return any(
        (needle in text) if op == "contains" else text.endswith(needle) for op, needle in rules
    )


def classify_path(path: str) -> FileKind:
    text = "/" + path.lower()
    for kind, rules in _RULES:
        if _matches(text, rules):
            return kind
    return "source"


def kind_expression(path_column: InstrumentedAttribute[str]) -> ColumnElement[str]:
    """`classify_path` 的 SQL 版本（PostgreSQL）；LIKE 以 autoescape 處理 `_` 與 `%`。"""
    text = func.concat("/", func.lower(path_column))
    whens = []
    for kind, rules in _RULES:
        conditions = [
            text.contains(needle, autoescape=True)
            if op == "contains"
            else text.endswith(needle, autoescape=True)
            for op, needle in rules
        ]
        whens.append((or_(*conditions), kind))
    return case(*whens, else_="source")
