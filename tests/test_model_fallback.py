import unittest
from unittest.mock import patch
from agent_core import agent, config


class ModelFallbackTests(unittest.TestCase):
    def test_both_queues_are_available_after_escalation(self):
        with patch.object(agent, "GEMINI_MODELS_FAST", ["flash-a", "flash-b"]), patch.object(agent, "GEMINI_MODELS_COMPLEX", ["pro-a"]), patch.object(agent, "GEMINI_MODEL", "flash-a"):
            self.assertEqual(agent._select_models_for_queue("crea una pagina", False), ["flash-a", "flash-b", "pro-a"])
            self.assertEqual(agent._select_models_for_queue("crea una pagina", True), ["pro-a", "flash-a", "flash-b"])

    def test_unavailable_models_do_not_hide_healthy_alternatives(self):
        import time
        with patch.object(config, "DEAD_MODELS", {"retired"}), patch.object(config, "MODEL_COOLDOWNS", {"limited": time.time() + 3600}):
            self.assertEqual(config.filter_available_models(["retired", "limited", "working"]), ["working"])

    def test_image_models_never_enter_conversation_fallback(self):
        from agent_core.provider_keys import text_model
        with patch.object(agent, "GEMINI_MODELS_FAST", ["gemini-flash", "nano-banana"]), patch.object(agent, "GEMINI_MODELS_COMPLEX", ["gemini-pro-image", "gemini-pro"]), patch.object(agent, "GEMINI_MODEL", "imagen-4"):
            for escalated in (True, False):
                models = agent._select_models_for_queue("crea index.html", escalated)
                self.assertEqual(set(models), {"gemini-flash", "gemini-pro"})
            self.assertFalse(text_model("nano-banana-pro"))
            self.assertFalse(text_model("gemini-3-pro-image"))
            self.assertTrue(text_model("gemini-2.5-flash"))

    def test_code_requests_do_not_expose_web_generation_tools(self):
        names = {tool.__name__ for tool in agent._tools_for_request("Crea animacion.html en el proyecto")}
        self.assertNotIn("build_with_heavy_web_ai", names)
        self.assertNotIn("create_google_doc_in_brave", names)
        self.assertIn("run_terminal_command", names)
        self.assertFalse(agent._allows_web_generation("usa Gemini para crear codigo"))
        with patch.object(agent, "get_env", return_value="true"):
            self.assertTrue(agent._allows_web_generation("usa Gemini Web para crear un documento"))
        with patch.object(agent, "get_env", return_value="false"):
            self.assertFalse(agent._allows_web_generation("usa Gemini Web para crear un documento"))

    def test_web_generation_is_blocked_at_execution_without_explicit_request(self):
        import json
        result = agent.VortexAgent._call_tool_with_timeout(None, "build_with_heavy_web_ai", {}, "test", user_message="crea animacion.html")
        self.assertIn("error", json.loads(result))

    def test_provider_preference_keeps_cross_provider_backups(self):
        models = ["gemini-a", "gemini-b", "glm/glm-4.7-flash", "glm/glm-4.5-flash"]
        self.assertEqual(agent._alternate_model_providers(models, True), ["glm/glm-4.7-flash", "gemini-a", "glm/glm-4.5-flash", "gemini-b"])
        self.assertEqual(agent._alternate_model_providers(models), ["gemini-a", "glm/glm-4.7-flash", "gemini-b", "glm/glm-4.5-flash"])
