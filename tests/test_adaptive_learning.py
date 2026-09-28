"""Resultados observables y selección estadística de mensajes."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from agente_virtual.learning import PatyLearningRecorder
from agente_virtual.offer_optimizer import AdaptiveOfferOptimizer, select_winner


class LearningOutcomeTests(unittest.TestCase):
    def test_partial_contact_is_not_a_confirmed_notified_lead(self):
        recorder = PatyLearningRecorder()
        recorder.enabled = True
        events = []
        recorder._append = lambda rows: events.extend(rows)
        state = {"rol": "cliente", "lead": {"nombre": "Ana"}}
        recorder.record_turn(sender="u", state=state, user_message="Soy Ana",
                             assistant_response="Hola Ana")
        self.assertTrue(events[0]["lead_captured"])
        self.assertFalse(events[0]["lead_complete"])
        self.assertFalse(events[0]["lead_confirmed"])
        self.assertFalse(events[0]["notification_sent"])
        first_id = events[0]["conversation_id"]
        next_state = {"rol": "colega_inmobiliario", "estado_conversacion": "colega_notificado"}
        recorder.record_turn(sender="u", state=next_state, user_message="Contacta al equipo",
                             assistant_response="Solicitud enviada")
        self.assertNotEqual(first_id, events[-1]["conversation_id"])
        self.assertTrue(events[-1]["colleague_notified"])

    def test_confirmed_lead_requires_successful_notification(self):
        recorder = PatyLearningRecorder(); recorder.enabled = True
        events = []; recorder._append = lambda rows: events.extend(rows)
        state = {"rol": "cliente", "lead": {"nombre": "Ana", "correo": "a@b.com",
                 "whatsapp": "584121234567", "whatsapp_confirmado": True},
                 "lead_confirmado": True, "agente_asignado": {"nombre": "Luis"},
                 "notificacion_enviada": False}
        recorder.record_turn(sender="a", state=state, user_message="Sí",
                             assistant_response="Confirmado")
        self.assertTrue(events[0]["lead_confirmed"])
        self.assertFalse(events[0]["notification_sent"])
        state["notificacion_enviada"] = True
        recorder.record_turn(sender="a", state=state, user_message="Gracias",
                             assistant_response="De nada")
        self.assertTrue(events[-1]["notification_sent"])


class AdaptiveOffersTests(unittest.IsolatedAsyncioTestCase):
    async def test_select_winner_only_with_strong_evidence(self):
        self.assertIsNone(select_winner({"A": {"offers": 29, "notified": 29},
                                         "B": {"offers": 29, "notified": 0}}))
        self.assertEqual(select_winner({"A": {"offers": 100, "notified": 60},
                                        "B": {"offers": 100, "notified": 10}}), "A")
        self.assertIsNone(select_winner({"A": {"offers": 100, "notified": 30},
                                         "B": {"offers": 100, "notified": 29}}))

    async def test_drive_stats_refresh_is_background_and_stable(self):
        store = AsyncMock(backend_name="google_drive")
        store.request.return_value = {"ok": True, "stats": {
            "A": {"offers": 100, "notified": 10},
            "B": {"offers": 100, "notified": 60},
        }}
        optimizer = AdaptiveOfferOptimizer(lambda: store)
        self.assertEqual(optimizer.select("u"), optimizer.select("u"))
        await optimizer._task
        self.assertEqual(optimizer.select("other"), "B")
        store.request.assert_awaited_once_with("offer_stats")
