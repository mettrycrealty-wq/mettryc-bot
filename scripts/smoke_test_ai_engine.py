"""Offline smoke test for the new Mettryc AI Engine.

Run from the repository root with:
    python scripts/smoke_test_ai_engine.py

No OpenRouter key, WhatsApp connection, WASI connection or production service is
required. The test proves that the engine can interpret a natural request, call a
property-search tool, keep conversation state and produce a safe customer reply.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Allow running this file directly from the repository root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_engine.engine import MettrycAIEngine  # noqa: E402
from ai_engine.schemas import ConversationState, ToolResult, UserTurnAnalysis  # noqa: E402
from ai_engine.router import extract_json_object  # noqa: E402


class FakeLLM:
    """Tiny deterministic fake provider used only by this smoke test."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_fallback(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return json.dumps(
                {
                    "role": "colleague",
                    "intent": "property_search",
                    "criteria": {
                        "operation": "sale",
                        "property_type": "casa",
                        "country": "Venezuela",
                        "state": "Carabobo",
                        "city": "Valencia",
                        "zone": "Mañongo",
                        "min_budget": None,
                        "max_budget": 200000,
                        "bedrooms": None,
                        "bathrooms": None,
                        "parking": None,
                        "min_m2": None,
                        "max_m2": None,
                        "features": [],
                    },
                    "requested_property_code": None,
                    "requested_property_reference": None,
                    "needs_human": False,
                    "reasoning_summary": "Corredor buscando una casa para un cliente.",
                },
                ensure_ascii=False,
            )

        return (
            "Claro. Encontré opciones en Mañongo dentro del presupuesto indicado. "
            "Te comparto las disponibles con sus datos de captación."
        )


class FakeLLMUnknownOperation:
    """Fake provider for verifying that incomplete searches do not hit tools."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_fallback(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return json.dumps(
                {
                    "role": "client",
                    "intent": "property_search",
                    "criteria": {
                        "operation": "unknown",
                        "property_type": "casa",
                        "country": None,
                        "state": None,
                        "city": None,
                        "zone": "Mañongo",
                        "min_budget": None,
                        "max_budget": None,
                        "bedrooms": None,
                        "bathrooms": None,
                        "parking": None,
                        "min_m2": None,
                        "max_m2": None,
                        "features": [],
                    },
                    "requested_property_code": None,
                    "requested_property_reference": None,
                    "needs_human": False,
                    "reasoning_summary": "Cliente que busca una casa en Mañongo.",
                },
                ensure_ascii=False,
            )
        return "¿La buscas en venta o alquiler?"


class FakeLLMReasoningLeak:
    """Fake provider used to verify the final-response safety filter."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_fallback(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return json.dumps(
                {
                    "role": "client",
                    "intent": "general_information",
                    "criteria": {
                        "operation": "unknown",
                        "property_type": None,
                        "country": None,
                        "state": None,
                        "city": None,
                        "zone": None,
                        "min_budget": None,
                        "max_budget": None,
                        "bedrooms": None,
                        "bathrooms": None,
                        "parking": None,
                        "min_m2": None,
                        "max_m2": None,
                        "features": [],
                    },
                    "requested_property_code": None,
                    "requested_property_reference": None,
                    "needs_human": False,
                    "reasoning_summary": "Pregunta general.",
                },
                ensure_ascii=False,
            )
        return (
            "Here's a thinking process:\n\n"
            "1. Analyze the user.\n"
            "2. Follow the rules.\n\n"
            "Draft: Claro, con gusto te ayudo."
        )


async def search_properties(
    state: ConversationState,
    analysis: UserTurnAnalysis,
) -> ToolResult:
    return ToolResult(
        name="search_properties",
        ok=True,
        data={
            "properties": [
                {
                    "code": "DEMO-001",
                    "type": "Casa",
                    "operation": "Venta",
                    "zone": "Mañongo",
                    "price": 185000,
                    "captador": {"name": "Ejemplo Captador", "phone": "+58 000 0000000"},
                }
            ]
        },
        message="1 propiedad de prueba encontrada.",
    )


async def main() -> None:
    # Make sure JSON extraction works before building the engine.
    assert extract_json_object('{"ok": true}') == {"ok": True}

    engine = MettrycAIEngine(
        llm=FakeLLM(),
        tools={"search_properties": search_properties},
    )

    result = await engine.process(
        "Hola, soy corredor y necesito una casa por Mañongo para un cliente, máximo 200 mil"
    )

    assert result.state.role == "colleague"
    assert result.state.intent == "property_search"
    assert result.state.criteria.max_budget == 200000
    assert result.state.criteria.zone == "Mañongo"
    assert result.tool_results and result.tool_results[0].ok
    assert result.state.last_properties
    assert result.reply

    # Incomplete operation must not invoke the inventory tool.
    unknown_llm = FakeLLMUnknownOperation()
    unknown_engine = MettrycAIEngine(
        llm=unknown_llm,
        tools={"search_properties": search_properties},
    )
    unknown_result = await unknown_engine.process("Hola, busco una casa en Mañongo")
    assert unknown_result.state.criteria.operation == "unknown"
    assert not unknown_result.tool_results
    assert unknown_result.reply == "¿La buscas en venta o alquiler?"

    # A model must never expose its thinking process to the customer.
    leak_engine = MettrycAIEngine(llm=FakeLLMReasoningLeak())
    leak_result = await leak_engine.process("¿Tienen propiedades?")
    assert leak_result.reply == "Claro, con gusto te ayudo."
    assert "thinking process" not in leak_result.reply.lower()

    print("\n✅ SMOKE TEST OK")
    print("Rol:", result.state.role)
    print("Intención:", result.state.intent)
    print("Zona:", result.state.criteria.zone)
    print("Presupuesto máximo:", result.state.criteria.max_budget)
    print("Propiedades recibidas:", len(result.state.last_properties))
    print("Respuesta:", result.reply)
    print("Búsqueda incompleta bloqueada: OK")
    print("Filtro anti-razonamiento: OK")


if __name__ == "__main__":
    asyncio.run(main())
