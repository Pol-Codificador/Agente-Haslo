import unittest
from unittest.mock import patch
from agent_core import tools

class ArtifactFormatTests(unittest.TestCase):
    def test_unknown_format_does_not_turn_into_powerpoint(self):
        with self.assertRaises(ValueError):
            tools._normalize_output_format("html")
        self.assertEqual(tools._normalize_output_format("pptx"), "pptx")

    def test_web_code_is_rejected_before_browser_or_files_are_touched(self):
        with patch.object(tools, "_launch_and_focus_brave") as browser:
            result = tools.build_with_heavy_web_ai("Crea animacion.html con motion graphics")
            self.assertIn('"error"', result)
            self.assertIn("run_terminal_command", result)
            browser.assert_not_called()
