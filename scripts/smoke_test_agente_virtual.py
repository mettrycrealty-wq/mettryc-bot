from __future__ import annotations

import asyncio
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agente_virtual.bridge import LegacyMettrycBridge
from agente_virtual.engine import AgenteVirtualEngine
from agente_virtual.schemas import TurnAnalysis


class FakeRouter:
    def __init__(self) -> None:
        self.analysis_calls = 0
        self.reply_calls = 0

    async def json_completion(self, schema, messages, **kwargs):
        self.analysis_calls += 1
        context = json.loads(messages[-1]["content"])
        text = context["latest_user_message"].lower()

        if any(
            marker in text
            for marker in ("soy corredor", "soy agente", "soy broker", "para mi cliente")
        ):
            return TurnAnalysis(
                role="colega_inmobiliario",
                intent="busqueda_propiedad",
                operation="venta" if "comprar" in text or "venta" in text else None,
                property_type="casa" if "casa" in text else None,
                zone="Mañongo" if "mañongo" in text else None,
                max_budget=250000 if "250" in text else None,
            )

        if "casa" in text or "comprar" in text or "buscar" in text:
            return TurnAnalysis(
                role="cliente",
                intent="busqueda_propiedad",
                operation="venta" if "comprar" in text else None,
                property_type="casa" if "casa" in text else None,
                zone="Mañongo" if "mañongo" in text else None,
                max_budget=250000 if "250" in text else None,
            )

        if "me interesa" in text or "me encanta" in text or "quiero comprarla" in text:
            return TurnAnalysis(
                role="cliente",
                intent="pregunta_propiedad",
                sales_signal="alta_intencion",
                sales_next_step="asesor",
            )

        if "precio" in text or (
            "propiedad" in text and ("detalle" in text or "esa" in text)
        ):
            return TurnAnalysis(
                role="cliente",
                intent="pregunta_propiedad",
            )

        if "asesor" in text or "persona" in text:
            return TurnAnalysis(
                role="cliente",
                intent="atencion_humana",
                human_requested=True,
            )

        if "dato" in text and "no tienes" in text:
            return TurnAnalysis(
                role="cliente",
                intent="informacion_no_disponible",
                information_not_available=True,
                unknown_information="dato solicitado",
            )

        return TurnAnalysis(
            role="cliente",
            intent="conversacion_casual",
        )

    async def completion_with_fallback(self, messages, **kwargs):
        self.reply_calls += 1
        payload = json.loads(messages[-1]["content"])
        analysis = payload["analysis"]

        if analysis["intent"] == "atencion_humana":
            return "Claro. Ya dejé avisado al equipo para que un asesor te atienda."

        if analysis["intent"] == "informacion_no_disponible":
            return "No quiero inventarte ese dato. Ya avisé al equipo para que lo confirme."

        if analysis["intent"] == "conversacion_casual":
            return "Sí 😊. Y cuando quieras retomamos la propiedad que estábamos viendo."

        return "Entendido. Seguimos desde ahí."


class FakeLegacy:
    def __init__(self) -> None:
        self.inventory_cache = {"inventario": [1]}
        self.sheets_cache = {"ultima_actualizacion": object()}
        self.states = {}
        self.events = []
        self.enviar_telegram_calls = 0

    async def actualizar_inventario(self, force=False):
        self.events.append("refresh_inventory")

    async def sincronizar_google_sheet(self, force=False):
        self.events.append("refresh_sheets")

    def inventario_necesita_actualizacion(self):
        return False

    def sheets_necesita_actualizacion(self):
        return False

    def reconstruir_catalogo_geografico(self):
        self.events.append("rebuild_geo")

    def obtener_sesion(self, sender):
        return self.states.setdefault(
            sender,
            {
                "rol": None,
                "rol_confirmado": False,
                "confianza_rol": 0.0,
                "filtros": {
                    "tipo_operacion": None,
                    "tipo_propiedad": None,
                    "ciudad": None,
                    "zona": None,
                    "presupuesto_max": None,
                    "habitaciones_min": None,
                    "banos_min": None,
                    "garajes_min": None,
                    "caracteristicas": [],
                },
                "historial": [],
                "ultimo_lote": [],
                "propiedades_enviadas": [],
                "lead": {},
                "accion_pendiente_rol": None,
                "pregunta_pendiente": None,
                "ambiguedad_geografica": None,
                "agente_asignado": None,
            },
        )

    def guardar_sesion(self, sender, state):
        self.states[sender] = state

    def agregar_historial(self, state, role, content):
        state.setdefault("historial", []).append(
            {"role": role, "content": content}
        )

    def normalizar_tipo_propiedad(self, value):
        return value

    def aplicar_sin_preferencia_desde_texto(self, state, message):
        return False

    def aplicar_extracciones_tecnicas(self, state, message):
        return False

    def construir_contexto_conocimiento(self):
        return "{}"

    def buscar_por_codigo(self, code):
        return {"id": code, "titulo": "Casa de prueba"}

    def detalle_propiedad_para_ia(self, prop):
        return deepcopy(prop)

    def extraer_codigo_mercadolibre(self, message):
        match = re.search(r"https?://\\S+?(\\d+)-+_JM\\b", message or "", re.IGNORECASE)
        return match.group(1) if match else None

    def extraer_codigo_inmueble(self, message, permitir_solo_digitos=False):
        text = str(message or "").strip()
        match = re.search(r"(?:codigo|código|id|inmueble)\\s*[:#-]?\\s*(\\d{4,})", text, re.IGNORECASE)
        if match:
            return match.group(1)
        if permitir_solo_digitos and re.fullmatch(r"\\d{4,10}", re.sub(r"[\\s.,-]", "", text)):
            return re.sub(r"[\\s.,-]", "", text)
        return None

    def solicita_informacion_propiedad_sin_referencia(self, message):
        text = message.strip().lower()
        return any(marker in text for marker in (
            "informacion de la propiedad",
            "información de la propiedad",
            "ficha de la propiedad",
            "detalles de la propiedad",
            "fotos de la casa",
        ))

    def solicita_informacion_propiedad_sin_referencia(self, message):
        text = message.strip().lower()
        return any(marker in text for marker in (
            "informacion de la propiedad",
            "información de la propiedad",
            "ficha de la propiedad",
            "detalles de la propiedad",
            "fotos de la casa",
        ))
    def detectar_rol_explicito(self, message):
        text = message.strip().lower()
        if "soy corredor" in text or "para mi cliente" in text:
            return "colega_inmobiliario"
        if "para mi" in text or "es para mi" in text:
            return "cliente"
        return None

    def interpretar_respuesta_rol(self, message, state):
        text = message.strip().lower()
        if state.get("pregunta_pendiente") != "confirmar_rol":
            return None
        if text in {"para mi", "para mí", "es para mi", "es para mí"}:
            state["rol"] = "cliente"
            state["rol_confirmado"] = True
            state["pregunta_pendiente"] = None
            return "cliente"
        if text in {"para un cliente", "para mi cliente"}:
            state["rol"] = "colega_inmobiliario"
            state["rol_confirmado"] = True
            state["pregunta_pendiente"] = None
            return "colega_inmobiliario"
        return None

    def rol_esta_confirmado(self, state):
        return bool(
            state.get("rol") in {"cliente", "colega_inmobiliario"}
            and state.get("rol_confirmado", False)
        )

    def mensaje_confirmacion_rol(self):
        return "Antes de continuar, ¿buscas la propiedad para ti o para un cliente?"

    def detectar_zona_ciudad(self, message):
        text = message.strip().lower()
        if "el trigal" in text and "valencia" not in text and "cabudare" not in text:
            return {
                "zona": "El Trigal",
                "ambiguedad": True,
                "ciudades_posibles": ["Valencia", "Cabudare"],
            }
        if "valencia" in text:
            return {"ciudad": "Valencia", "zona": None}
        if "cabudare" in text:
            return {"ciudad": "Cabudare", "zona": None}
        return {}

    def obtener_ciudades_para_zona(self, zone):
        if str(zone).strip().lower() == "el trigal":
            return {"Valencia", "Cabudare"}
        return set()

    def detectar_ciudad_canonica(self, message):
        text = message.strip().lower()
        if "valencia" in text:
            return "Valencia"
        if "cabudare" in text:
            return "Cabudare"
        return None

    async def mostrar_propiedades(self, state):
        self.events.append("search")
        property_item = {
            "id": "1001",
            "titulo": "Casa en Mañongo",
            "zona": "Mañongo",
            "ciudad": "Valencia",
            "precio_venta": 200000,
            "area": 200,
            "habitaciones": 4,
            "banos": 3,
            "garajes": 2,
            "enlace": "https://mettryc.com/p/1001",
        }
        state["ultimo_lote"] = ["1001"]
        state["propiedades_enviadas"] = ["1001"]
        state["estado_conversacion"] = "propiedades_mostradas"
        state["pregunta_pendiente"] = "visita_o_pregunta_propiedad"

        if state.get("rol") == "colega_inmobiliario":
            return (
                "Encontré estas opciones que pueden encajar con lo que buscas:\n\n"
                "Opción 1: Casa en Mañongo\n"
                "👤 *Captador:* Ana Ejemplo\n"
                "📲 *WhatsApp captador:* https://wa.me/584120000001\n\n"
                "Puedes contactar al captador indicado en la ficha."
            )

        return (
            "Encontré estas opciones que pueden encajar con lo que buscas:\n\n"
            "*Opción 1: Casa en Mañongo*\n"
            "📍 Mañongo, Valencia\n"
            "💰 $200.000\n"
            "📐 200 m² | 🛏️ 4 | 🛁 3 | 🚗 2\n"
            "🔗 https://mettryc.com/p/1001\n\n"
            "¿Quieres agendar una visita o prefieres preguntarme algo sobre alguna de estas propiedades?"
        )

    def resolver_propiedad_contexto(self, state):
        self.events.append("resolve_property")
        return {"id": "1001", "titulo": "Casa en Mañongo"}

    async def consultar_detalle_propiedad_wasi(self, code):
        self.events.append("detail")
        return {
            "id": code or "1001",
            "titulo": "Casa en Mañongo",
            "descripcion": "Casa de prueba.",
            "activa": True,
        }

    async def mostrar_inmueble_especifico(self, state, code):
        self.events.append("detail_format")
        state["propiedad_interes"] = {
            "id": code,
            "titulo": "Casa en Mañongo",
            "precio_venta": 200000,
            "area": 200,
            "habitaciones": 4,
            "banos": 3,
            "garajes": 2,
        }
        state["ultimo_lote"] = [code]
        state["propiedad_activa_id"] = code
        return "*Casa en Mañongo*\n💰 $200.000\n🔗 https://mettryc.com/p/1001"

    async def atender_solicitud_captador(self, state, posicion=None, codigo=None):
        self.events.append("captador")
        return "Captador de prueba."

    async def iniciar_visita(self, state, posicion=None, codigo=None):
        self.events.append("visit")
        return "Visita de prueba."

    async def iniciar_atencion_humana(self, state, message):
        self.events.append("human")
        state["objetivo"] = "captura_lead"
        return "Solicitud humana registrada."

    async def procesar_captura_lead(self, state, message):
        self.events.append("lead")
        return "Necesito tus datos."

    async def procesar_captura_contacto_colega(self, state, message):
        self.events.append("colleague_contact")
        return "Datos del colega recibidos."

    async def completar_y_asignar_lead(self, state):
        self.events.append("assign")
        state["agente_asignado"] = {"nombre": "Agente Demo"}
        return "Lead asignado a Agente Demo."

    def solicita_humano(self, message):
        return False

    def es_respuesta_afirmativa(self, message):
        return message.strip().lower() in {"si", "sí"}

    def es_respuesta_negativa(self, message):
        return message.strip().lower() == "no"

    async def enviar_telegram(self, chat_id, message):
        self.enviar_telegram_calls += 1
        return True

    TELEGRAM_ADMIN_IDS = ["admin-demo"]


async def main():
    legacy = FakeLegacy()
    bridge = LegacyMettrycBridge(legacy=legacy)
    router = FakeRouter()
    engine = AgenteVirtualEngine(router=router, bridge=bridge)

    client_sender = "whatsapp:+584120000001"

    response = await engine.process(
        client_sender,
        "Hola, busco una casa en El Trigal para comprar hasta 250 mil.",
    )
    assert "para ti o para un cliente" in response.lower()
    assert legacy.events.count("search") == 0

    response = await engine.process(client_sender, "Para mí")
    assert "valencia" in response.lower() and "cabudare" in response.lower()
    assert "en cuál de esas ciudades" in response.lower()
    assert legacy.states[client_sender]["ambiguedad_geografica"]["zona"] == "El Trigal"

    response = await engine.process(client_sender, "Valencia")
    assert "*Opción 1: Casa en Mañongo*" in response
    assert "💰 $200.000" in response
    assert legacy.states[client_sender]["filtros"]["ciudad"] == "Valencia"
    assert legacy.states[client_sender]["filtros"]["zona"] == "El Trigal"
    assert legacy.events.count("search") == 1

    portal_sender = "whatsapp:+584120000003"
    response = await engine.process(
        portal_sender,
        "Hola, tengo algunas preguntas sobre tu publicación en Mercado Libre: "
        "https://inmueble.mercadolibre.com.ve/MLV-779448427-anexo-en-alquiler-urb-prebo-aa-9464257-_JM",
    )
    assert "disponible" in response.lower()
    assert "para ti o para un cliente" not in response.lower()
    assert legacy.states[portal_sender]["propiedad_interes"]["id"] == "9464257"
    assert legacy.states[portal_sender]["consulta_anuncio_pendiente"] is True

    response = await engine.process(portal_sender, "Sí, quiero más información.")
    assert "*Casa en Mañongo*" in response
    assert "detail_format" in legacy.events

    # Un agradecimiento no debe disparar otra búsqueda.
    search_count = legacy.events.count("search")
    response = await engine.process(portal_sender, "Gracias")
    assert legacy.events.count("search") == search_count
    assert "quedo atento" in response.lower()

    # Un pronombre/vaga referencia mantiene el inmueble de portal en contexto.
    response = await engine.process(portal_sender, "en esto")
    assert "propiedad" in response.lower()
    # Regresión: un agradecimiento no puede disparar una nueva búsqueda
    # solo porque quedaron filtros de propiedad en el estado.
    search_count_before_ack = legacy.events.count("search")
    legacy.states[portal_sender]["filtros"]["tipo_operacion"] = "alquiler"
    legacy.states[portal_sender]["filtros"]["tipo_propiedad"] = "apartamento"
    response = await engine.process(portal_sender, "Gracias")
    assert legacy.events.count("search") == search_count_before_ack
    assert "quedo atento" in response.lower()

    # Regresión: referencias vagas posteriores al anuncio conservan contexto.
    response = await engine.process(portal_sender, "en esto")
    assert "propiedad" in response.lower()

    # Regresión: un estado pendiente de código no puede secuestrar un cambio de tema.
    pending_sender = "whatsapp:+584120000004"
    legacy.states[pending_sender] = deepcopy(legacy.obtener_sesion(portal_sender))
    legacy.states[pending_sender]["pregunta_pendiente"] = "codigo_para_detalle"
    legacy.states[pending_sender]["esperando_codigo"] = True
    response = await engine.process(pending_sender, "¿Cómo se llama la empresa?")
    assert "enviame el código" not in response.lower()
    assert legacy.states[pending_sender]["pregunta_pendiente"] is None

    colleague_sender = "whatsapp:+584120000002"
    response = await engine.process(
        colleague_sender,
        "Hola, soy corredor. Busco una casa en Mañongo para mi cliente, en venta hasta 250 mil.",
    )
    assert "*Captador:* Ana Ejemplo" in response
    assert "https://wa.me/584120000001" in response

    response = await engine.process(
        client_sender,
        "Qué bello está el día, ¿verdad?",
    )
    assert "retomamos" in response.lower()

    response = await engine.process(
        client_sender,
        "¿Cuál es el precio de esa propiedad?",
    )
    assert "detail" in legacy.events
    assert "detail_format" not in legacy.events
    assert "Perfecto" in response or "$" in response

    sales_sender = "whatsapp:+584120000003"
    response = await engine.process(
        sales_sender,
        "Busco una casa en Mañongo para comprar hasta 250 mil.",
    )
    assert "para ti o para un cliente" in response.lower()

    response = await engine.process(sales_sender, "Para mí")
    assert "💰 $200.000" in response

    response = await engine.process(
        sales_sender,
        "Esta casa me interesa mucho, quiero comprarla.",
    )
    assert "quieres que te contacte" in response.lower()
    assert legacy.states[sales_sender]["pregunta_pendiente"] == "ofrecer_asesor"

    response = await engine.process(sales_sender, "Sí")
    assert "human" in legacy.events
    assert legacy.states[sales_sender]["objetivo"] == "captura_lead"

    response = await engine.process(
        client_sender,
        "Ahora sí, quiero hablar con un asesor.",
    )
    assert "human" in legacy.events
    assert legacy.enviar_telegram_calls == 1

    response = await engine.process(
        client_sender,
        "Necesito un dato que no tienes a mano.",
    )
    assert legacy.enviar_telegram_calls == 2
    assert "dato" in response.lower()

    print("\n✅ AGENTE VIRTUAL SMOKE TEST OK")
    print("Confirmación de rol antes de búsqueda: OK")
    print("Desambiguación geográfica El Trigal: OK")
    print("Búsqueda natural + ficha cliente: OK")
    print("Ficha para colega + captador: OK")
    print("Cambio de tema casual: OK")
    print("Pregunta sobre propiedad sin repetir ficha completa: OK")
    print("Solicitud humana + aviso administrativo: OK")
    print("Alta intención cliente + oferta de asesor + inicio de lead: OK")
    print("Mercado Libre: disponibilidad inmediata + ficha bajo pedido: OK")
    print("Información no disponible + aviso administrativo: OK")


if __name__ == "__main__":
    asyncio.run(main())
