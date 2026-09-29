"""Regresiones de negocio: motor real, HTTP/LLM externos controlados."""
import asyncio
import json
import os
import time
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

os.environ["PATY_LEARNING_ENABLED"] = "false"

import httpx
import main as bot
from agente_virtual.bridge import LegacyMettrycBridge
from agente_virtual.engine import AgenteVirtualEngine
from agente_virtual.schemas import TurnAnalysis


def raw_property(code, **extra):
    return dict(id_property=code, title="Casa " + code, type_label="Casa",
                city_label="Valencia", zone_label="Centro", sale_price=120000,
                bedrooms=3, bathrooms=2, garages=1, area=100,
                id_status_on_page="1", id_availability="1", **extra)


class Router:
    def __init__(self, **analysis):
        self.analysis = TurnAnalysis(**analysis)

    async def json_completion(self, *args, **kwargs):
        return self.analysis

    async def completion_with_fallback(self, *args, **kwargs):
        return "Claro, seguimos conversando."


class BotRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.codes = [str(1000000 + i) for i in range(1, 6)]
        self.calls = []
        self.summaries = {code: raw_property(code) for code in self.codes}
        self.details = {code: raw_property(code, description="Tiene planta eléctrica.", built_area="90") for code in self.codes}
        self.users = {}
        self.fail_details = False
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.transport))
        self.patches = [patch.object(bot, "http_client", self.client),
                        patch.object(bot, "WASI_TOKEN", "test-token"),
                        patch.object(bot, "WASI_COMPANY_ID", "test-company"),
                        patch.object(bot, "conversation_store", None),
                        patch.object(bot, "API_KEYS_AGENTES", {"test-key"}),
                        patch.object(bot, "TELEGRAM_ADMIN_IDS", []),
                        patch.object(bot, "AGENTE_VIRTUAL_ACTIVO", False)]
        for item in self.patches:
            item.start()
        bot.sesiones.clear()
        bot.locks_usuarios.clear()
        bot.mensajes_duplicados.clear()
        bot.property_detail_cache.clear()
        bot.inventory_cache.clear()
        bot.inventory_cache.update(inventario=[], ultima_actualizacion=None)
        bot.sheets_cache["ultima_actualizacion"] = datetime.utcnow()
        await bot.actualizar_inventario(force=True)

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()

    def transport(self, request):
        self.calls.append(request)
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"status": "success", "total": len(self.summaries),
                **{str(i): prop for i, prop in enumerate(self.summaries.values())}})
        if self.fail_details:
            return httpx.Response(503, json={"status": "error"})
        if "/user/get/" in request.url.path:
            user_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=self.users.get(user_id, {"status": "error"}))
        code = request.url.path.rsplit("/", 1)[-1]
        code = request.url.params.get("id_property", code)
        return httpx.Response(200, json=self.details.get(code, {"status": "success"}))

    def state(self):
        state = bot.obtener_sesion("test-user")
        state.update(rol="cliente", rol_confirmado=True, confianza_rol=1.0)
        state["filtros"].update(tipo_operacion="venta", tipo_propiedad="casa", ciudad="Valencia")
        state["sin_preferencia"] = ["zona", "presupuesto_max"]
        state["perfil_preferencias_consultadas"] = True
        return state

    def engine(self, **analysis):
        return AgenteVirtualEngine(router=Router(**analysis), bridge=LegacyMettrycBridge(bot))

    async def test_summary_does_not_fill_detail_cache(self):
        self.assertEqual(bot.property_detail_cache, {})
        detail = await bot.consultar_detalle_propiedad_wasi(self.codes[0])
        self.assertEqual(detail["descripcion"], "Tiene planta eléctrica.")
        self.assertEqual(detail["area_construida"], 90)
        self.assertTrue(detail["_detalle_completo"])
        self.assertEqual(len(self.calls), 2)

    async def test_full_detail_cache_expires_and_refresh_invalidates_it(self):
        code = self.codes[0]
        first = await bot.consultar_detalle_propiedad_wasi(code)
        await bot.consultar_detalle_propiedad_wasi(code)
        self.assertEqual(len(self.calls), 2)
        bot.property_detail_cache[code]["_detalle_obtenido_en"] = time.time() - bot.PROPERTY_DETAIL_TTL_SECONDS - 1
        await bot.consultar_detalle_propiedad_wasi(code)
        self.assertEqual(len(self.calls), 3)
        await bot.actualizar_inventario(force=True)
        self.assertEqual(bot.property_detail_cache, {})

    async def test_timeout_never_silently_returns_summary(self):
        self.fail_details = True
        with self.assertRaises(bot.WasiConsultaError):
            await bot.consultar_detalle_propiedad_wasi(self.codes[0])
        reply = await bot.mostrar_inmueble_especifico(self.state(), self.codes[0])
        self.assertIn("No pude consultar", reply)
        self.assertNotIn("No encontré", reply)

    async def test_missing_inactive_and_wrong_ids_are_not_successes(self):
        bridge = LegacyMettrycBridge(bot)
        state = self.state()
        state["propiedad_interes"] = bot.buscar_por_codigo(self.codes[0])
        result = await bridge.detail(state, code="9999999")
        self.assertFalse(result.ok)
        self.assertIsNone(state["propiedad_interes"])
        self.details[self.codes[1]]["id_status_on_page"] = "2"
        result = await bridge.detail(state, code=self.codes[1])
        self.assertFalse(result.ok)
        self.details[self.codes[2]]["id_property"] = "other"
        with self.assertRaises(bot.WasiConsultaError):
            await bot.consultar_detalle_propiedad_wasi(self.codes[2])

    async def test_wasi_active_and_available_fields(self):
        for field, value in [("id_status_on_page", "2"), ("id_status_on_page", "4"),
                             ("id_availability", "2"), ("id_availability", "3")]:
            raw = raw_property("1")
            raw[field] = value
            self.assertFalse(bot.normalizar_propiedad_wasi(raw)["activa"])
        raw = raw_property("1")
        raw["id_status_on_page"] = "3"
        self.assertTrue(bot.normalizar_propiedad_wasi(raw)["activa"])

    async def test_bad_inventory_page_preserves_last_complete_snapshot(self):
        before = deepcopy(bot.inventory_cache["inventario"])
        async def broken(request):
            return httpx.Response(200, json={"status": "error"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
            with patch.object(bot, "http_client", client):
                self.assertFalse(await bot.actualizar_inventario(force=True))
        self.assertEqual(bot.inventory_cache["inventario"], before)
        self.assertEqual((await bot.health())["status"], "degraded")
        reply = await bot.mostrar_propiedades(self.state())
        self.assertIn("confirmar el inventario", reply)
        self.assertNotIn("No encontré casas", reply)

    async def test_empty_successful_inventory_is_valid(self):
        self.summaries.clear()
        self.assertTrue(await bot.actualizar_inventario(force=True))
        self.assertEqual(bot.inventory_cache["inventario"], [])
        self.assertFalse(bot.inventario_necesita_actualizacion())
        self.assertEqual((await bot.health())["status"], "ok")

    async def test_incomplete_pagination_does_not_replace_inventory(self):
        before = deepcopy(bot.inventory_cache["inventario"])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
            200, json={"status": "success", "total": 200, "0": raw_property("9")}))) as client:
            with patch.object(bot, "http_client", client):
                self.assertFalse(await bot.actualizar_inventario(force=True))
        self.assertEqual(bot.inventory_cache["inventario"], before)

    async def test_stale_health_reports_degradation(self):
        bot.inventory_cache["ultima_actualizacion"] = datetime.utcnow() - timedelta(days=2)
        health = await bot.health()
        self.assertEqual(health["estado_inventario"]["estado"], "desactualizado")
        self.assertEqual(health["status"], "degraded")

    async def test_topic_change_does_not_search_or_change_filters(self):
        state = self.state()
        await bot.mostrar_propiedades(state)
        before = deepcopy(state)
        engine = self.engine(intent="conversacion_casual")
        reply = await engine.process("test-user", "Qué bonito está el día, ¿verdad?")
        self.assertEqual(reply, "Claro, seguimos conversando.")
        self.assertEqual(state["ultimo_lote"], before["ultimo_lote"])
        self.assertEqual(state["propiedades_enviadas"], before["propiedades_enviadas"])
        self.assertEqual(state["filtros"], before["filtros"])

    async def test_question_preserves_batch_and_uses_complete_document(self):
        state = self.state()
        await bot.mostrar_propiedades(state)
        batch = list(state["ultimo_lote"])
        async def answer(schema, messages, **kwargs):
            data = json.loads(messages[-1]["content"])
            self.assertEqual(data["id_propiedad_consultada"], batch[1])
            self.assertIn("planta eléctrica", data["fuente_documental"])
            return bot.RespuestaPropiedadIA(respuesta="Sí, la segunda tiene planta eléctrica.")
        with patch.object(bot, "llamar_openrouter_json", side_effect=answer):
            reply = await self.engine(intent="pregunta_propiedad", property_position=2).process(
                "test-user", "¿La segunda tiene planta eléctrica?")
        self.assertIn("segunda tiene planta", reply)
        self.assertEqual(state["ultimo_lote"], batch)
        self.assertEqual(state["propiedad_activa_id"], batch[1])
        result = await LegacyMettrycBridge(bot).detail(state, position=3)
        self.assertTrue(result.ok)
        self.assertEqual(state["propiedad_activa_id"], batch[2])
        self.assertEqual(state["ultimo_lote"], batch)

    async def test_detail_without_code_refreshes_selected_summary(self):
        state = self.state()
        state["propiedad_interes"] = bot.buscar_por_codigo(self.codes[0])
        result = await LegacyMettrycBridge(bot).detail(state, format_legacy=False)
        self.assertTrue(result.data["property"]["detalle_completo"])
        self.assertIn("planta eléctrica", result.data["property"]["descripcion"])

    async def test_model_failure_is_not_reported_as_missing_property_data(self):
        with patch.object(bot, "llamar_openrouter_json", new=AsyncMock(return_value=None)):
            reply = await bot.responder_pregunta_propiedad(self.state(), {"id": self.codes[0]}, "¿Tiene planta?")
        self.assertIn("no pude preparar", reply)
        self.assertNotIn("No encontré ese dato", reply)

    async def test_undocumented_fact_is_returned_without_inventing(self):
        with patch.object(bot, "llamar_openrouter_json", new=AsyncMock(return_value=bot.RespuestaPropiedadIA(
            respuesta="La ficha no especifica si aceptan mascotas.", informacion_no_especificada=["mascotas"]))):
            reply = await bot.responder_pregunta_propiedad(self.state(), {"id": self.codes[0]}, "¿Aceptan mascotas?")
        self.assertIn("no especifica", reply)

    async def test_captador_without_phone_does_not_crash(self):
        state = self.state()
        state["rol"] = "colega_inmobiliario"
        self.details[self.codes[0]]["user_data"] = {"first_name": "Ana", "last_name": "Ejemplo"}
        reply = await bot.atender_solicitud_captador(state, codigo=self.codes[0])
        self.assertIn("Ana Ejemplo", reply)
        self.assertIn("WhatsApp", reply)

    async def test_invalid_position_never_reuses_previous_property(self):
        state = self.state()
        state["propiedad_interes"] = bot.buscar_por_codigo(self.codes[0])
        state["ultimo_lote"] = [self.codes[0]]
        self.assertIsNone(bot.resolver_propiedad_contexto(state, posicion=3))
        self.assertFalse((await LegacyMettrycBridge(bot).detail(state, position=3)).ok)

    async def test_more_options_and_changed_filters(self):
        state = self.state()
        await bot.mostrar_propiedades(state)
        first = set(state["ultimo_lote"])
        await self.engine(intent="mas_propiedades").process("test-user", "Muéstrame más opciones")
        self.assertTrue(first.isdisjoint(state["ultimo_lote"]))
        await self.engine(intent="busqueda_propiedad", max_budget=150000).process("test-user", "Busco hasta 150000 dólares")
        self.assertTrue(first.intersection(state["ultimo_lote"]))

    async def test_both_webhooks_and_duplicate_messages(self):
        self.state()
        engine = self.engine(intent="conversacion_casual")
        with patch.object(bot, "agente_virtual_engine", engine), patch.object(bot, "AGENTE_VIRTUAL_ACTIVO", True):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bot.app), base_url="http://test") as client:
                payload = {"sender": "test-user", "message": "Buenos días", "message_id": "same"}
                first = await client.post("/webhook", json=payload, headers={"x-api-key": "test-key"})
                duplicate = await client.post("/webhook-agente-virtual", json=payload, headers={"x-api-key": "test-key"})
                self.assertEqual(first.status_code, 200)
                self.assertTrue(first.json()["replies"])
                self.assertEqual(duplicate.json(), {"replies": []})

    async def test_failed_turn_can_be_retried(self):
        with patch.object(bot, "procesar_mensaje", new=AsyncMock(side_effect=[RuntimeError("test"), "respuesta"])):
            with self.assertRaises(RuntimeError):
                await bot.procesar_turno("user", "Hola", "retry")
            self.assertEqual(await bot.procesar_turno("user", "Hola", "retry"), "respuesta")

    async def test_legacy_property_question_uses_same_complete_source(self):
        state = self.state()
        state["propiedad_interes"] = bot.buscar_por_codigo(self.codes[0])
        with patch.object(bot, "llamar_openrouter_json", new=AsyncMock(return_value=bot.RespuestaPropiedadIA(respuesta="Tiene planta eléctrica."))):
            reply = await bot.procesar_mensaje("test-user", "¿Tiene planta eléctrica?")
        self.assertIn("planta eléctrica", reply)
        self.assertTrue(state["propiedad_interes"]["_detalle_completo"])

    async def test_role_confirmation_still_resumes_search(self):
        engine = self.engine(intent="busqueda_propiedad", operation="venta", property_type="casa", city="Valencia")
        first = await engine.process("new-user", "Busco una casa en venta en Valencia")
        state = bot.sesiones["new-user"]
        self.assertEqual(state["pregunta_pendiente"], "confirmar_rol")
        self.assertFalse(state["propiedades_enviadas"])
        reply = await engine.process("new-user", "Para mí")
        self.assertNotIn("Opción 1", reply)
        self.assertIn("presupuesto", reply)
        self.assertEqual(state["rol"], "cliente")
        reply = await engine.process("new-user", "Cualquier zona, presupuesto abierto")
        self.assertIn("Opción 1", reply)

    async def test_legacy_turn_is_included_in_learning(self):
        with patch.object(bot.legacy_learning_recorder, "enabled", True), patch.object(
            bot.legacy_learning_recorder, "record_turn"
        ) as record:
            response = await bot.procesar_mensaje("test-user", "Gracias")
        self.assertTrue(response)
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["user_message"], "Gracias")

    async def test_high_intent_offer_follows_confirmed_property_without_changing_fact(self):
        state = self.state()
        state["ultimo_lote"] = [self.codes[0]]
        engine = self.engine(intent="pregunta_propiedad", property_position=1,
                             sales_signal="alta_intencion")
        with patch.object(engine.offer_optimizer, "select", return_value="A"), patch.object(
            bot, "llamar_openrouter_json", new=AsyncMock(return_value=bot.RespuestaPropiedadIA(
                respuesta="Tiene planta eléctrica."))
        ):
            response = await engine.process("test-user", "¿Tiene planta? Quiero avanzar con esta casa")
        self.assertIn("Tiene planta eléctrica.", response)
        self.assertIn("¿Quieres que te contacte?", response)
        self.assertEqual(state["pregunta_pendiente"], "ofrecer_asesor")
        self.assertEqual(state["advisor_offer_variant"], "A")
        self.assertEqual(state["propiedad_activa_id"], self.codes[0])
        self.assertTrue(state["advisor_offer_attempted"])
        state["pregunta_pendiente"] = None
        engine._update_sales_state(state, TurnAnalysis(intent="pregunta_propiedad",
                                                       sales_signal="alta_intencion"))
        self.assertIsNone(state["pregunta_pendiente"])

    async def test_colleague_does_not_get_sales_offer(self):
        state = self.state()
        state["rol"] = "colega_inmobiliario"
        state["ultimo_lote"] = [self.codes[0]]
        engine = self.engine(intent="pregunta_propiedad", property_position=1,
                             sales_signal="alta_intencion")
        with patch.object(bot, "llamar_openrouter_json", new=AsyncMock(return_value=bot.RespuestaPropiedadIA(
            respuesta="Tiene planta eléctrica."))):
            response = await engine.process("test-user", "¿Tiene planta eléctrica?")
        self.assertEqual(response, "Tiene planta eléctrica.")
        self.assertNotIn("advisor_offer_variant", state)

    async def test_wasi_failure_never_appends_an_advisor_offer(self):
        state = self.state()
        state["ultimo_lote"] = [self.codes[0]]
        self.fail_details = True
        engine = self.engine(intent="pregunta_propiedad", property_position=1,
                             sales_signal="alta_intencion")
        response = await engine.process("test-user", "¿Tiene planta? Quiero avanzar")
        self.assertIn("No pude consultar", response)
        self.assertNotIn("asesor de Mettryc", response)
        self.assertNotIn("advisor_offer_variant", state)

    async def test_geographic_question_keeps_search_until_city_is_confirmed(self):
        # Dos ciudades en el inventario de prueba comparten el mismo sector.
        bot.inventory_cache["inventario"][-1].update(ciudad="Cabudare", zona="El Trigal")
        bot.reconstruir_catalogo_geografico()
        state = self.state()
        state["filtros"]["ciudad"] = None
        engine = self.engine(intent="busqueda_propiedad", zone="El Trigal")
        reply = await engine.process("test-user", "Busco en El Trigal")
        self.assertIn("valencia", reply.lower())
        self.assertIn("cabudare", reply.lower())
        await engine.process("test-user", "Valencia")
        self.assertEqual(state["filtros"]["ciudad"].lower(), "valencia")
        self.assertIsNone(state["ambiguedad_geografica"])

    async def test_portal_identifies_property_then_answers_without_asking_role(self):
        engine = self.engine(intent="pregunta_propiedad")
        reply = await engine.process("portal-user", "Tengo preguntas de esta publicación https://inmueble.mercadolibre.com.ve/MLV-123456-casa-aa-1000002-_JM")
        self.assertIn("disponible", reply.lower())
        self.assertEqual(bot.sesiones["portal-user"]["propiedad_activa_id"], "1000002")
        with patch.object(bot, "llamar_openrouter_json", new=AsyncMock(return_value=bot.RespuestaPropiedadIA(respuesta="Tiene planta eléctrica."))):
            reply = await engine.process("portal-user", "¿Tiene planta eléctrica?")
        self.assertEqual(reply, "Tiene planta eléctrica.")

    async def test_colleague_cards_still_include_captador(self):
        state = self.state()
        state["rol"] = "colega_inmobiliario"
        self.details[self.codes[0]]["observations"] = "Nombre: Ana Ejemplo\nWhatsApp: 04141234567"
        self.details[self.codes[0]]["user_data"] = {
            "first_name": "Oficina", "last_name": "Centro", "phone": "04142223333",
        }
        reply = await bot.mostrar_propiedades(state)
        self.assertIn("Asesor Mettryc:* Ana Ejemplo", reply)
        self.assertIn("https://wa.me/584141234567", reply)
        self.assertNotIn("https://wa.me/584142223333", reply)

    async def test_confirmed_lead_still_assigns_using_legacy(self):
        state = self.state()
        state.update(lead_confirmacion_pendiente=True, objetivo="captura_lead")
        state["lead"].update(nombre="Persona Ejemplo", correo="test@example.com", whatsapp="584120000099", whatsapp_confirmado=True)
        with patch.object(bot, "asignar_agente_round_robin", new=AsyncMock(return_value={"nombre": "Agente Ejemplo"})), patch.object(bot, "notificar_lead_cliente", new=AsyncMock(return_value=True)) as notify:
            reply = await self.engine(intent="captura_datos").process("test-user", "Sí")
        notify.assert_awaited_once()
        self.assertEqual(state["objetivo"], "lead_asignado")
        self.assertIn("Agente Ejemplo", reply)

    async def test_redis_failure_returns_503_from_both_webhooks(self):
        from conversation_store import ConversationStoreUnavailable
        store = AsyncMock()
        store.run.side_effect = ConversationStoreUnavailable("offline")
        with patch.object(bot, "conversation_store", store):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bot.app), base_url="http://test") as client:
                for route in ["/webhook", "/webhook-agente-virtual"]:
                    response = await client.post(route, json={"sender": "test-user", "message": "Hola"}, headers={"x-api-key": "test-key"})
                    self.assertEqual(response.status_code, 503)

    async def test_same_property_question_through_legacy_and_virtual_webhooks(self):
        for virtual, route in [(False, "/webhook"), (True, "/webhook"), (True, "/webhook-agente-virtual")]:
            with self.subTest(virtual=virtual, route=route):
                bot.sesiones.clear()
                bot.mensajes_duplicados.clear()
                state = self.state()
                await bot.mostrar_propiedades(state)
                state["propiedad_interes"] = bot.buscar_por_codigo(self.codes[0])
                async def answer(schema, messages, **kwargs):
                    payload = json.loads(messages[-1]["content"])
                    self.assertEqual(payload["id_propiedad_consultada"], self.codes[1])
                    return bot.RespuestaPropiedadIA(respuesta="La segunda tiene planta eléctrica.")
                engine = self.engine(intent="pregunta_propiedad", property_position=2)
                with patch.object(bot, "agente_virtual_engine", engine), patch.object(bot, "AGENTE_VIRTUAL_ACTIVO", virtual), patch.object(bot, "llamar_openrouter_json", side_effect=answer):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bot.app), base_url="http://test") as client:
                        result = await client.post(route, json={"sender": "test-user", "message": "¿La segunda tiene planta eléctrica?", "message_id": "question"}, headers={"x-api-key": "test-key"})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["replies"][0]["message"], "La segunda tiene planta eléctrica.")

    async def test_question_with_explicit_code_answers_fact_instead_of_only_card(self):
        self.state()
        with patch.object(bot, "llamar_openrouter_json", new=AsyncMock(return_value=bot.RespuestaPropiedadIA(respuesta="Tiene planta eléctrica."))):
            reply = await self.engine(intent="pregunta_propiedad").process("test-user", "¿El inmueble 1000002 tiene planta eléctrica?")
        self.assertEqual(reply, "Tiene planta eléctrica.")

    async def test_profile_blocks_both_search_entry_points(self):
        state = self.state()
        state["sin_preferencia"] = []
        state["filtros"]["ciudad"] = None
        for search in (bot.mostrar_propiedades, LegacyMettrycBridge(bot).search):
            result = await search(state)
            text = result if isinstance(result, str) else result.message
            self.assertIn("ciudad", text)
            self.assertIn("presupuesto", text)
            self.assertNotIn("Opción", text)
            self.assertEqual(state["ultimo_lote"], [])

    async def test_optional_preferences_allow_decline_without_loop(self):
        state = self.state()
        state.pop("perfil_preferencias_consultadas")
        reply = await self.engine(intent="busqueda_propiedad").process("test-user", "Busco casas")
        self.assertIn("habitaciones", reply)
        self.assertNotIn("Opción", reply)
        reply = await self.engine(intent="conversacion_casual").process("test-user", "No tengo preferencias")
        self.assertIn("Opción 1", reply)

    async def test_specific_code_bypasses_incomplete_profile(self):
        state = bot.obtener_sesion("test-user")
        reply = await self.engine(intent="detalle_propiedad", property_code=self.codes[0]).process("test-user", "Información del código " + self.codes[0])
        self.assertIn(self.codes[0], reply)
        self.assertNotIn("presupuesto", reply)
        self.assertIsNone(state["filtros"]["ciudad"])

    async def test_initial_does_not_overwrite_total_budget_even_if_model_confuses_it(self):
        state = self.state()
        state["filtros"]["presupuesto_max"] = 40000
        bridge = LegacyMettrycBridge(bot)
        bridge.apply_analysis(state, TurnAnalysis(intent="busqueda_propiedad", max_budget=2000), "Tengo 2000 de inicial y necesito financiamiento")
        self.assertEqual(state["filtros"]["presupuesto_max"], 40000)
        self.assertEqual(state["filtros"]["inicial_disponible"], 2000)
        self.assertTrue(state["filtros"]["requiere_financiamiento"])
        for message in ("Inicial de $2.000", "Dispongo de 2 mil para la inicial", "2000 de inicial"):
            self.assertEqual(bot.detectar_presupuesto(message), 0, message)
        self.assertEqual(bot.detectar_presupuesto("Inicial de 2000, precio máximo 40000"), 40000)

    async def test_colleague_intro_and_role_question_repair(self):
        self.assertEqual(bot.detectar_rol_explicito("Te escribe Ana Ejemplo asesora de Mettryc San Diego"), "colega_inmobiliario")
        self.assertNotEqual(bot.detectar_rol_explicito("Quiero hablar con una asesora de Mettryc"), "colega_inmobiliario")
        state = bot.obtener_sesion("test-user")
        state["pregunta_pendiente"] = "confirmar_rol"
        reply = await self.engine().process("test-user", "?")
        self.assertIn("adaptar la atención", reply)

    async def test_yes_to_visit_requests_full_contact_without_llm_rewrite(self):
        for virtual in (False, True):
            state = self.state()
            state["ultimo_lote"] = [self.codes[0]]
            state["propiedad_activa_id"] = self.codes[0]
            state["objetivo"] = "evaluar_resultados"
            state["historial"] = [{"role": "assistant", "content": "¿Coordinamos una visita?"}]
            if virtual:
                reply = await self.engine().process("test-user", "Sí")
            else:
                reply = await bot.procesar_mensaje("test-user", "Sí")
            self.assertIn("nombre completo", reply.lower())
            self.assertIn("WhatsApp", reply)
            self.assertIn("correo", reply.lower())
            self.assertEqual(state["objetivo"], "captura_lead")

    async def test_unsupported_operations_and_broken_link(self):
        self.state()
        engine = self.engine()
        reply = await engine.process("test-user", "Grábame en tus contactos")
        self.assertIn("no tengo una función", reply)
        reply = await engine.process("test-user", "📷 Envió una foto.")
        self.assertIn("código o enlace", reply)
        bot.sesiones["test-user"]["propiedad_activa_id"] = self.codes[0]
        reply = await engine.process("test-user", "No me abre el link")
        self.assertIn("https://www.mettryc.com/inmueble/" + self.codes[0], reply)

    async def test_card_preserves_zero_unknown_and_decimal_area(self):
        raw = raw_property(self.codes[0])
        for raw_area, expected in (("418.11", 418.11), ("272.99", 272.99), ("1.200,50", 1200.5), ("1,200.50", 1200.5), ("160,25", 160.25)):
            self.assertEqual(bot.extraer_area_principal_wasi({"area": raw_area}), expected)
        raw.update(bedrooms=None, bathrooms=0, garages="0", area="160.25")
        prop = bot.normalizar_propiedad_wasi(raw)
        reply = await bot.formatear_ficha(prop, False)
        self.assertIn("160,25 m²", reply)
        self.assertIn("🛏️ N/D", reply)
        self.assertIn("🛁 0", reply)
        self.assertIn("🚗 0", reply)

    async def test_profile_learning_uses_existing_filters_column(self):
        from agente_virtual.learning import PatyLearningRecorder
        from agente_virtual.learning_analyzer import PatyLearningAnalyzer
        recorder = PatyLearningRecorder()
        recorder.enabled = True
        state = self.state()
        state["filtros"]["caracteristicas"] = ["planta eléctrica"]
        state["estado_conversacion"] = "propiedades_mostradas"
        state["ultima_intencion"] = "busqueda_propiedad"
        with patch.object(recorder, "_append") as append:
            recorder.record_turn(sender="test-user", state=state, user_message="Prueba", assistant_response="Opciones")
        event = append.call_args.args[0][0]
        self.assertEqual(event["filters"]["caracteristicas"], ["planta eléctrica"])
        self.assertEqual(event["filters"]["perfil_faltante"], [])
        # Apps Script devuelve filters como JSON dentro de una celda.
        event["filters"] = json.dumps(event["filters"])
        quality = PatyLearningAnalyzer.summarize([event])["calidad_datos"]
        self.assertEqual(quality["listados_con_perfil_completo"], 1)
        self.assertEqual(quality["listados_con_perfil_incompleto"], 0)

    async def test_interest_selects_previous_option_and_invites_visit_in_both_engines(self):
        for virtual in (False, True):
            with self.subTest(virtual=virtual):
                bot.sesiones.clear()
                state = self.state()
                await bot.mostrar_propiedades(state)
                batch = list(state["ultimo_lote"])
                engine = self.engine(intent="detalle_propiedad", property_position=3,
                                     sales_signal="alta_intencion")
                if virtual:
                    reply = await engine.process("test-user", "Me interesa la opción 3")
                else:
                    with patch.object(bot, "decidir_con_ia", new=AsyncMock(return_value=bot.DecisionAgente())):
                        reply = await bot.procesar_mensaje("test-user", "Me interesa la opción 3")
                self.assertIn("agendar una visita", reply)
                self.assertNotIn("https://www.mettryc.com/inmueble/", reply)
                self.assertEqual(state["propiedad_activa_id"], batch[2])
                self.assertEqual(state["ultimo_lote"], batch)
                self.assertEqual(state["pregunta_pendiente"], "confirmar_visita")
                if virtual:
                    accepted = await engine.process("test-user", "Sí")
                else:
                    accepted = await bot.procesar_mensaje("test-user", "Sí")
                self.assertIn("nombre completo", accepted.lower())
                self.assertIn("correo electrónico", accepted.lower())
                self.assertEqual(state["objetivo"], "captura_lead")

    async def test_interest_failure_never_offers_stale_property(self):
        for issue in ("missing", "inactive", "failure"):
            with self.subTest(issue=issue):
                bot.sesiones.clear()
                state = self.state()
                await bot.mostrar_propiedades(state)
                state["propiedad_interes"] = bot.buscar_por_codigo(self.codes[0])
                state["propiedad_activa_id"] = self.codes[0]
                position = 5
                if issue == "missing":
                    state["ultimo_lote"] = state["ultimo_lote"][:2]
                if issue == "inactive":
                    self.details[self.codes[4]]["id_availability"] = "2"
                if issue == "failure":
                    self.fail_details = True
                try:
                    reply = await self.engine(intent="detalle_propiedad", property_position=position).process(
                        "test-user", f"Me interesa la opción {position}")
                finally:
                    self.fail_details = False
                    self.details[self.codes[4]]["id_availability"] = "1"
                self.assertNotIn("agendar una visita", reply)
                self.assertIsNone(state["propiedad_activa_id"])
                self.assertIsNone(state["propiedad_interes"])

    async def test_property_question_is_not_treated_as_simple_interest(self):
        state = self.state()
        await bot.mostrar_propiedades(state)
        self.assertIsNone(bot.detectar_interes_en_opcion("Me interesa la opción 3, ¿tiene patio?", state))
        self.assertEqual(bot.detectar_interes_en_opcion("Me interesa la última", state), len(state["ultimo_lote"]))
        with patch.object(bot, "llamar_openrouter_json", new=AsyncMock(
            return_value=bot.RespuestaPropiedadIA(respuesta="Esa característica no aparece en la ficha."))):
            reply = await self.engine(intent="pregunta_propiedad", property_position=3).process(
                "test-user", "Me interesa la opción 3, ¿tiene patio?")
        self.assertIn("característica", reply)
        self.assertNotIn("agendar una visita", reply)

    async def test_colleague_selection_preserves_detail_card(self):
        state = self.state()
        state["rol"] = "colega_inmobiliario"
        await bot.mostrar_propiedades(state)
        reply = await self.engine(intent="detalle_propiedad", property_position=2).process(
            "test-user", "Me interesa la opción 2")
        self.assertIn("https://www.mettryc.com/inmueble/" + state["ultimo_lote"][1], reply)
        self.assertIn("Oficina Mettryc:", reply)
        self.assertNotIn("¿Quieres agendar una visita para conocerla?", reply)

    async def test_colleague_contact_sources_never_mix_advisor_and_office(self):
        state = self.state()
        state["rol"] = "colega_inmobiliario"
        code = self.codes[0]
        self.details[code]["user_data"] = {
            "first_name": "Oficina", "last_name": "Centro", "phone": "04142223333",
        }
        for observations, expected_type in (
            ("Nombre: Ana Ejemplo\nWhatsApp: 04141234567", "asesor"),
            ("Nombre: Ana Ejemplo", "oficina"),
            ("WhatsApp: 04141234567", "oficina"),
            ("", "oficina"),
        ):
            with self.subTest(observations=observations):
                self.details[code]["observations"] = observations
                bot.property_detail_cache.clear()
                detail = await bot.consultar_detalle_propiedad_wasi(code)
                contact = bot.obtener_datos_captador(detail)
                self.assertEqual(contact["tipo"], expected_type)
                reply = await bot.atender_solicitud_captador(state, codigo=code)
                if expected_type == "asesor":
                    self.assertIn("Ana Ejemplo", reply)
                    self.assertIn("584141234567", reply)
                    self.assertNotIn("584142223333", reply)
                else:
                    self.assertIn("Oficina Centro", reply)
                    self.assertIn("584142223333", reply)
                    self.assertNotIn("584141234567", reply)
                    self.assertNotIn("El captador", reply)

    async def test_colleague_visit_uses_wasi_even_if_sheet_has_a_different_phone(self):
        state = self.state()
        state["rol"] = "colega_inmobiliario"
        code = self.codes[0]
        self.details[code]["observations"] = "Nombre: Ana Ejemplo\nWhatsApp: 04141234567"
        self.details[code]["user_data"] = {
            "first_name": "Oficina", "last_name": "Centro", "phone": "04142223333",
        }
        bot.sheets_cache["captadores"] = {"Ana Ejemplo": "584149999999"}
        with patch.object(bot, "sincronizar_google_sheet", new=AsyncMock(
            side_effect=AssertionError("No consultar Sheets para contactos"))):
            reply = await bot.iniciar_visita(state, posicion=None, codigo=code)
        self.assertIn("584141234567", reply)
        self.assertNotIn("584149999999", reply)

    async def test_office_whatsapp_prefers_mobile_fields_over_landline(self):
        code = self.codes[0]
        self.details[code]["observations"] = "Nombre: Ana Ejemplo"
        self.details[code]["user_data"] = {
            "first_name": "Oficina", "last_name": "Centro",
            "phone": "02412223333", "cell_phone": "04142223333",
        }
        state = self.state()
        state["rol"] = "colega_inmobiliario"
        reply = await bot.atender_solicitud_captador(state, codigo=code)
        self.assertIn("https://wa.me/584142223333", reply)
        self.assertNotIn("582412223333", reply)
        self.assertNotIn("WASI", reply.upper())

        self.details[code]["user_data"]["whatsapp"] = "04143334444"
        bot.property_detail_cache.clear()
        reply = await bot.atender_solicitud_oficina(state, codigo=code)
        self.assertIn("https://wa.me/584143334444", reply)
        self.assertNotIn("584142223333", reply)
        self.assertNotIn("WASI", reply.upper())

    async def test_office_phone_looks_up_matching_user_profile_when_property_omits_it(self):
        code = self.codes[0]
        self.details[code]["id_user"] = 42
        self.details[code]["observations"] = "Nombre: Ana Ejemplo"
        self.details[code]["user_data"] = {
            "id_user": 42, "first_name": "Mettryc", "last_name": "Valencia",
        }
        self.users["42"] = {
            "status": "success", "id_user": "42", "first_name": "Mettryc",
            "last_name": "Valencia", "phone": "02412223333",
            "cell_phone": "04142223333",
        }
        state = self.state()
        state["rol"] = "colega_inmobiliario"
        reply = await bot.atender_solicitud_captador(state, codigo=code)
        self.assertIn("Mettryc Valencia", reply)
        self.assertIn("https://wa.me/584142223333", reply)
        self.assertNotIn("582412223333", reply)
        self.assertNotIn("WASI", reply.upper())
        calls = [r for r in self.calls if "/user/get/" in r.url.path]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url.params.get("for_contact"), "true")

        reply = await bot.atender_solicitud_oficina(state, codigo=code)
        self.assertIn("https://wa.me/584142223333", reply)
        reply = await bot.iniciar_visita(state, posicion=None, codigo=code)
        self.assertIn("https://wa.me/584142223333", reply)

    async def test_user_profile_with_another_id_is_never_used_as_office(self):
        code = self.codes[0]
        self.details[code]["id_user"] = 42
        self.details[code]["observations"] = ""
        self.details[code]["user_data"] = {
            "first_name": "Mettryc", "last_name": "Valencia",
        }
        self.users["42"] = {
            "status": "success", "id_user": "99", "first_name": "Otro",
            "last_name": "Usuario", "cell_phone": "04149999999",
        }
        state = self.state()
        state["rol"] = "colega_inmobiliario"
        reply = await bot.atender_solicitud_captador(state, codigo=code)
        self.assertIn("Mettryc Valencia", reply)
        self.assertNotIn("https://wa.me/", reply)
        self.assertNotIn("Otro Usuario", reply)

    async def test_colleague_card_gets_office_profile_but_complete_advisor_skips_lookup(self):
        code = self.codes[0]
        self.details[code]["id_user"] = 42
        self.details[code]["user_data"] = {
            "first_name": "Mettryc", "last_name": "Valencia",
        }
        self.users["42"] = {
            "status": "success", "id_user": "42", "first_name": "Mettryc",
            "last_name": "Valencia", "cell_phone": "04142223333",
        }
        state = self.state()
        state["rol"] = "colega_inmobiliario"
        self.details[code]["observations"] = "Nombre: Ana Ejemplo"
        reply = await bot.construir_respuesta_fichas(state, [bot.buscar_por_codigo(code)])
        self.assertIn("https://wa.me/584142223333", reply)
        self.details[code]["observations"] = "Nombre: Ana Ejemplo\nWhatsApp: 04141234567"
        bot.property_detail_cache.clear()
        self.calls.clear()
        reply = await bot.atender_solicitud_captador(state, codigo=code)
        self.assertIn("https://wa.me/584141234567", reply)
        self.assertFalse(any("/user/get/" in r.url.path for r in self.calls))

    async def test_contact_failures_and_missing_phone_hide_inventory_provider(self):
        code = self.codes[0]
        self.details[code]["observations"] = "Nombre: Ana Ejemplo"
        self.details[code]["user_data"] = {
            "first_name": "Oficina", "last_name": "Centro", "phone": "",
        }
        state = self.state()
        state["rol"] = "colega_inmobiliario"
        reply = await bot.atender_solicitud_captador(state, codigo=code)
        self.assertIn("WhatsApp de la oficina:* No disponible", reply)
        self.assertNotIn("WASI", reply.upper())
        self.fail_details = True
        bot.property_detail_cache.clear()
        reply = await bot.atender_solicitud_oficina(state, codigo=code)
        self.assertNotIn("WASI", reply.upper())
        reply = await bot.iniciar_visita(state, posicion=None, codigo=code)
        self.assertNotIn("WASI", reply.upper())

    async def test_colleague_unreachable_advisor_gets_office_in_both_engines(self):
        code = self.codes[0]
        self.details[code]["observations"] = "Nombre: Ana Ejemplo\nWhatsApp: 04141234567"
        self.details[code]["user_data"] = {
            "first_name": "Oficina", "last_name": "Centro", "phone": "04142223333",
        }
        for virtual in (False, True):
            with self.subTest(virtual=virtual):
                bot.sesiones.clear()
                state = self.state()
                state["rol"] = "colega_inmobiliario"
                state["propiedad_activa_id"] = code
                state["propiedad_interes"] = bot.buscar_por_codigo(code)
                self.assertFalse(bot.solicita_ayuda_contacto_oficina("No me responde mi cliente", state))
                self.assertTrue(bot.solicita_ayuda_contacto_oficina("No pude comunicarme con el asesor", state))
                if virtual:
                    reply = await self.engine(intent="conversacion_casual").process(
                        "test-user", "No me responde el asesor, ¿me ayudas?")
                else:
                    reply = await bot.procesar_mensaje("test-user", "No me responde el asesor, ¿me ayudas?")
                self.assertIn("Oficina Centro", reply)
                self.assertIn("584142223333", reply)
                self.assertNotIn("584141234567", reply)

    async def test_missing_office_contact_does_not_invent_one(self):
        code = self.codes[0]
        self.details[code]["observations"] = "Nombre: Ana Ejemplo"
        self.details[code]["user_data"] = {}
        state = self.state()
        state["rol"] = "colega_inmobiliario"
        reply = await bot.atender_solicitud_captador(state, codigo=code)
        self.assertIn("No pude confirmar", reply)
        self.assertNotIn("https://wa.me/", reply)


if __name__ == "__main__":
    unittest.main()
