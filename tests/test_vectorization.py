"""Unit tests for automatic embedding-profile vectorization configuration."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import llm_gateway
import vectorization


def embedding_config(config_id="embedding-one", model="embed-model"):
    """Return one usable registry configuration for test selection."""
    return {
        "config_id": config_id,
        "name": config_id,
        "provider": "ollama",
        "api_url": "http://ollama:11434/v1/chat/completions",
        "api_key": "",
        "model": model,
        "timeout_seconds": 90,
        "enabled": True,
        "configured": True,
        "profile_type": "embedding",
    }


class VectorizationProfileTest(unittest.TestCase):
    """Validate profile selection without launching a vectorization batch."""

    def test_embedding_dimensions_support_auto_detection(self):
        self.assertEqual(vectorization.normalize_embedding_dimensions(None), 0)
        self.assertEqual(vectorization.normalize_embedding_dimensions("auto"), 0)
        self.assertEqual(vectorization.normalize_embedding_dimensions("0"), 0)
        self.assertEqual(vectorization.normalize_embedding_dimensions("1024"), 1024)

    @patch.dict("os.environ", {}, clear=True)
    @patch("vectorization.list_llm_configs")
    def test_unique_embedding_profile_is_selected_automatically(self, list_configs):
        list_configs.return_value = [
            embedding_config(),
            {**embedding_config("chat-one"), "profile_type": "chat"},
        ]

        config = vectorization.fallback_vectorization_config()

        self.assertTrue(config["enabled"])
        self.assertEqual(config["llm_config_id"], "embedding-one")
        self.assertEqual(config["embedding_dimensions"], 0)
        self.assertEqual(config["source"], "llm_profile")

    @patch.dict("os.environ", {}, clear=True)
    @patch("vectorization.list_llm_configs")
    def test_multiple_embedding_profiles_require_explicit_selection(self, list_configs):
        list_configs.return_value = [
            embedding_config("embedding-one"),
            embedding_config("embedding-two"),
        ]

        config = vectorization.fallback_vectorization_config()

        self.assertFalse(config["enabled"])
        self.assertEqual(config["llm_config_id"], "")
        self.assertIn("Plusieurs configurations", config["auto_selection_error"])

    @patch("vectorization.effective_llm_config")
    @patch("vectorization.get_vectorization_config")
    def test_runtime_derives_embedding_endpoint_and_model(self, get_config, effective):
        get_config.return_value = {
            "enabled": True,
            "llm_config_id": "embedding-one",
            "embedding_api_url": "",
            "embedding_model": "",
            "embedding_dimensions": 0,
            "batch_size": 25,
            "source": "llm_profile",
        }
        effective.return_value = embedding_config()

        runtime = vectorization.resolve_vectorization_runtime_config()

        self.assertTrue(runtime["configured"])
        self.assertEqual(runtime["api_url"], "http://ollama:11434/v1/embeddings")
        self.assertEqual(runtime["model"], "embed-model")

    @patch("vectorization.execute_embedding_request", return_value=[0.1, 0.2, 0.3])
    @patch("vectorization.resolve_vectorization_runtime_config")
    def test_vectorization_test_detects_dimensions(self, resolve_runtime, _execute):
        resolve_runtime.return_value = {
            **embedding_config(),
            "configured": True,
            "vector_config": {"embedding_dimensions": 0},
        }

        result = vectorization.test_vectorization_config("test")

        self.assertEqual(result["embedding_dimensions"], 3)

    @patch("llm_gateway.finish_llm_audit_session")
    @patch("llm_gateway.record_llm_exchange")
    @patch("llm_gateway.start_llm_audit_session", return_value="audit-embedding")
    @patch("vectorization.test_embedding_llm_config")
    @patch("llm_gateway.get_llm_config_by_id")
    def test_generic_llm_test_routes_embedding_profile_to_embedding_call(
        self,
        get_config,
        test_embedding,
        _start_audit,
        _record_exchange,
        finish_audit,
    ):
        get_config.return_value = embedding_config()
        test_embedding.return_value = {
            "embedding_dimensions": 1024,
            "embedding_api_url": "http://ollama:11434/v1/embeddings",
            "preview": [0.1, 0.2],
        }

        result = llm_gateway.test_llm_config("embedding-one")

        test_embedding.assert_called_once()
        self.assertEqual(result["embedding_dimensions"], 1024)
        self.assertEqual(result["content"], "Embedding valide: 1024 dimensions.")
        self.assertEqual(result["audit_session_id"], "audit-embedding")
        finish_audit.assert_called_once_with("audit-embedding", "success")


if __name__ == "__main__":
    unittest.main()
