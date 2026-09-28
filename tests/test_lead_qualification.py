"""Lead real: nombre completo, WhatsApp y correo; entrega medida aparte."""
import unittest
from unittest.mock import AsyncMock, patch

import main as bot
from agente_virtual.engine import AgenteVirtualEngine
from lead_rules import nombre_prospecto_valido


class LeadQualificationTests(unittest.IsolatedAsyncioTestCase):
    def state(self):
        state = bot.crear_sesion("584120000111")
        state["rol"] = "cliente"
        state["objetivo"] = "captura_lead"
        return state

    async def test_full_name_whatsapp_and_email_are_required(self):
        state = self.state()
        response = await bot.procesar_captura_lead(state, "Ana 0412 1234567")
        self.assertFalse(bot.lead_completo(state))
        self.assertEqual(state["lead"]["nombre"], "Ana")
        self.assertEqual(state["lead"]["whatsapp"], "584121234567")
        self.assertIsNone(state["lead"]["correo"])
        self.assertIn("apellido", response.lower())
        self.assertIn("correo electrónico", response.lower())
        self.assertFalse(state["lead_confirmacion_pendiente"])
        response = await bot.procesar_captura_lead(state, "Mi apellido es Pérez")
        self.assertEqual(state["lead"]["nombre"], "Ana Pérez")
        self.assertIn("correo electrónico", response.lower())
        response = await bot.procesar_captura_lead(state, "ana@example.com")
        self.assertTrue(bot.lead_completo(state))
        self.assertEqual(state["lead"]["correo"], "ana@example.com")
        self.assertIn("✉️ Correo electrónico: ana@example.com", response)
        self.assertIn("👤 Nombre completo: Ana Pérez", response)
        with patch.object(bot, "asignar_agente_round_robin", new=AsyncMock(return_value={"nombre": "Luis"})), \
             patch.object(bot, "notificar_lead_cliente", new=AsyncMock(return_value=True)) as notify:
            confirmation = await bot.completar_y_asignar_lead(state)
        notify.assert_awaited_once()
        self.assertIn("envié el aviso por Telegram", confirmation)
        self.assertEqual(state["estado_conversacion"], "lead_asignado")
        self.assertTrue(state["notificacion_enviada"])

    async def test_name_and_whatsapp_without_email_do_not_assign(self):
        state = self.state()
        state["lead"].update(nombre="Ana Pérez", whatsapp="584121234567", whatsapp_confirmado=True)
        with patch.object(bot, "asignar_agente_round_robin", new=AsyncMock()) as assign:
            response = await bot.completar_y_asignar_lead(state)
        assign.assert_not_awaited()
        self.assertIn("correo electrónico", response.lower())
        self.assertIn("✉️", response)
        self.assertNotIn("👤", response)
        self.assertNotIn("📱", response)

    async def test_initial_question_shows_all_three_required_fields(self):
        response = bot.mensaje_solicitud_datos_lead(self.state(), saludo=True)
        self.assertIn("👤 Tu nombre completo", response)
        self.assertIn("📱 Número de WhatsApp", response)
        self.assertIn("✉️ Correo electrónico", response)

    async def test_virtual_flow_accepts_surname_without_model(self):
        state = self.state()
        state["lead"].update(nombre="Ana", whatsapp="584121234567", whatsapp_confirmado=True)
        response = await AgenteVirtualEngine()._process_lead_flow_deterministic("Pérez", state)
        self.assertEqual(state["lead"]["nombre"], "Ana Pérez")
        self.assertIn("✉️ Correo electrónico", response)

    async def test_missing_name_or_whatsapp_blocks_assignment(self):
        state = self.state()
        state["lead"].update(nombre="Ana", whatsapp=None)
        with patch.object(bot, "asignar_agente_round_robin", new=AsyncMock()) as assign:
            response = await bot.completar_y_asignar_lead(state)
        assign.assert_not_awaited()
        self.assertIn("whatsapp", response.lower())
        self.assertFalse(state["lead_confirmado"])
        self.assertFalse(nombre_prospecto_valido("Ana"))
        self.assertTrue(nombre_prospecto_valido("Ana Pérez"))
        self.assertFalse(nombre_prospecto_valido("Hola"))
        self.assertFalse(bot.nombre_valido("Ana"))  # El criterio del colega no cambia.

    async def test_assignment_failure_does_not_claim_lead(self):
        state = self.state()
        state["lead"].update(nombre="Ana Pérez", whatsapp="584121234567", whatsapp_confirmado=True,
                             correo="ana@example.com")
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
        state["lead"].update(nombre="Ana Pérez", whatsapp="584121234567", whatsapp_confirmado=True,
                             correo="ana@example.com")
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
