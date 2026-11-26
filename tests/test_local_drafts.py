import tempfile
import unittest
from pathlib import Path
from agent_core.local_drafts import create_html_draft

class LocalDraftTests(unittest.TestCase):
    def test_explicit_html_creates_functional_animation_without_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            result = create_html_draft("Crea un archivo llamado animacion.html con animación", folder)
            self.assertIsNotNone(result)
            text = Path(result).read_text(encoding="utf-8")
            self.assertIn("@keyframes", text)
            self.assertIsNone(create_html_draft("Crea animacion.html", folder))
            self.assertEqual(Path(result).read_text(encoding="utf-8"), text)
    def test_no_write_without_explicit_request_or_project(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(create_html_draft("Explica animacion.html", folder))
            self.assertIsNone(create_html_draft("Crea ../fuera.html", folder))
            self.assertIsNone(create_html_draft("Crea animacion.html", ""))
            self.assertEqual(list(Path(folder).iterdir()), [])
