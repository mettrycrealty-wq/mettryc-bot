"""Lead real: nombre y WhatsApp, agente asignado y aviso por Telegram."""
import unittest
from unittest.mock import AsyncMock, patch

import main as bot
from lead_rules import nombre_prospecto_valido


class LeadQualificationTests(unittest.IsolatedAsyncioTestCase):
    def state(self):
        state = bot.crear_sesion("584120000111")
        state["rol"] = "cliente"
        state["objetivo"] = "captura_lead"
        return state

    async def test_single_first_name_and_whatsapp_without_email_are_enough(self):
        state = self.state()
        response = await bot.procesar_captura_lead(state, "Ana 0412 1234567")
        self.assertTrue(bot.lead_completo(state))
        self.assertEqual(state["lead"]["nombre"], "Ana")
        self.assertEqual(state["lead"]["whatsapp"], "584121234567")
        self.assertIsNone(state["lead"]["correo"])
        self.assertNotIn("correo electrónico", response.lower())
        self.assertIn("Correo (opcional): No indicado", response)
        with patch.object(bot, "asignar_agente_round_robin", new=AsyncMock(return_value={"nombre": "Luis"})), \
             patch.object(bot, "notificar_lead_cliente", new=AsyncMock(return_value=True)) as notify:
            confirmation = await bot.completar_y_asignar_lead(state)
        notify.assert_awaited_once()
        self.assertIn("envié el aviso por Telegram", confirmation)
        self.assertEqual(state["estado_conversacion"], "lead_asignado")
        self.assertTrue(state["notificacion_enviada"])

    async def test_missing_name_or_whatsapp_blocks_assignment(self):
        state = self.state()
        state["lead"].update(nombre="Ana", whatsapp=None)
        with patch.object(bot, "asignar_agente_round_robin", new=AsyncMock()) as assign:
            response = await bot.completar_y_asignar_lead(state)
        assign.assert_not_awaited()
        self.assertIn("whatsapp", response.lower())
        self.assertFalse(state["lead_confirmado"])
        self.assertTrue(nombre_prospecto_valido("Ana"))
        self.assertFalse(nombre_prospecto_valido("Hola"))
        self.assertFalse(bot.nombre_valido("Ana"))  # El criterio del colega no cambia.

    async def test_assignment_failure_does_not_claim_lead(self):
        state = self.state()
        state["lead"].update(nombre="Ana", whatsapp="584121234567", whatsapp_confirmado=True)
        with patch.object(bot, "asignar_agente_round_robin", new=AsyncMock(return_value=None)), \
             patch.object(bot, "notificar_lead_cliente", new=AsyncMock()) as notify:
            response = await bot.completar_y_asignar_lead(state)
        notify.assert_not_awaited()
        self.assertIn("no pude confirmar la asignación", response)
        self.assertEqual(state["estado_conversacion"], "asignacion_pendiente")
        self.assertTrue(state["lead_confirmacion_pendiente"])
        self.assertFalse(state["notificacion_enviada"])

    async def test_telegram_failure_can_retry_without_assigning_twice(self):
        state = self.state()
        state["lead"].update(nombre="Ana", whatsapp="584121234567", whatsapp_confirmado=True)
        assign = AsyncMock(return_value={"nombre": "Luis"})
        notify = AsyncMock(side_effect=[False, True])
        with patch.object(bot, "asignar_agente_round_robin", new=assign), \
             patch.object(bot, "notificar_lead_cliente", new=notify):
            failed = await bot.completar_y_asignar_lead(state)
            lead_id = state["lead_id"]
            self.assertIn("no pude confirmar el aviso", failed)
            self.assertNotEqual(state["estado_conversacion"], "lead_asignado")
            success = await bot.completar_y_asignar_lead(state)
        self.assertIn("envié el aviso por Telegram", success)
        self.assertEqual(state["lead_id"], lead_id)
        assign.assert_awaited_once()
        self.assertEqual(notify.await_count, 2)
