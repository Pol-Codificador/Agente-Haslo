import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from google.genai import types
from agent_core import agent, tools

class ToolHandoffTests(unittest.TestCase):
    def test_handoff_keeps_action_results_without_foreign_function_calls(self):
        history = [types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="write_project_file", args={"relative_path":"index.html","content":"<html></html>"}))]), types.Content(role="user", parts=[types.Part.from_function_response(name="write_project_file", response={"saved": True, "path":"index.html"})])]
        portable = agent._portable_tool_history(history)
        self.assertFalse(any(part.function_call or part.function_response for item in portable for part in item.parts))
        text = " ".join(part.text for item in portable for part in item.parts)
        self.assertIn("index.html", text)
        self.assertIn('"saved": true', text)
        self.assertIsNotNone(history[0].parts[0].function_call)

    def test_writer_preserves_literal_code_and_backs_up_previous_file(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tools, "current_project_directory", return_value=Path(folder)):
            original = '<html><script>const x = "a && b"; const y = `${x}`;</script></html>'
            result = json.loads(tools.write_project_file("index.html", original))
            self.assertTrue(result["saved"])
            self.assertEqual(Path(result["path"]).read_text(), original)
            updated = json.loads(tools.write_project_file("index.html", "nuevo"))
            self.assertEqual(Path(updated["backup"]).read_text(), original)
            self.assertIn("error", json.loads(tools.write_project_file("../outside.html", "no")))
