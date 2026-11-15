"""不同模型與執行環境不得共用不相容的向量。"""

import pytest

from repopilot.embedding.profile import EmbeddingProfile


def test_model_and_runtime_identity_each_change_the_profile_hash() -> None:
    first = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    same = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    different_model = EmbeddingProfile(model_sha256="c" * 64, runtime_bundle_sha256="b" * 64)
    different_runtime = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="d" * 64)
    assert first.hash() == same.hash()
    assert len({first.hash(), different_model.hash(), different_runtime.hash()}) == 3


@pytest.mark.parametrize("digest", ["", "A" * 64, "z" * 64, "0" * 63])
def test_invalid_artifact_digests_are_rejected(digest: str) -> None:
    with pytest.raises(ValueError):
        EmbeddingProfile(model_sha256=digest, runtime_bundle_sha256="b" * 64)
