"""Regresiones del almacén de claves y del adaptador de herramientas GLM."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from fastapi import HTTPException
from google.genai import types

from agent_core import provider_keys as keys
from agent_core.glm_provider import convert_messages, generate_glm


class ProviderKeysTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "keys.json"
        self.path.write_text("[]", encoding="utf-8")
        self.store_patch = patch.object(keys, "PATH", self.path)
        self.catalog_patch = patch.object(keys, "apply_catalog")
        self.store_patch.start()
        self.catalog_patch.start()

    def tearDown(self):
        self.catalog_patch.stop()
        self.store_patch.stop()
        self.directory.cleanup()

    def add(self, secret="test-key-one"):
        return keys.add_key(keys.KeyInput(provider="glm", label="Prueba", secret=secret))["id"]

    def test_glm_balance_error_is_explained_and_thinking_is_not_forced(self):
        key_id = self.add()
        response = requests.Response()
        response.status_code = 429
        response._content = b'{"error":{"code":"1113","message":"Insufficient balance"}}'
        with patch.object(keys, "discover", return_value=["glm-5.3-flash"]), patch.object(keys.requests, "post", return_value=response) as post:
            result = keys.test_key(key_id)
        self.assertEqual(result["status"], "limited")
        self.assertIn("saldo", result["message"])
        self.assertNotIn("thinking", post.call_args.kwargs["json"])

    def test_glm_discovery_and_runtime_exclude_paid_models(self):
        self.assertEqual(keys.discover("glm", "test"), ["glm-4.7-flash", "glm-4.5-flash"])
        self.assertFalse(keys.text_model("glm/glm-5.3-flash"))
        self.assertTrue(keys.text_model("glm/glm-4.7-flash"))
        with patch("agent_core.glm_provider.requests.post") as post:
            with self.assertRaises(ValueError):
                generate_glm("glm/glm-5.3-flash", [], types.GenerateContentConfig())
            post.assert_not_called()

    def test_encrypted_crud_and_hidden_listing(self):
        key_id = self.add()
        self.assertNotIn("test-key-one", self.path.read_text())
        self.assertNotIn("secret", keys.list_keys()[0])
        self.assertEqual(keys.reveal_key(key_id)["secret"], "test-key-one")
        keys.edit_key(key_id, keys.KeyInput(provider="glm", label="Renombrada"))
        self.assertEqual(keys.reveal_key(key_id)["secret"], "test-key-one")
        keys.delete_key(key_id)
        self.assertEqual(json.loads(self.path.read_text()), [])

    def test_edit_rejects_duplicate_secret(self):
        self.add()
        second = self.add("test-key-two")
        with self.assertRaises(HTTPException) as result:
            keys.edit_key(second, keys.KeyInput(provider="glm", label="Duplicada", secret="test-key-one"))
        self.assertEqual(result.exception.status_code, 409)

    def test_quota_error_does_not_mark_key_invalid(self):
        key_id = self.add()
        response = requests.Response()
        response.status_code = 429
        with patch.object(keys, "discover", return_value=["glm-4.7-flash"]), patch.object(keys.requests, "post", return_value=response):
            result = keys.test_key(key_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "limited")
        self.assertEqual(keys.list_keys()[0]["status"], "limited")

    def test_stale_check_cannot_overwrite_changed_key(self):
        key_id = self.add()
        def discover(provider, secret):
            keys.edit_key(key_id, keys.KeyInput(provider="glm", label="Nueva", secret="test-key-updated"))
            return ["glm-4.7-flash"]
        response = requests.Response()
        response.status_code = 401
        with patch.object(keys, "discover", side_effect=discover), patch.object(keys.requests, "post", return_value=response):
            with self.assertRaises(HTTPException) as result:
                keys.test_key(key_id)
        self.assertEqual(result.exception.status_code, 409)
        self.assertEqual(keys.list_keys()[0]["status"], "untested")


class GlmAdapterTests(unittest.TestCase):
    def test_parallel_calls_keep_their_ids_when_results_return(self):
        calls = [types.Part(function_call=types.FunctionCall(id="first", name="sample", args={"x": 1})), types.Part(function_call=types.FunctionCall(id="second", name="sample", args={"x": 2}))]
        results = [types.Part.from_function_response(name="sample", response={"value": 1}), types.Part.from_function_response(name="sample", response={"value": 2})]
        messages, _ = convert_messages([types.Content(role="model", parts=calls), types.Content(role="user", parts=results)], "Asistente")
        self.assertEqual([message["tool_call_id"] for message in messages if message["role"] == "tool"], ["first", "second"])

    def test_glm_overload_is_transient_not_quota(self):
        response = requests.Response()
        response.status_code = 429
        response._content = b'{"error":{"code":"1305"}}'
        with patch("agent_core.glm_provider.credentials", return_value=[("id", "test-key", [])]), patch("agent_core.glm_provider.requests.post", return_value=response):
            with self.assertRaisesRegex(ValueError, "503"):
                generate_glm("glm/glm-4.7-flash", [types.Content(role="user", parts=[types.Part.from_text(text="Prueba")])], types.GenerateContentConfig())

    def test_generation_preserves_tool_call_and_token_counts(self):
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"choices": [{"message": {"tool_calls": [{"id": "call-one", "function": {"name": "sample", "arguments": "{}"}}]}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}).encode()
        with patch("agent_core.glm_provider.credentials", return_value=[("id", "test-key", [])]), patch("agent_core.glm_provider.requests.post", return_value=response):
            result = generate_glm("glm/glm-4.7-flash", [types.Content(role="user", parts=[types.Part.from_text(text="Prueba")])], types.GenerateContentConfig())
        self.assertEqual(result.function_calls[0].id, "call-one")
        self.assertEqual(result.usage_metadata.total_token_count, 7)


if __name__ == "__main__":
    unittest.main()
