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
        self.assertEqual(summary["por_rol"]["cliente"]["conversaciones"], 1)
        self.assertEqual(summary["por_rol"]["cliente"]["lead_notificado"], 0)

    def test_colleague_is_reported_separately_from_client(self):
        summary = PatyLearningAnalyzer.summarize([
            {"event_type": "conversation_turn", "conversation_id": "c1", "role": "cliente",
             "schema_version": "2",
             "lead_captured": True, "lead_complete": True, "lead_confirmed": True,
             "notification_sent": True},
            {"event_type": "conversation_turn", "conversation_id": "c2",
             "role": "colega_inmobiliario", "colleague_notified": True},
        ])
        self.assertEqual(summary["por_rol"]["cliente"]["lead_notificado"], 1)
        self.assertEqual(summary["por_rol"]["colega_inmobiliario"]["colega_notificado"], 1)
        self.assertEqual(summary["por_rol"]["colega_inmobiliario"]["lead_notificado"], 0)

    def test_old_partial_contact_is_not_counted_as_qualified_lead(self):
        summary = PatyLearningAnalyzer.summarize([
            {"event_type": "conversation_turn", "conversation_id": "old",
             "role": "cliente", "lead_captured": True}
        ])
        self.assertEqual(summary["conversaciones_con_lead"], 0)
        self.assertEqual(summary["conversaciones_con_contacto_parcial"], 1)

    def test_contact_counts_without_assignment_or_telegram(self):
        summary = PatyLearningAnalyzer.summarize([
            {"event_type": "conversation_turn", "conversation_id": "ana",
             "schema_version": 2, "role": "cliente", "lead_captured": True,
             "lead_complete": True, "lead_confirmed": False,
             "lead_assigned": False, "notification_sent": False},
        ])
        self.assertEqual(summary["conversaciones_con_lead"], 1)
        self.assertEqual(summary["conversaciones_asignadas"], 0)
        self.assertEqual(summary["conversaciones_con_lead_notificado"], 0)

    def test_empty_events_have_zero_coverage(self):
        summary = PatyLearningAnalyzer.summarize([])
        self.assertEqual(summary["calidad_datos"]["turnos_evaluados"], 0)
        self.assertEqual(summary["calidad_datos"]["cobertura_rol"], 0)
