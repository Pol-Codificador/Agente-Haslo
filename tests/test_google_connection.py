import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import admin


class GoogleConnectionTests(unittest.TestCase):
    def test_disconnect_keeps_credentials_and_remove_deletes_everything(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials = root / "credentials.json"
            token = root / "token.json"
            metadata = root / "google_connection.json"
            credentials.write_text("{}", encoding="utf-8")
            token.write_text("{}", encoding="utf-8")
            metadata.write_text(json.dumps({"filename": "cliente-vortex.json"}), encoding="utf-8")
            with (
                patch.object(admin, "CREDENTIALS_PATH", credentials),
                patch.object(admin, "TOKEN_PATH", token),
                patch.object(admin, "GOOGLE_CONNECTION_META_PATH", metadata),
            ):
                self.assertEqual(admin.google_connection_info()["credential_file"], "cliente-vortex.json")
                admin.disconnect_google()
                self.assertFalse(token.exists())
                self.assertTrue(credentials.exists())
                admin.remove_google_connection()
                self.assertFalse(credentials.exists())
                self.assertFalse(metadata.exists())


if __name__ == "__main__":
    unittest.main()
