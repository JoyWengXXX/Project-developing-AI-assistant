"""可重現的 embedding 設定識別。"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EmbeddingProfile:
    model_sha256: str
    runtime_bundle_sha256: str

    def __post_init__(self) -> None:
        for value in (self.model_sha256, self.runtime_bundle_sha256):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("Model and runtime bundle digests must be lowercase SHA-256.")

    def describe(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "model_sha256": self.model_sha256,
            "runtime_bundle_sha256": self.runtime_bundle_sha256,
            "dimensions": 768,
            "pooling": "cls",
            "normalization": "l2",
            "add_special_tokens": True,
            "max_tokens": 512,
            "max_input_bytes": 32768,
            "segment_algorithm": "utf8-token-bisect-v1",
        }

    def hash(self) -> str:
        encoded = json.dumps(self.describe(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
