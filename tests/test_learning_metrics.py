"""Las métricas de la rama activa en Render deben sobrevivir al cambio a main."""
import unittest
from agente_virtual.learning_analyzer import PatyLearningAnalyzer


class LearningMetricsTests(unittest.TestCase):
    def test_data_quality_and_roles_on_realistic_turns(self):
        turns = [
            {"event_type": "conversation_turn", "conversation_id": "a", "intent": "busqueda_propiedad",
             "origin": "mercadolibre", "role": "cliente", "sales_signal": "ninguna",
             "sales_next_step": "ninguno"},
            {"event_type": "conversation_turn", "conversation_id": "a", "intent": "pregunta_propiedad",
             "sales_signal": "visita", "sales_next_step": "coordinar visita"},
        ]
        summary = PatyLearningAnalyzer.summarize(turns)
        self.assertEqual(summary["roles"], {"cliente": 1, "sin_rol": 1})
        quality = summary["calidad_datos"]
        self.assertEqual(quality["turnos_evaluados"], 2)
        self.assertEqual(quality["cobertura_conversation_id"], 1)
        self.assertEqual(quality["cobertura_origen"], 0.5)
        self.assertEqual(quality["cobertura_senal_comercial"], 0.5)
        self.assertEqual(quality["cobertura_siguiente_paso"], 0.5)

    def test_empty_events_have_zero_coverage(self):
        summary = PatyLearningAnalyzer.summarize([])
        self.assertEqual(summary["calidad_datos"]["turnos_evaluados"], 0)
        self.assertEqual(summary["calidad_datos"]["cobertura_rol"], 0)
