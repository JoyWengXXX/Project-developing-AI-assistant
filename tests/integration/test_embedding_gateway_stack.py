"""測試 Windows 主機經私有橋接取得真實模型查詢向量。"""

import math
import os
import unittest

import httpx

from repopilot.config import Settings
from repopilot.embedding.gateway_client import embed_query
from repopilot.embedding.profile import EmbeddingProfile


@unittest.skipUnless(
    os.environ.get("REPOPILOT_RUN_REAL_GATEWAY_TEST") == "1",
    "requires an explicitly configured isolated gateway and model",
)
class TestEmbeddingGatewayStack(unittest.TestCase):
    def test_gateway_authentication_and_real_model(self) -> None:
        settings = Settings(_env_file=None)
        profile = EmbeddingProfile.from_settings(settings)
        url = f"http://127.0.0.1:{settings.embedding_gateway_port}/embed"
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
            response = client.post(
                url, json={"profile_hash": profile.hash(), "query": "fake source query"}
            )
        self.assertEqual(response.status_code, 401)
        vector = embed_query(settings, profile, "fake source function sorts values")
        self.assertEqual(len(vector), 768)
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in vector)), 1.0, places=4)
