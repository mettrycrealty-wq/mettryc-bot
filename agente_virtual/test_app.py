from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .service import AgenteVirtualService


class MessageRequest(BaseModel):
    sender: str = Field(min_length=1)
    message: str = Field(min_length=1)


def create_test_app(
    service: AgenteVirtualService | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Mettryc Realty - Agente Virtual (Prueba)",
        version="1.0.0",
    )
    agent_service = service or AgenteVirtualService()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "mettryc-agente-virtual",
            "production": False,
        }

    @app.post("/message")
    async def message(payload: MessageRequest) -> dict[str, Any]:
        try:
            reply = await agent_service.process(
                payload.sender,
                payload.message,
            )
            return {
                "ok": True,
                "sender": payload.sender,
                "reply": reply,
            }
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Error procesando el mensaje: "
                    + type(exc).__name__
                    + ": "
                    + str(exc)
                ),
            ) from exc

    return app


app = create_test_app()
