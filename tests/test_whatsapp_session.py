"""Desvinculación sobre una sesión temporal, sin tocar WhatsApp real."""

import importlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class WhatsAppSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch("agent_core.agent.VortexAgent"), patch("agent_core.provider_keys.apply_catalog"):
            cls.server = importlib.import_module("agent_core.server")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        auth = self.root / "auth_info_baileys"
        auth.mkdir()
        (auth / "creds.json").write_text(json.dumps({"registered": True, "me": {"id": "51900000000:1@s.whatsapp.net", "name": "Prueba"}}))
        (self.root / "contacts_cache.json").write_text("[]")
        self.state = {"status": "connected", "detail": "Prueba", "qr_text": "", "updated_at": "", "account_phone": "", "account_name": ""}
        self.patches = [patch.object(self.server, "WHATSAPP_DIR", self.root), patch.object(self.server, "_WA_STATE", self.state), patch.object(self.server, "_find_whatsapp_bridge_procs", return_value=[object()]), patch.object(self.server, "_kill_whatsapp_bridge"), patch.object(self.server, "_restart_whatsapp_bridge"), patch.object(self.server.urllib.request, "urlopen", return_value=io.BytesIO(b'{"revoked":true}'))]
        self.mocks = [item.start() for item in self.patches]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def test_session_uses_account_identity_and_device(self):
        result = self.server.whatsapp_session()
        self.assertEqual(result["phone"], "51900000000")
        self.assertEqual(result["name"], "Prueba")
        self.assertTrue(result["has_session"])

    def test_disconnect_removes_local_session_and_requests_new_qr(self):
        result = self.server.disconnect_whatsapp(self.server.WhatsAppDisconnectRequest(generate_qr=True))
        self.assertTrue(result["revoked"])
        self.assertFalse((self.root / "auth_info_baileys").exists())
        self.assertFalse((self.root / "contacts_cache.json").exists())
        self.assertEqual(self.state["account_phone"], "")
        self.mocks[4].assert_called_once()

    def test_disconnect_without_new_qr_leaves_bridge_stopped(self):
        self.server.disconnect_whatsapp(self.server.WhatsAppDisconnectRequest())
        self.mocks[4].assert_not_called()
        self.assertFalse(self.server.whatsapp_session()["has_session"])


if __name__ == "__main__":
    unittest.main()
