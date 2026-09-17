from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_engine.engine import MettrycAIEngine  # noqa: E402
from ai_engine.schemas import ToolResult  # noqa: E402
from ai_engine.service import ConversationService  # noqa: E402
from ai_engine.router import extract_json_object  # noqa: E402


class FakeLLM:
    """Offline model stub that extracts criteria incrementally across turns."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_fallback(self, messages, **kwargs):
        self.calls += 1
        if kwargs.get("force_json"):
            user_payload = messages[-1]["content"]
            import json

            context = json.loads(user_payload)
            message = context["user_message"].lower()
            analysis = {
                "role": "client",
                "intent": "property_search",
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
                "reasoning_summary": "Turno de prueba.",
            }

            if "mañongo" in message:
                analysis["criteria"].update(
                    {
                        "operation": "sale",
                        "property_type": "casa",
                        "city": "Valencia",
                        "zone": "Mañongo",
                    }
                )
            elif "200" in message:
                analysis["criteria"]["max_budget"] = 200000
            elif "4" in message and "habit" in message:
                analysis["criteria"]["bedrooms"] = 4
            elif "san diego" in message:
                analysis["criteria"].update(
                    {
                        "operation": "sale",
                        "property_type": "casa",
                        "city": "Valencia",
                        "zone": "San Diego",
                        "max_budget": 300000,
                    }
                )
            else:
                analysis["intent"] = "general_information"

            return json.dumps(analysis, ensure_ascii=False)

        return "Perfecto, sigo teniendo en cuenta lo que me indicaste."


async def fake_search(state, analysis):
    return ToolResult(
        ok=True,
        name="search_properties",
        data={
            "properties": [
                {
                    "id": f"TEST-{state.criteria.zone}",
                    "titulo": f"Casa en {state.criteria.zone or 'zona indicada'}",
                }
            ],
            "count": 1,
        },
        message="Búsqueda de prueba.",
    )


async def main() -> None:
    engine = MettrycAIEngine(
        FakeLLM(),
        tools={"search_properties": fake_search},
        max_history=20,
    )
    service = ConversationService(engine, max_sessions=10)

    first = await service.process("whatsapp:+584120000001", "Hola, busco una casa en Mañongo")
    second = await service.process("whatsapp:+584120000001", "Hasta 200 mil")
    third = await service.process("whatsapp:+584120000001", "Y mínimo 4 habitaciones")

    assert first.state.criteria.zone == "Mañongo"
    assert second.state.criteria.zone == "Mañongo"
    assert second.state.criteria.max_budget == 200000
    assert third.state.criteria.zone == "Mañongo"
    assert third.state.criteria.max_budget == 200000
    assert third.state.criteria.bedrooms == 4
    assert len(third.state.history) == 6
    assert third.state.last_properties[0]["id"] == "TEST-Mañongo"

    other = await service.process("whatsapp:+584120000002", "Busco casa en San Diego hasta 300 mil")
    assert other.state.criteria.zone == "San Diego"
    assert other.state.criteria.max_budget == 300000

    original = await service.get_state("whatsapp:+584120000001")
    assert original is not None
    assert original.criteria.zone == "Mañongo"
    assert original.criteria.max_budget == 200000
    assert original.criteria.bedrooms == 4

    assert service.session_count() == 2
    assert await service.clear_session("whatsapp:+584120000002") is True
    assert await service.get_state("whatsapp:+584120000002") is None
    assert service.session_count() == 1

    print("\n✅ CONVERSATION SERVICE TEST OK")
    print(f"Sesiones activas: {service.session_count()}")
    print(f"Sender 1: {original.summary}")
    print(f"Sender 1 mensajes: {len(original.history)}")
    print("Memoria entre turnos: OK")
    print("Aislamiento entre usuarios: OK")
    print("Limpieza de sesión: OK")


if __name__ == "__main__":
    asyncio.run(main())
