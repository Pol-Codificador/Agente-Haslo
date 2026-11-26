"""Regresiones de proyectos, carpetas y conversaciones compartidas con WhatsApp."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import chats


class ProjectWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.patches = [patch.object(chats, "BASE_DIR", self.root), patch("agent_core.memory_vault.DB_PATH", self.root / "memory.db"), patch.object(chats, "_busy", set()), patch.object(chats, "_project_targets", {})]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def test_delete_project_removes_history_and_selection_preserves_files(self):
        project = chats.ensure_workspace_project("Eliminar", sender="51900000000", request="Crear")
        folder = Path(project["folder_path"])
        (folder / "keep.txt").write_text("archivo")
        chats.delete_project(project["id"])
        self.assertEqual(chats.list_projects(), [])
        self.assertEqual(chats.list_chats(), [])
        with chats.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM whatsapp_chats").fetchone()[0], 0)
        self.assertTrue((folder / "keep.txt").exists())

    def test_delete_busy_chat_is_rejected(self):
        from fastapi import HTTPException
        chat_id = chats.create_chat(chats.ChatEdit())["id"]
        chats.begin_turn(chat_id, "trabaja")
        with self.assertRaises(HTTPException) as caught:
            chats.delete_chat(chat_id)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(len(chats.list_chats()), 1)

    def test_ai_title_is_saved_and_manual_title_is_respected(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        chat_id = chats.create_chat(chats.ChatEdit())["id"]
        chats.begin_turn(chat_id, "ayudame a preparar mi examen de calculo")
        client = MagicMock()
        client.__enter__.return_value.models.generate_content.return_value = SimpleNamespace(text="Preparación del examen de cálculo", usage_metadata=None)
        with patch("agent_core.admin.get_gemini_keys", return_value=["test"]), patch("google.genai.Client", return_value=client), patch("agent_core.memory_vault.record_model_usage"):
            chats._name_first_conversation(chat_id)
            self.assertEqual(chats.read_chat(chat_id)["title"], "Preparación del examen de cálculo")
            with chats.connection() as conn:
                conn.execute("UPDATE chats SET title='Mi título' WHERE id=?", (chat_id,))
            chats._name_first_conversation(chat_id)
            self.assertEqual(chats.read_chat(chat_id)["title"], "Mi título")
            self.assertEqual(client.__enter__.return_value.models.generate_content.call_count, 1)

    def test_new_chat_reuses_empty_draft_in_same_project(self):
        first = chats.create_chat(chats.ChatEdit())
        again = chats.create_chat(chats.ChatEdit())
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["reused"])
        project = chats.ensure_workspace_project("Separado")
        draft = chats.create_chat(chats.ChatEdit(project_id=project["id"]))
        self.assertNotEqual(first["id"], draft["id"])
        self.assertEqual(draft["id"], chats.create_chat(chats.ChatEdit(project_id=project["id"]))["id"])

    def test_new_chat_preserves_context_and_chats_with_messages(self):
        saved = chats.create_chat(chats.ChatEdit(context="Objetivo importante"))
        draft = chats.create_chat(chats.ChatEdit())
        self.assertNotEqual(saved["id"], draft["id"])
        chats.begin_turn(draft["id"], "Hola")
        new = chats.create_chat(chats.ChatEdit())
        self.assertNotEqual(new["id"], draft["id"])

    def test_whatsapp_creation_registers_folder_project_chat_and_selection(self):
        reply, chat_id = chats.whatsapp_chat_command("51900000000", "Crea un nuevo proyecto llamado Prueba Uno")
        chat = chats.read_chat(chat_id)
        self.assertIn("listo en Vortex", reply)
        self.assertTrue(Path(chat["folder_path"]).is_dir())
        self.assertEqual(len(chat["messages"]), 2)
        self.assertEqual(chats.whatsapp_chat_command("51900000000", "continua")[1], chat_id)
        self.assertEqual(chats.list_projects()[0]["chats"], 1)

    def test_creation_retry_reuses_project_and_initial_chat(self):
        first = chats.ensure_workspace_project("Mi proyecto")
        second = chats.ensure_workspace_project("MI PROYECTO")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["chat_id"], second["chat_id"])
        self.assertEqual(len(chats.list_projects()), 1)

    def test_adopts_existing_folder_and_preserves_its_files(self):
        folder = self.root / "pruebaborrar"
        folder.mkdir()
        existing = folder / "original.txt"
        existing.write_text("Contenido existente")
        result = chats.ensure_workspace_project("pruebaborrar")
        self.assertEqual(Path(result["folder_path"]), folder)
        self.assertEqual(existing.read_text(), "Contenido existente")

    def test_project_can_hold_multiple_independent_chats(self):
        project = chats.create_project(chats.ProjectEdit(title="Aplicación"))
        second = chats.create_chat(chats.ChatEdit(title="Backend", project_id=project["id"]))
        chats.begin_turn(project["chat_id"], "Primer chat")
        chats.end_turn(project["chat_id"], {"reply": "Respuesta"})
        self.assertEqual(chats.read_chat(second["id"])["messages"], [])
        self.assertEqual(chats.list_projects()[0]["chats"], 2)

    def test_migrates_existing_project_schema(self):
        with sqlite3.connect(self.root / "memory.db") as conn:
            conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY,title TEXT NOT NULL)")
            conn.execute("INSERT INTO projects VALUES ('old','Existente')")
        conn.close()
        project = chats.ensure_workspace_project("Existente")
        self.assertEqual(project["id"], "old")
        self.assertTrue(Path(project["folder_path"]).is_dir())

    def test_terminal_context_uses_project_directory(self):
        from agent_core import tools
        project = chats.ensure_workspace_project("Programación")
        with patch.object(tools, "get_security_context", return_value=("chat-" + project["chat_id"], "dashboard", False)), patch.object(tools, "get_current_task_id", return_value="test-task"):
            self.assertEqual(tools.current_project_directory(), Path(project["folder_path"]))
            other = chats.ensure_workspace_project("Otro proyecto")
            chats.set_project_target("test-task", other["chat_id"])
            self.assertEqual(tools.current_project_directory(), Path(other["folder_path"]))

    def test_catalog_contains_workspace_and_chat_relationship(self):
        project = chats.ensure_workspace_project("Proyecto catálogo")
        catalog = chats.project_catalog()
        self.assertEqual(catalog["chats"][0]["project_id"], project["id"])
        self.assertEqual(catalog["projects"][0]["folder_path"], project["folder_path"])

    def test_whatsapp_new_chat_inherits_selected_project(self):
        _, first = chats.whatsapp_chat_command("51900000000", "Crea un proyecto llamado Aplicacion")
        _, second = chats.whatsapp_chat_command("51900000000", "/nuevo Backend")
        self.assertNotEqual(first, second)
        self.assertEqual(chats.read_chat(first)["project_id"], chats.read_chat(second)["project_id"])

    def test_creation_with_implementation_details_routes_task_to_new_chat(self):
        reply, chat_id = chats.whatsapp_chat_command("51900000000", "Crea un proyecto llamado Tienda con Python")
        self.assertIsNone(reply)
        self.assertTrue(chat_id)
        self.assertEqual(chats.list_projects()[0]["title"], "Tienda")
        self.assertEqual(chats.read_chat(chat_id)["messages"], [])

    def test_tool_created_project_receives_the_request_and_whatsapp_selection(self):
        import importlib
        with patch("agent_core.agent.VortexAgent"), patch("agent_core.provider_keys.apply_catalog"):
            server = importlib.import_module("agent_core.server")
        project = chats.ensure_workspace_project("Herramienta")
        with patch.object(server.agent, "process_text", return_value={"reply": "Listo", "project_chat_id": project["chat_id"]}):
            response = server.chat_endpoint(server.ChatRequest(message="Construye la aplicación", sender="51900000000", source="whatsapp"))
        self.assertEqual(response.chat_id, project["chat_id"])
        self.assertEqual(len(chats.read_chat(project["chat_id"])["messages"]), 2)
        self.assertEqual(chats.whatsapp_chat_command("51900000000", "continua")[1], project["chat_id"])

    def test_git_does_not_manage_parent_repository_from_project_folder(self):
        import subprocess
        from agent_core import tools
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True, capture_output=True)
        project = chats.ensure_workspace_project("Dentro de otro repositorio")
        with patch.object(tools, "current_project_directory", return_value=Path(project["folder_path"])):
            result = tools.git_project_manager(action="status")
        self.assertIn("repositorio Git propio", result)


if __name__ == "__main__":
    unittest.main()
