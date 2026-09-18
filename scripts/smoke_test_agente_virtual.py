from __future__ import annotations

import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agente_virtual.engine import AgenteVirtualEngine
from agente_virtual.schemas import TurnAnalysis
from agente_virtual.bridge import LegacyMettrycBridge


class FakeRouter:
    def __init__(self) -> None:
        self.analysis_calls = 0
        self.reply_calls = 0

    async def json_completion(self, schema, messages, **kwargs):
        self.analysis_calls += 1
        context = json.loads(messages[-1]["content"])
        text = context["latest_user_message"].lower()

        if "casa" in text or "comprar" in text or "buscar" in text:
            return TurnAnalysis(
                role="cliente",
                intent="busqueda_propiedad",
                operation="venta" if "comprar" in text else None,
                property_type="casa" if "casa" in text else None,
                zone="Mañongo" if "mañongo" in text else None,
                max_budget=250000 if "250" in text else None,
            )

        if "propiedad" in text and ("detalle" in text or "esa" in text):
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
        results = payload["business_results"]

        if results:
            return "Perfecto 😊. Ya revisé la información y seguimos desde ahí."

        if payload["analysis"]["intent"] == "atencion_humana":
            return "Claro. Ya dejé avisado al equipo para que un asesor te atienda."

        if payload["analysis"]["intent"] == "informacion_no_disponible":
            return "No quiero inventarte ese dato. Ya avisé al equipo para que lo confirme."

        if payload["analysis"]["intent"] == "conversacion_casual":
            return "Sí 😊. Y cuando quieras retomamos la propiedad que estábamos viendo."

        return "Entendido. Cuéntame un poco más y seguimos."


class FakeLegacy:
    def __init__(self) -> None:
        self.inventory_cache = {"inventario": [1]}
        self.sheets_cache = {"ultima_actualizacion": object()}
        self.locks_usuarios = {}
        self.states = {}
        self.events = []

    async def actualizar_inventario(self, force=False):
        self.events.append("refresh_inventory")

    async def sincronizar_google_sheet(self, force=False):
        self.events.append("refresh_sheets")

    def inventario_necesita_actualizacion(self):
        return False

    def sheets_necesita_actualizacion(self):
        return False

    def obtener_sesion(self, sender):
        return self.states.setdefault(
            sender,
            {
                "rol": None,
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
        return {
            "id": code,
            "titulo": "Casa de prueba",
        }

    def detalle_propiedad_para_ia(self, prop):
        return deepcopy(prop)

    def buscar_mejores_propiedades(self, state, cantidad=3):
        self.events.append("search")
        return (
            [
                {
                    "id": "1001",
                    "titulo": "Casa en Mañongo",
                    "zona": "Mañongo",
                    "precio_venta": 200000,
                }
            ],
            "",
        )

    def resolver_propiedad_contexto(self, state):
        self.events.append("resolve_property")
        return {
            "id": "1001",
            "titulo": "Casa en Mañongo",
        }

    async def consultar_detalle_propiedad_wasi(self, code):
        self.events.append("detail")
        return {
            "id": code or "1001",
            "titulo": "Casa en Mañongo",
            "descripcion": "Casa de prueba.",
        }

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

    def detectar_rol_explicito(self, message):
        return None

    def solicita_humano(self, message):
        return False

    def es_respuesta_afirmativa(self, message):
        return message.strip().lower() in {"si", "sí"}

    def es_respuesta_negativa(self, message):
        return message.strip().lower() == "no"

    enviar_telegram_calls = 0

    async def enviar_telegram(self, chat_id, message):
        self.enviar_telegram_calls += 1
        return True

    TELEGRAM_ADMIN_IDS = ["admin-demo"]


async def main():
    legacy = FakeLegacy()
    bridge = LegacyMettrycBridge(legacy=legacy)
    router = FakeRouter()
    engine = AgenteVirtualEngine(router=router, bridge=bridge)

    sender = "whatsapp:+584120000001"

    response = await engine.process(
        sender,
        "Hola, busco una casa en Mañongo para comprar hasta 250 mil.",
    )
    assert "revisé" in response.lower()
    assert "search" in legacy.events

    response = await engine.process(
        sender,
        "Qué bello está el día, ¿verdad?",
    )
    assert "retomamos" in response.lower()

    response = await engine.process(
        sender,
        "Quiero saber más detalles de esa propiedad.",
    )
    assert "detail" in legacy.events

    response = await engine.process(
        sender,
        "Ahora sí, quiero hablar con un asesor.",
    )
    assert "human" in legacy.events
    assert legacy.enviar_telegram_calls == 1

    response = await engine.process(
        sender,
        "Necesito un dato que no tienes a mano.",
    )
    assert legacy.enviar_telegram_calls == 2
    assert "confirm" in response.lower()

    state = legacy.states[sender]
    assert len(state["historial"]) == 10

    print("\n✅ AGENTE VIRTUAL SMOKE TEST OK")
    print("Búsqueda natural: OK")
    print("Cambio de tema casual: OK")
    print("Regreso al contexto de propiedad: OK")
    print("Solicitud humana + aviso administrativo: OK")
    print("Información no disponible + aviso administrativo: OK")


if __name__ == "__main__":
    asyncio.run(main())
