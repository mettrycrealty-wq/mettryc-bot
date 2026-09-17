from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_engine.channel_adapter import ChannelAdapter, MULTIMEDIA_MARKER  # noqa: E402
from ai_engine.engine import MettrycAIEngine  # noqa: E402
from ai_engine.schemas import ToolResult  # noqa: E402
from ai_engine.service import ConversationService  # noqa: E402


class FakeLLM:
    async def chat_with_fallback(self, messages, **kwargs):
        if kwargs.get("force_json"):
            context = json.loads(messages[-1]["content"])
            text = context["user_message"].lower()
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
                "reasoning_summary": "Prueba de adaptador.",
            }
            if "mañongo" in text:
                analysis["criteria"].update(
                    {
                        "operation": "sale",
                        "property_type": "casa",
                        "city": "Valencia",
                        "zone": "Mañongo",
                    }
                )
            elif "200" in text:
                analysis["criteria"]["max_budget"] = 200000
            else:
                analysis["intent"] = "general_information"
            return json.dumps(analysis, ensure_ascii=False)

        return "Entendido, sigo con la búsqueda."


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
    engine = MettrycAIEngine(FakeLLM(), tools={"search_properties": fake_search})
    service = ConversationService(engine)
    adapter = ChannelAdapter(service)

    first = await adapter.handle_payload(
        {
            "sender": "whatsapp:+584120000001",
            "message": "Hola, busco una casa en Mañongo",
            "message_id": "msg-001",
        }
    )
    second = await adapter.handle_payload(
        {
            "sender": "whatsapp:+584120000001",
            "message": "Hasta 200 mil",
            "message_id": "msg-002",
        }
    )

    assert first["ok"] is True
    assert first["sender"] == "whatsapp:+584120000001"
    assert first["message_id"] == "msg-001"
    assert first["state"]["criteria"]["zone"] == "Mañongo"

    assert second["message_id"] == "msg-002"
    assert second["state"]["criteria"]["zone"] == "Mañongo"
    assert second["state"]["criteria"]["max_budget"] == 200000
    assert second["reply"]

    sender_alias, alias_message, alias_id = adapter.extract_message(
        {"from": "messenger:abc123", "text": "Hola", "id": "fb-001"}
    )
    assert sender_alias == "messenger:abc123"
    assert alias_message == "Hola"
    assert alias_id == "fb-001"

    media_sender, media_message, media_id = adapter.extract_message(
        {
            "sender": "whatsapp:+584120000099",
            "message_id": "media-001",
            "image": {"url": "https://example.invalid/image.jpg"},
        }
    )
    assert media_sender == "whatsapp:+584120000099"
    assert media_message == MULTIMEDIA_MARKER
    assert media_id == "media-001"

    print("\n✅ CHANNEL ADAPTER TEST OK")
    print(f"Sender: {second['sender']}")
    print(f"Message ID: {second['message_id']}")
    print(f"Zona conservada: {second['state']['criteria']['zone']}")
    print(f"Presupuesto conservado: {second['state']['criteria']['max_budget']}")
    print("Webhook flat payload: OK")
    print("Aliases future channels: OK")
    print("Multimedia sin texto: OK")


if __name__ == "__main__":
    asyncio.run(main())
