import json
import os
import unittest
from unittest.mock import Mock, patch

from zotero_agent.local_llm import LLMConfig, analyze_local, local_text_budget
from zotero_agent.net import AgentError, HTTP, HttpError, Response


def output(**changes):
    value = {"category": "Methods", "tags": ["auditory cortex", "prediction", "learning", "neural networks", "memory"],
             "reason": "Reusable model", "confidence": .8, "pdf_title": "Test title", "pdf_abstract": "Abstract text",
             "matches_doi_metadata": True}
    value.update(changes)
    return value


class LocalModelTests(unittest.TestCase):
    def setUp(self):
        self.config = LLMConfig("ollama", "qwen3:8b", "http://127.0.0.1:11434")
        self.pdf = {"sampled": False, "text": "Test title. Abstract text."}
        self.http = Mock()
        self.http.request.return_value = Response(200, {}, json.dumps({"done": True, "done_reason": "stop",
            "message": {"content": json.dumps(output())}}).encode())

    def test_shared_schema_and_no_api_key(self):
        result = analyze_local(self.http, {"title": "Test title"}, self.pdf, self.config)
        self.assertEqual(len(result["tags"]), 5)
        args, kw = self.http.request.call_args
        self.assertEqual(args[1], "http://127.0.0.1:11434/api/chat")
        self.assertTrue(kw["allow_localhost"])
        self.assertEqual(kw["timeout"], 900)
        self.assertNotIn("headers", kw)
        self.assertFalse(kw["json_data"]["stream"])
        self.assertFalse(kw["json_data"]["think"])
        self.assertEqual(kw["json_data"]["format"]["properties"]["tags"]["minItems"], 5)

    def test_duplicate_tags_are_rejected(self):
        self.http.request.return_value = Response(200, {}, json.dumps({"done": True, "message": {
            "content": json.dumps(output(tags=["duplicate"] * 5))}}).encode())
        with self.assertRaisesRegex(AgentError, "Invalid local model"):
            analyze_local(self.http, {}, self.pdf, self.config)

    def test_truncated_response_is_rejected(self):
        self.http.request.return_value = Response(200, {}, b'{"done":true,"done_reason":"length"}')
        with self.assertRaisesRegex(AgentError, "incomplete"):
            analyze_local(self.http, {}, self.pdf, self.config)

    def test_unavailable_ollama_does_not_call_openai(self):
        self.http.request.side_effect = AgentError("Connection failed")
        with self.assertRaisesRegex(AgentError, "Open the Ollama app"):
            analyze_local(self.http, {}, self.pdf, self.config)
        self.assertEqual(self.http.request.call_count, 1)

    def test_missing_model_explains_download(self):
        self.http.request.side_effect = HttpError(404, "127.0.0.1:11434")
        with self.assertRaisesRegex(AgentError, "ollama pull qwen3:8b"):
            analyze_local(self.http, {}, self.pdf, self.config)

    def test_oversized_input_stops_before_request(self):
        with self.assertRaisesRegex(AgentError, "context budget"):
            analyze_local(self.http, {}, {"sampled": True, "text": "x" * 40000}, self.config)
        self.http.request.assert_not_called()

    def test_budget_rejects_metadata_that_fills_context(self):
        self.assertGreater(local_text_budget({}, 32768), 1000)
        with self.assertRaisesRegex(AgentError, "too little"):
            local_text_budget({"abstract": "x" * 40000}, 32768)

    def test_remote_endpoints_and_cloud_models_are_not_accepted(self):
        for base in ("https://ollama.com", "http://192.168.1.2:11434", "http://127.0.0.1.evil.example:11434",
                     "http://user:secret@127.0.0.1:11434", "http://127.0.0.1:11434/api"):
            with self.subTest(base=base), patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "OLLAMA_BASE_URL": base}):
                with self.assertRaises(AgentError):
                    LLMConfig.from_env()
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "OLLAMA_MODEL": "gemma4:cloud"}):
            with self.assertRaisesRegex(AgentError, "cloud tags"):
                LLMConfig.from_env()

    def test_context_or_model_change_invalidates_local_cache(self):
        plan = {"provider": "ollama", "model": "qwen3:8b", "analysis_config": self.config.cache_settings()}
        self.assertTrue(self.config.matches(plan))
        self.assertFalse(LLMConfig("ollama", "qwen3:14b", self.config.base_url).matches(plan))
        self.assertFalse(LLMConfig("ollama", "qwen3:8b", self.config.base_url, 16384).matches(plan))

    def test_http_permits_loopback_only_when_explicit_and_bypasses_proxy(self):
        client = HTTP()
        client.local_opener = Mock()
        client.opener = Mock()
        result = Mock()
        result.status, result.headers = 200, {}
        result.read.return_value = b'{}'
        manager = client.local_opener.open.return_value
        manager.__enter__ = Mock(return_value=result)
        manager.__exit__ = Mock(return_value=False)
        with self.assertRaisesRegex(AgentError, "HTTPS"):
            client.request("POST", self.config.base_url + "/api/chat")
        self.assertEqual(client.request("POST", self.config.base_url + "/api/chat", allow_localhost=True).status, 200)
        client.opener.open.assert_not_called()
        for url in ("http://example.com/api/chat", "http://127.0.0.1.evil.example/api/chat"):
            with self.assertRaisesRegex(AgentError, "loopback"):
                client.request("POST", url, allow_localhost=True)
        with self.assertRaisesRegex(AgentError, "Credentials"):
            client.request("POST", self.config.base_url, allow_localhost=True, headers={"Authorization": "secret"})


if __name__ == "__main__":
    unittest.main()
