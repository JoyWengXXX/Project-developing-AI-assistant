"""檢查 embedding 向量的維度、數值與正規化。"""

import math
from typing import Any

from repopilot.embedding.local_runtime import EmbeddingRuntimeError


def validate_vector(vector: Any) -> list[float]:
    if not isinstance(vector, list) or len(vector) != 768:
        raise EmbeddingRuntimeError("The isolated embedding runtime returned an invalid vector.")
    if any(isinstance(value, bool) or not isinstance(value, int | float) for value in vector):
        raise EmbeddingRuntimeError("The isolated embedding runtime returned an invalid vector.")
    try:
        converted = [float(value) for value in vector]
    except OverflowError:
        raise EmbeddingRuntimeError(
            "The isolated embedding runtime returned an invalid vector."
        ) from None
    norm = math.sqrt(sum(value * value for value in converted))
    if (
        not all(math.isfinite(value) for value in converted)
        or not math.isfinite(norm)
        or abs(norm - 1.0) > 0.0001
    ):
        raise EmbeddingRuntimeError("The isolated embedding runtime returned an invalid vector.")
    return converted
