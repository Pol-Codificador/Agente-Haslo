import unittest

from agent_core.agent import _internal_component_identity_reply, _vortex_identity_reply


class ComponentIdentityTests(unittest.TestCase):
    def test_whatsapp_connector_has_fixed_identity_reply(self):
        self.assertEqual(
            _internal_component_identity_reply("¿Con qué lenguaje está hecho el conector de WhatsApp?"),
            "El conector de WhatsApp pertenece a Vortex AI Desktop y forma parte de esta aplicación.",
        )

    def test_other_internal_components_hide_implementation_details(self):
        self.assertEqual(
            _internal_component_identity_reply("¿Dónde está el archivo del backend?"),
            "Este componente pertenece a Vortex AI Desktop y forma parte de esta aplicación.",
        )

    def test_visible_feature_questions_can_be_explained(self):
        self.assertIsNone(_internal_component_identity_reply("¿Para qué sirve el menú Memoria?"))

    def test_creator_questions_never_use_a_personal_name(self):
        for question in ("¿Quién te creó?", "¿Quién es tu desarrollador?", "¿Quién eres?"):
            self.assertEqual(
                _vortex_identity_reply(question),
                "Soy Vortex AI Desktop, el asistente de esta aplicación.",
            )


if __name__ == "__main__":
    unittest.main()
