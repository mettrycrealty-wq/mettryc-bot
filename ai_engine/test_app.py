from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .app import create_mettryc_conversation_service
from .channel_adapter import ChannelAdapter
from .service import ConversationService


class AITestRequest(BaseModel):
    sender: str = Field(min_length=1)
    message: str = Field(min_length=1)
    message_id: str | None = None


class AIResetRequest(BaseModel):
    sender: str = Field(min_length=1)


def create_ai_test_app(
    conversation_service: ConversationService | None = None,
) -> FastAPI:
    """Create a localhost-only test API for the new conversational engine.

    This module is intentionally separate from ``main.py``. It is not imported by
    the production webhook, so starting the legacy application does not expose
    these test endpoints.
    """

    app = FastAPI(title="Mettryc AI Engine Test API", version="1.0")
    service = conversation_service or create_mettryc_conversation_service()
    adapter = ChannelAdapter(service)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "mettryc-ai-engine",
            "sessions": service.session_count(),
        }

    @app.post("/ai-test")
    async def ai_test(payload: AITestRequest) -> dict[str, Any]:
        try:
            return await adapter.handle_payload(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Error procesando la conversación: {type(exc).__name__}: {exc}",
            ) from exc

    @app.get("/ai-test/state")
    async def ai_test_state(sender: str) -> dict[str, Any]:
        try:
            state = await service.get_state(sender)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if state is None:
            raise HTTPException(status_code=404, detail="No existe una sesión para ese sender.")
        return {
            "ok": True,
            "sender": sender,
            "state": state.model_dump(mode="json"),
        }

    @app.post("/ai-test/reset")
    async def ai_test_reset(payload: AIResetRequest) -> dict[str, Any]:
        try:
            cleared = await service.clear_session(payload.sender)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "sender": payload.sender,
            "cleared": cleared,
        }

    return app


app = create_ai_test_app()
