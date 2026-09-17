from __future__ import annotations

import os

from .engine import MettrycAIEngine
from .router import OpenRouterClient
from .service import ConversationService
from tools.mettryc_inventory import (
    get_mettryc_property_detail,
    search_mettryc_properties,
)


def create_mettryc_ai_engine() -> MettrycAIEngine:
    """Create the new AI engine with the real Mettryc inventory tools.

    This factory is intentionally separate from main.py so the legacy bot can keep
    running unchanged while the new engine is tested in isolation.
    """

    llm = OpenRouterClient(
        api_key=os.getenv("OPENROUTER_API_KEY"),
        model=os.getenv("OPENROUTER_MAIN_MODEL") or os.getenv("OPENROUTER_MODEL"),
        fallback_model=os.getenv("OPENROUTER_FALLBACK_MODEL"),
    )

    return MettrycAIEngine(
        llm,
        tools={
            "search_properties": search_mettryc_properties,
            "property_detail": get_mettryc_property_detail,
        },
        knowledge_context="Mettryc Realty: asistente virtual inmobiliario. Usa únicamente los datos devueltos por las herramientas para propiedades, precios, disponibilidad y captadores.",
    )


def create_mettryc_conversation_service(*, max_sessions: int = 5000) -> ConversationService:
    """Create a channel-independent conversation service backed by the AI engine."""

    return ConversationService(
        create_mettryc_ai_engine(),
        max_sessions=max_sessions,
    )
