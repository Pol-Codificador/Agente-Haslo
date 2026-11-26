import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_core import admin, tools

class WebGenerationSettingsTests(unittest.TestCase):
    def test_preference_persists_without_overwriting_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("OTHER_SETTING=keep\n")
            with patch.object(admin, "ENV_PATH", env), patch.dict(os.environ):
                admin.save_web_generation(admin.WebGenerationSettings(enabled=True, browser="edge"))
                self.assertIn("OTHER_SETTING=keep", env.read_text())
                self.assertEqual(os.environ["WEB_AI_BROWSER"], "edge")
                self.assertEqual(os.environ["ALLOW_WEB_GENERATION"], "true")

    def test_selected_browser_is_launched_without_using_default_browser(self):
        token = tools._WEB_AI_BROWSER_CONTEXT.set("chrome")
        try:
            with patch.object(tools, "_selected_web_browser_executable", return_value="chrome.exe"), patch.object(tools.subprocess, "Popen") as launch, patch.object(tools, "_RECENT_BRAVE_URLS", {}):
                tools._open_url_in_brave("https://gemini.google.com/", new_window=True)
                self.assertEqual(launch.call_args.args[0], ["chrome.exe", "--new-window", "https://gemini.google.com/"])
        finally:
            tools._WEB_AI_BROWSER_CONTEXT.reset(token)
