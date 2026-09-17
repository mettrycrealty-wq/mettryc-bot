from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from .router import OpenRouterClient, extract_json_object
from .schemas import (
    ConversationState,
    EngineResult,
    ToolResult,
    UserTurnAnalysis,
)


ToolHandler = Callable[[ConversationState, UserTurnAnalysis], Awaitable[ToolResult]]


ANALYSIS_SYSTEM_PROMPT = """
Eres el motor de interpretación de Mettryc Realty.

Tu trabajo NO es responder al cliente. Tu trabajo es interpretar el último mensaje
con contexto de la conversación y devolver exclusivamente un objeto JSON válido.

Clasifica:
- role: client | colleague | unknown
  * colleague = corredor, agente, asesor inmobiliario, inmobiliaria externa o alguien
    que habla explícitamente de "mi cliente" / "un cliente" para buscar inventario.
  * client = persona que busca para sí misma o para su familia.
- intent:
  property_search | property_detail | visit_request | human_handoff |
  general_information | qualification | unknown

Extrae solo datos que realmente aparecen o que son una inferencia muy directa.
No inventes precio, zona, código, disponibilidad, captador, agente ni características.
Usa null cuando un dato no esté presente.

Devuelve exactamente estas claves:
{
  "role": "client|colleague|unknown",
  "intent": "...",
  "criteria": {
    "operation": "sale|rent|unknown",
    "property_type": null,
    "country": null,
    "state": null,
    "city": null,
    "zone": null,
    "min_budget": null,
    "max_budget": null,
    "bedrooms": null,
    "bathrooms": null,
    "parking": null,
    "min_m2": null,
    "max_m2": null,
    "features": []
  },
  "requested_property_code": null,
  "requested_property_reference": null,
  "needs_human": false,
  "reasoning_summary": "breve resumen factual"
}
""".strip()


RESPONSE_SYSTEM_PROMPT = """
Eres el asistente virtual de Mettryc Realty. Conversas por mensajería como un
asistente humano profesional: natural, breve y atento al contexto.

Reglas fundamentales:
1. No inventes información inmobiliaria. Precios, disponibilidad, códigos,
   direcciones, características, captadores, agentes y horarios solo pueden salir
   de los resultados de herramientas o de la base de conocimiento entregada.
2. No repitas un menú rígido. Responde de forma conversacional.
3. Haz una sola pregunta útil cuando falte información realmente necesaria.
4. Si ya tienes suficiente información, avanza con la búsqueda o la acción.
5. Si el interlocutor es colleague, puedes compartir inventario y los datos de
   captación que devuelva la herramienta. No inventes contactos.
6. Si es client, concéntrate primero en entender qué necesita y luego en ayudarlo
   con propiedades, detalles, visitas o contacto humano.
7. Si el usuario cambia presupuesto, zona, operación o tipo de inmueble, actualiza
   el contexto; el último dato explícito prevalece.
8. No menciones nombres de modelos de IA ni detalles internos del sistema.
9. Si una herramienta no encontró resultados, dilo con naturalidad y propone
   ampliar un criterio razonable sin afirmar que no existe ninguna propiedad.
10. Si solicita una persona, entrega la solicitud de escalación usando la acción
    disponible; no prometas una llamada ni una respuesta en un tiempo concreto.
""".strip()


class MettrycAIEngine:
    """Conversational orchestration layer, independent from messaging channels."""

    def __init__(
        self,
        llm: OpenRouterClient,
        tools: dict[str, ToolHandler] | None = None,
        *,
        knowledge_context: str = "",
    ) -> None:
        self.llm = llm
        self.tools = tools or {}
        self.knowledge_context = knowledge_context.strip()

    async def process(
        self,
        user_message: str,
        state: ConversationState | None = None,
    ) -> EngineResult:
        message = user_message.strip()
        if not message:
            raise ValueError("El mensaje del usuario no puede estar vacío.")

        current_state = state or ConversationState()
        analysis = await self._analyze_turn(message, current_state)
        current_state = self._merge_state(current_state, analysis)

        tool_results: list[ToolResult] = []

        if analysis.needs_human or analysis.intent == "human_handoff":
            result = await self._run_tool("human_handoff", current_state, analysis)
            if result:
                tool_results.append(result)

        if analysis.intent == "property_detail":
            result = await self._run_tool("property_detail", current_state, analysis)
            if result:
                tool_results.append(result)

        if analysis.intent == "property_search" and self._has_search_signal(current_state):
            result = await self._run_tool("search_properties", current_state, analysis)
            if result:
                tool_results.append(result)
                if result.ok and isinstance(result.data, dict):
                    current_state.last_properties = self._safe_property_list(
                        result.data.get("properties")
                    )

        if analysis.intent == "visit_request":
            result = await self._run_tool("schedule_visit", current_state, analysis)
            if result:
                tool_results.append(result)

        reply = await self._generate_reply(message, current_state, tool_results)
        current_state.summary = self._build_summary(current_state, analysis)
        return EngineResult(reply=reply, state=current_state, tool_results=tool_results)

    async def _analyze_turn(
        self,
        user_message: str,
        state: ConversationState,
    ) -> UserTurnAnalysis:
        context = {
            "state": state.model_dump(mode="json"),
            "user_message": user_message,
        }
        raw = await self.llm.chat_with_fallback(
            [
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            temperature=0,
            max_tokens=900,
            force_json=True,
        )
        try:
            return UserTurnAnalysis.model_validate(extract_json_object(raw))
        except (ValueError, ValidationError) as exc:
            raise RuntimeError(f"No se pudo validar la interpretación del turno: {exc}") from exc

    async def _generate_reply(
        self,
        user_message: str,
        state: ConversationState,
        tool_results: list[ToolResult],
    ) -> str:
        payload = {
            "conversation_state": state.model_dump(mode="json"),
            "latest_user_message": user_message,
            "tool_results": [item.model_dump(mode="json") for item in tool_results],
            "knowledge_context": self.knowledge_context,
        }
        return await self.llm.chat_with_fallback(
            [
                {"role": "system", "content": RESPONSE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.5,
            max_tokens=900,
        )

    async def _run_tool(
        self,
        name: str,
        state: ConversationState,
        analysis: UserTurnAnalysis,
    ) -> ToolResult | None:
        handler = self.tools.get(name)
        if handler is None:
            return ToolResult(
                ok=False,
                name=name,
                data=None,
                message=f"La acción {name} todavía no está conectada al sistema.",
            )
        return await handler(state, analysis)

    @staticmethod
    def _has_search_signal(state: ConversationState) -> bool:
        criteria = state.criteria
        return any(
            [
                criteria.property_type,
                criteria.city,
                criteria.zone,
                criteria.max_budget is not None,
                criteria.min_budget is not None,
                criteria.bedrooms is not None,
                criteria.bathrooms is not None,
                criteria.parking is not None,
                criteria.min_m2 is not None,
                criteria.max_m2 is not None,
                bool(criteria.features),
            ]
        )

    @staticmethod
    def _merge_state(
        state: ConversationState,
        analysis: UserTurnAnalysis,
    ) -> ConversationState:
        merged = state.model_copy(deep=True)
        if analysis.role != "unknown":
            merged.role = analysis.role
        if analysis.intent != "unknown":
            merged.intent = analysis.intent

        old = merged.criteria.model_dump()
        new = analysis.criteria.model_dump()
        for key, value in new.items():
            if key == "features":
                if value:
                    old[key] = list(dict.fromkeys([*(old.get(key) or []), *value]))
            elif value is not None and value != "unknown":
                old[key] = value
        merged.criteria = type(merged.criteria).model_validate(old)
        return merged

    @staticmethod
    def _safe_property_list(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)][:10]

    @staticmethod
    def _build_summary(
        state: ConversationState,
        analysis: UserTurnAnalysis,
    ) -> str:
        criteria = state.criteria
        pieces = [
            f"rol={state.role}",
            f"intención={state.intent}",
            f"operación={criteria.operation}",
        ]
        if criteria.property_type:
            pieces.append(f"tipo={criteria.property_type}")
        if criteria.city:
            pieces.append(f"ciudad={criteria.city}")
        if criteria.zone:
            pieces.append(f"zona={criteria.zone}")
        if criteria.max_budget is not None:
            pieces.append(f"presupuesto_max={criteria.max_budget:g}")
        if criteria.bedrooms is not None:
            pieces.append(f"habitaciones={criteria.bedrooms}")
        if criteria.bathrooms is not None:
            pieces.append(f"baños={criteria.bathrooms}")
        if criteria.parking is not None:
            pieces.append(f"puestos={criteria.parking}")
        if analysis.reasoning_summary:
            pieces.append(f"último_turno={analysis.reasoning_summary}")
        return " | ".join(pieces)
