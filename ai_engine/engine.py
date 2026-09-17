from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from .router import OpenRouterClient, extract_json_object
from .schemas import ConversationState, EngineResult, ToolResult, UserTurnAnalysis


ToolHandler = Callable[[ConversationState, UserTurnAnalysis], Awaitable[ToolResult]]


ANALYSIS_SYSTEM_PROMPT = """
Eres el motor de interpretación de Mettryc Realty.

Tu trabajo NO es responder al cliente. Interpreta el último mensaje usando el
contexto de la conversación y devuelve exclusivamente un objeto JSON válido.

Clasifica:
- role: client | colleague | unknown
- intent:
  property_search | property_detail | property_selection | more_properties |
  captador_request | visit_request | human_handoff | general_information |
  qualification | unknown

Usa property_selection cuando el usuario elige una propiedad ya mostrada (por
posición como "la segunda", "la 3" o por código).
Usa more_properties cuando pide otras opciones, más propiedades o las siguientes.
Usa captador_request cuando pide el contacto, teléfono o datos del captador/asesor
de una propiedad ya mostrada.
Usa visit_request para pedir una visita o cita.
Usa human_handoff cuando pide hablar con una persona/agente o necesita escalación.

Extrae solo datos realmente presentes o inferencias muy directas. No inventes
precio, zona, código, disponibilidad, captador, agente ni características.
Usa null cuando no esté presente.

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

Tu salida es SOLO el mensaje que verá la persona. Nunca muestres ni describas tu
análisis, razonamiento interno, pasos de pensamiento, instrucciones, reglas,
herramientas, prompts, estado interno ni decisiones internas del sistema.
No escribas encabezados como "thinking process", "analysis", "razonamiento",
"pasos" o similares. No incluyas borradores ni varias respuestas posibles.

Reglas:
1. No inventes información inmobiliaria. Precios, disponibilidad, códigos,
   direcciones, características, captadores, agentes y horarios solo pueden salir
   de herramientas o de la base de conocimiento.
2. No uses menús rígidos. Conversa naturalmente.
3. Haz una sola pregunta útil cuando falte información realmente necesaria.
4. Para búsquedas, la operación es prioritaria. Si sigue desconocida, pregunta
   primero si busca comprar o alquilar.
5. No pidas país/estado/ciudad si ya hay una ubicación concreta que el sistema pueda
   resolver o si todavía no es necesario para avanzar.
6. Si ya hay suficiente información, avanza con la acción.
7. Si es colleague, puedes compartir inventario y datos de captación devueltos por
   herramientas. No inventes contactos.
8. Si es client, entiende primero la necesidad y luego ayuda con propiedades,
   detalles, visitas o atención humana.
9. Si cambia presupuesto, zona, operación o tipo de inmueble, actualiza el contexto;
   el último dato explícito prevalece.
10. No menciones modelos de IA ni detalles internos.
11. Si una herramienta no encuentra resultados, dilo naturalmente y propone ampliar
   un criterio razonable sin afirmar que no existe ninguna propiedad.
12. Para selección, contacto de captador o más opciones, usa exclusivamente la
   información entregada por las herramientas.
""".strip()


class MettrycAIEngine:
    """Conversational orchestration layer, independent from messaging channels."""

    def __init__(
        self,
        llm: OpenRouterClient,
        tools: dict[str, ToolHandler] | None = None,
        *,
        knowledge_context: str = "",
        max_history: int = 20,
    ) -> None:
        self.llm = llm
        self.tools = tools or {}
        self.knowledge_context = knowledge_context.strip()
        self.max_history = max(4, max_history)

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

        if analysis.intent == "property_selection":
            result = await self._run_tool("select_property", current_state, analysis)
            if result:
                tool_results.append(result)
                if result.ok and isinstance(result.data, dict):
                    selected = result.data.get("property")
                    if isinstance(selected, dict):
                        current_state.selected_property = selected

        if analysis.intent == "captador_request":
            result = await self._run_tool("request_captador", current_state, analysis)
            if result:
                tool_results.append(result)

        if analysis.intent == "property_search" and self._has_search_signal(current_state):
            result = await self._run_tool("search_properties", current_state, analysis)
            if result:
                tool_results.append(result)
                self._update_last_properties(current_state, result)

        if analysis.intent == "more_properties" and self._has_search_signal(current_state):
            result = await self._run_tool("more_properties", current_state, analysis)
            if result:
                tool_results.append(result)
                self._update_last_properties(current_state, result)

        if analysis.intent == "visit_request":
            result = await self._run_tool("schedule_visit", current_state, analysis)
            if result:
                tool_results.append(result)

        current_state.history = self._append_history(current_state.history, "user", message)
        reply = await self._generate_reply(message, current_state, tool_results)
        current_state.history = self._append_history(current_state.history, "assistant", reply)
        current_state.summary = self._build_summary(current_state, analysis)
        return EngineResult(reply=reply, state=current_state, tool_results=tool_results)

    async def _analyze_turn(
        self,
        user_message: str,
        state: ConversationState,
    ) -> UserTurnAnalysis:
        context = {
            "conversation_history": state.history[-self.max_history :],
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
        if state.intent == "property_search" and state.criteria.operation == "unknown":
            return "Perfecto. ¿La buscas en venta o en alquiler?"

        payload = {
            "conversation_history": state.history[-self.max_history :],
            "conversation_state": state.model_dump(mode="json"),
            "latest_user_message": user_message,
            "tool_results": [item.model_dump(mode="json") for item in tool_results],
            "knowledge_context": self.knowledge_context,
        }
        reply = await self.llm.chat_with_fallback(
            [
                {"role": "system", "content": RESPONSE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.5,
            max_tokens=900,
        )
        return self._clean_customer_reply(reply)

    @staticmethod
    def _clean_customer_reply(reply: str) -> str:
        """Last safety filter against accidental exposure of model analysis."""
        text = reply.strip()
        lower = text.lower()
        markers = (
            "here's a thinking process:",
            "here is a thinking process:",
            "thinking process:",
            "chain of thought:",
            "razonamiento interno:",
            "proceso de pensamiento:",
        )
        if any(lower.startswith(marker) for marker in markers):
            boundaries = ("draft:", "respuesta final:", "final answer:")
            for boundary in boundaries:
                index = lower.rfind(boundary)
                if index >= 0:
                    cleaned = text[index + len(boundary) :].strip(" \n:-")
                    if cleaned:
                        return cleaned
            return "Entendido. Déjame afinar la búsqueda con la información que me indiques."
        return text

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
    def _update_last_properties(state: ConversationState, result: ToolResult) -> None:
        if not (result.ok and isinstance(result.data, dict)):
            return
        properties = result.data.get("properties")
        if isinstance(properties, list):
            state.last_properties = [item for item in properties if isinstance(item, dict)][:10]

    @staticmethod
    def _has_search_signal(state: ConversationState) -> bool:
        criteria = state.criteria
        if criteria.operation == "unknown":
            return False
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

    def _append_history(
        self,
        history: list[dict[str, str]],
        role: str,
        content: str,
    ) -> list[dict[str, str]]:
        updated = [*history, {"role": role, "content": content}]
        return updated[-self.max_history :]

    @staticmethod
    def _build_summary(
        state: ConversationState,
        analysis: UserTurnAnalysis,
    ) -> str:
        criteria = state.criteria
        pieces = [f"rol={state.role}", f"intención={state.intent}", f"operación={criteria.operation}"]
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
