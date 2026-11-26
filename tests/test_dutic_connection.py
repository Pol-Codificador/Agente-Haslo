import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import admin


class DuticConnectionTests(unittest.TestCase):
    def test_remove_dutic_deletes_saved_url_and_runtime_value(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("OTHER_SETTING=keep\nDUTIC_ICAL_URL=https://example.test/private.ics\n", encoding="utf-8")
            with patch.object(admin, "ENV_PATH", env), patch.dict(os.environ, {"DUTIC_ICAL_URL": "https://example.test/private.ics"}, clear=False):
                result = admin.remove_dutic_connection()
                content = env.read_text(encoding="utf-8")
                self.assertTrue(result["removed"])
                self.assertNotIn("DUTIC_ICAL_URL", content)
                self.assertIn("OTHER_SETTING=keep", content)
                self.assertNotIn("DUTIC_ICAL_URL", os.environ)


if __name__ == "__main__":
    unittest.main()
