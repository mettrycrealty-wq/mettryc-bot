"""Offline tests for natural conversational follow-ups.

Run from the repository root with:
    python scripts/smoke_test_conversational_tools.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_engine.engine import MettrycAIEngine  # noqa: E402
from ai_engine.schemas import ConversationState, ToolResult, UserTurnAnalysis  # noqa: E402
from tools.mettryc_conversation import (  # noqa: E402
    request_mettryc_captador,
    select_mettryc_property,
)


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_fallback(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return json.dumps(
                {
                    "role": "client",
                    "intent": "property_selection",
                    "criteria": {"operation": "sale"},
                    "requested_property_code": None,
                    "requested_property_reference": "segunda",
                    "needs_human": False,
                    "reasoning_summary": "El cliente selecciona la segunda propiedad mostrada.",
                },
                ensure_ascii=False,
            )
        return "Perfecto, ya tengo seleccionada esa propiedad."


async def main() -> None:
    properties = [
        {
            "id": "1001",
            "code": "VE-1001",
            "type": "Casa",
            "price": 180000,
            "zone": "Mañongo",
            "captador": {"nombre": "Ana Ejemplo", "telefono": "+584120000001"},
        },
        {
            "id": "1002",
            "code": "VE-1002",
            "type": "Casa",
            "price": 195000,
            "zone": "Mañongo",
            "captador": {"nombre": "Carlos Ejemplo", "telefono": "+584120000002"},
        },
    ]

    state = ConversationState(last_properties=properties)
    analysis = UserTurnAnalysis(
        intent="property_selection",
        criteria={"operation": "sale"},
        requested_property_reference="segunda",
    )

    selected = await select_mettryc_property(state, analysis)
    assert selected.ok
    assert selected.data["property"]["code"] == "VE-1002"

    state.selected_property = selected.data["property"]
    captador = await request_mettryc_captador(state, UserTurnAnalysis(intent="captador_request"))
    assert captador.ok
    assert captador.data["captador"]["nombre"] == "Carlos Ejemplo"
    assert captador.data["captador"]["telefono"] == "+584120000002"

    # Engine routing must call selection without touching inventory search.
    async def forbidden_search(state, analysis):
        raise AssertionError("No debe ejecutarse búsqueda al seleccionar una propiedad")

    engine = MettrycAIEngine(
        llm=FakeLLM(),
        tools={
            "select_property": select_mettryc_property,
            "search_properties": forbidden_search,
        },
    )
    engine_result = await engine.process("Quiero la segunda")
    assert engine_result.state.intent == "property_selection"
    assert engine_result.state.selected_property is not None
    assert engine_result.state.selected_property["code"] == "VE-1002"

    print("\n✅ CONVERSATIONAL TOOLS TEST OK")
    print("Selección por posición: OK")
    print("Contacto de captador desde propiedad seleccionada: OK")
    print("Routing evita búsqueda innecesaria: OK")


if __name__ == "__main__":
    asyncio.run(main())
