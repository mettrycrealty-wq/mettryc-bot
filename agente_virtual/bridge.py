from __future__ import annotations

from copy import deepcopy
from typing import Any

import httpx

from .schemas import BusinessActionResult, TurnAnalysis


class LegacyMettrycBridge:
    """Puente fino hacia main.py, que sigue siendo la fuente de verdad."""

    def __init__(self, legacy: Any | None = None) -> None:
        self.legacy = legacy

    def load(self) -> Any:
        if self.legacy is None:
            import main as legacy
            self.legacy = legacy

        legacy = self.legacy

        if getattr(legacy, "http_client", None) is None:
            legacy.http_client = httpx.AsyncClient(
                follow_redirects=True,
                trust_env=False,
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=20,
                ),
                headers={"User-Agent": "Mettryc-AgenteVirtual/1.0"},
            )

        return legacy

    async def prepare_data(self) -> None:
        legacy = self.load()

        if not legacy.inventory_cache.get("inventario"):
            await legacy.actualizar_inventario(force=True)
        elif legacy.inventario_necesita_actualizacion():
            await legacy.actualizar_inventario()

        if legacy.sheets_necesita_actualizacion():
            await legacy.sincronizar_google_sheet()

    def get_state(self, sender: str) -> dict:
        return self.load().obtener_sesion(sender)

    def save_state(self, sender: str, state: dict) -> None:
        self.load().guardar_sesion(sender, state)

    def append_history(self, state: dict, role: str, content: str) -> None:
        legacy = self.load()
        legacy.agregar_historial(state, role, content)

    def conversation_context(self, state: dict) -> dict:
        legacy = self.load()

        recent_history = [
            {
                "role": item.get("role"),
                "content": item.get("content"),
            }
            for item in state.get("historial", [])[-14:]
        ]

        properties = []
        for index, property_id in enumerate(
            state.get("ultimo_lote", [])[-5:],
            start=1,
        ):
            property_item = legacy.buscar_por_codigo(str(property_id))
            if not property_item:
                continue

            safe = legacy.detalle_propiedad_para_ia(property_item)
            safe["posicion"] = index
            properties.append(safe)

        interest = state.get("propiedad_interes")
        selected = (
            legacy.detalle_propiedad_para_ia(interest)
            if isinstance(interest, dict)
            else None
        )

        return {
            "role": state.get("rol"),
            "intent": state.get("ultima_intencion"),
            "conversation_status": state.get("estado_conversacion"),
            "goal": state.get("objetivo"),
            "filters": deepcopy(state.get("filtros", {})),
            "pending": state.get("pregunta_pendiente"),
            "last_properties": properties,
            "selected_property": selected,
            "agent_assigned": deepcopy(state.get("agente_asignado")),
            "lead": {
                "nombre": state.get("lead", {}).get("nombre"),
                "correo": state.get("lead", {}).get("correo"),
                "whatsapp_available": bool(
                    state.get("lead", {}).get("whatsapp")
                ),
                "confirmation_pending": bool(
                    state.get("lead_confirmacion_pendiente")
                ),
            },
            "history": recent_history,
        }

    def apply_analysis(
        self,
        state: dict,
        analysis: TurnAnalysis,
        message: str,
    ) -> None:
        legacy = self.load()

        filtros = state.setdefault(
            "filtros",
            {
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
        )

        if analysis.role in {"cliente", "colega_inmobiliario"}:
            state["rol"] = analysis.role
            state["rol_confirmado"] = True
            state["confianza_rol"] = 1.0

        updates = {
            "tipo_operacion": analysis.operation,
            "tipo_propiedad": analysis.property_type,
            "ciudad": analysis.city,
            "zona": analysis.zone,
            "presupuesto_max": analysis.max_budget,
            "habitaciones_min": analysis.bedrooms,
            "banos_min": analysis.bathrooms,
            "garajes_min": analysis.parking,
        }

        for key, value in updates.items():
            if value is not None:
                if key == "tipo_operacion":
                    filtros[key] = value
                elif key == "tipo_propiedad":
                    filtros[key] = legacy.normalizar_tipo_propiedad(value)
                else:
                    filtros[key] = value

        if analysis.features:
            previous = filtros.get("caracteristicas") or []
            filtros["caracteristicas"] = list(
                dict.fromkeys([*previous, *analysis.features])
            )

        if analysis.property_code:
            state["ultima_propiedad_consultada_id"] = str(
                analysis.property_code
            )

        state["ultima_intencion"] = analysis.intent

        # Conservamos dos mecanismos útiles del bot anterior como apoyo
        # determinista: preferencias expresamente abiertas y extracciones
        # técnicas obvias del mensaje.
        if hasattr(legacy, "aplicar_sin_preferencia_desde_texto"):
            legacy.aplicar_sin_preferencia_desde_texto(state, message)
        if hasattr(legacy, "aplicar_extracciones_tecnicas"):
            legacy.aplicar_extracciones_tecnicas(state, message)

    async def search(self, state: dict) -> BusinessActionResult:
        legacy = self.load()

        cantidad = (
            5
            if state.get("rol") == "colega_inmobiliario"
            else getattr(legacy, "MAX_PROPIEDADES_POR_LOTE", 3)
        )

        propiedades, motivo = legacy.buscar_mejores_propiedades(
            state,
            cantidad=cantidad,
        )

        if not propiedades:
            return BusinessActionResult(
                ok=False,
                name="buscar_propiedades",
                data={"count": 0, "reason": motivo},
                message=(
                    "No encontré coincidencias con los criterios actuales "
                    "en el inventario disponible."
                ),
            )

        ids = [
            str(item.get("id"))
            for item in propiedades
            if item.get("id")
        ]

        enviados = state.setdefault("propiedades_enviadas", [])
        for property_id in ids:
            if property_id not in enviados:
                enviados.append(property_id)

        state["ultimo_lote"] = ids
        state["propiedad_activa_id"] = ids[0] if len(ids) == 1 else None
        state["propiedad_interes"] = (
            deepcopy(propiedades[0]) if len(propiedades) == 1 else None
        )
        state["objetivo"] = "evaluar_resultados"
        state["estado_conversacion"] = "propiedades_mostradas"
        state["pregunta_pendiente"] = None

        safe_properties = [
            legacy.detalle_propiedad_para_ia(item)
            for item in propiedades
        ]

        return BusinessActionResult(
            ok=True,
            name="buscar_propiedades",
            data={
                "properties": safe_properties,
                "count": len(safe_properties),
                "reason": motivo,
            },
            message=(
                str(len(safe_properties))
                + " propiedades encontradas en el inventario real."
            ),
        )

    async def detail(
        self,
        state: dict,
        *,
        code: str | None = None,
        position: int | None = None,
    ) -> BusinessActionResult:
        legacy = self.load()
        property_item = None

        if code:
            property_item = await legacy.consultar_detalle_propiedad_wasi(
                str(code)
            )
        elif position and 1 <= position <= len(state.get("ultimo_lote", [])):
            property_id = state["ultimo_lote"][position - 1]
            property_item = await legacy.consultar_detalle_propiedad_wasi(
                str(property_id)
            )
        else:
            property_item = legacy.resolver_propiedad_contexto(state)

        if not property_item:
            return BusinessActionResult(
                ok=False,
                name="detalle_propiedad",
                message=(
                    "No pude identificar una propiedad concreta para "
                    "consultar."
                ),
            )

        property_id = str(property_item.get("id") or "")

        state["propiedad_interes"] = deepcopy(property_item)
        state["propiedad_activa_id"] = property_id
        state["ultima_propiedad_consultada_id"] = property_id

        return BusinessActionResult(
            ok=True,
            name="detalle_propiedad",
            data={
                "property": legacy.detalle_propiedad_para_ia(property_item)
            },
            message=(
                "Detalle real del inmueble "
                + property_id
                + " recuperado."
            ),
        )

    async def captador(
        self,
        state: dict,
        *,
        code: str | None = None,
        position: int | None = None,
    ) -> BusinessActionResult:
        legacy = self.load()

        response = await legacy.atender_solicitud_captador(
            state,
            posicion=position,
            codigo=code,
        )

        return BusinessActionResult(
            ok=True,
            name="captador",
            message=response,
        )

    async def visit(
        self,
        state: dict,
        *,
        code: str | None = None,
        position: int | None = None,
    ) -> BusinessActionResult:
        legacy = self.load()

        response = await legacy.iniciar_visita(
            state,
            posicion=position,
            codigo=code,
        )

        return BusinessActionResult(
            ok=True,
            name="visita",
            message=response,
        )

    async def human(
        self,
        state: dict,
        message: str,
    ) -> BusinessActionResult:
        legacy = self.load()

        response = await legacy.iniciar_atencion_humana(
            state,
            message,
        )

        return BusinessActionResult(
            ok=True,
            name="atencion_humana",
            message=response,
        )

    async def capture_lead(
        self,
        state: dict,
        message: str,
    ) -> BusinessActionResult:
        legacy = self.load()

        response = await legacy.procesar_captura_lead(
            state,
            message,
        )

        return BusinessActionResult(
            ok=True,
            name="captura_lead",
            message=response,
        )

    async def capture_colleague_contact(
        self,
        state: dict,
        message: str,
    ) -> BusinessActionResult:
        legacy = self.load()

        response = await legacy.procesar_captura_contacto_colega(
            state,
            message,
        )

        return BusinessActionResult(
            ok=True,
            name="captura_contacto_colega",
            message=response,
        )

    async def complete_lead(self, state: dict) -> BusinessActionResult:
        legacy = self.load()

        response = await legacy.completar_y_asignar_lead(state)

        return BusinessActionResult(
            ok=True,
            name="asignacion_lead",
            data={
                "agent": deepcopy(state.get("agente_asignado"))
            },
            message=response,
        )

    async def notify_admins(
        self,
        *,
        sender: str,
        state: dict,
        reason: str,
        original_message: str,
    ) -> bool:
        legacy = self.load()
        admins = set(
            getattr(legacy, "TELEGRAM_ADMIN_IDS", []) or []
        )

        if not admins:
            return False

        property_interest = state.get("propiedad_interes") or {}
        agent = state.get("agente_asignado")

        message = (
            "🚨 AVISO AGENTE VIRTUAL METTRYC\n\n"
            + "Motivo: " + reason + "\n"
            + "Sender: " + sender + "\n"
            + "Rol: " + str(state.get("rol") or "no confirmado") + "\n"
            + "Mensaje: " + str(original_message)[:700] + "\n\n"
            + "Intención: " + str(state.get("ultima_intencion") or "N/D") + "\n"
            + "Propiedad: "
            + str(property_interest.get("titulo") or "N/D")
            + " (ID "
            + str(property_interest.get("id") or "N/D")
            + ")\n"
            + "Agente asignado: "
            + (
                str(agent.get("nombre") or "N/D")
                if isinstance(agent, dict)
                else "N/D"
            )
        )

        resultados = [
            await legacy.enviar_telegram(chat_id, message)
            for chat_id in admins
        ]

        return any(resultados)
