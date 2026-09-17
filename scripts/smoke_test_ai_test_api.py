from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from ai_engine.engine import MettrycAIEngine  # noqa: E402
from ai_engine.schemas import ToolResult  # noqa: E402
from ai_engine.service import ConversationService  # noqa: E402
from ai_engine.test_app import create_ai_test_app  # noqa: E402


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
                "reasoning_summary": "Prueba de API local.",
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

        return "Entendido. Mantengo el contexto de tu búsqueda."


async def fake_search(state, analysis):
    return ToolResult(
        ok=True,
        name="search_properties",
        data={
            "properties": [
                {
                    "id": "TEST-MAÑONGO",
                    "titulo": "Casa de prueba en Mañongo",
                }
            ],
            "count": 1,
        },
        message="Búsqueda local de prueba.",
    )


def main() -> None:
    engine = MettrycAIEngine(FakeLLM(), tools={"search_properties": fake_search})
    service = ConversationService(engine)
    app = create_ai_test_app(service)

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True

        first = client.post(
            "/ai-test",
            json={
                "sender": "whatsapp:+584120000001",
                "message": "Hola, busco una casa en Mañongo",
                "message_id": "msg-001",
            },
        )
        assert first.status_code == 200
        body = first.json()
        assert body["message_id"] == "msg-001"
        assert body["state"]["criteria"]["zone"] == "Mañongo"

        second = client.post(
            "/ai-test",
            json={
                "sender": "whatsapp:+584120000001",
                "message": "Hasta 200 mil",
                "message_id": "msg-002",
            },
        )
        assert second.status_code == 200
        body = second.json()
        assert body["state"]["criteria"]["zone"] == "Mañongo"
        assert body["state"]["criteria"]["max_budget"] == 200000

        state = client.get(
            "/ai-test/state",
            params={"sender": "whatsapp:+584120000001"},
        )
        assert state.status_code == 200
        assert state.json()["state"]["criteria"]["max_budget"] == 200000

        reset = client.post(
            "/ai-test/reset",
            json={"sender": "whatsapp:+584120000001"},
        )
        assert reset.status_code == 200
        assert reset.json()["cleared"] is True

        missing = client.get(
            "/ai-test/state",
            params={"sender": "whatsapp:+584120000001"},
        )
        assert missing.status_code == 404

    print("\n✅ AI TEST API SMOKE TEST OK")
    print("Health endpoint: OK")
    print("POST /ai-test: OK")
    print("Conversación persistente: OK")
    print("GET /ai-test/state: OK")
    print("POST /ai-test/reset: OK")


if __name__ == "__main__":
    main()
