"""
Capa inicial de memoria conversacional de Paty.

Objetivo:
- mantener contexto de propiedad activa
- distinguir agradecimientos/respuestas sociales
- evitar lanzar nuevas búsquedas cuando existe una conversación activa
"""

from datetime import datetime, timezone

SOCIAL_MESSAGES = {
    "gracias",
    "muchas gracias",
    "ok",
    "perfecto",
    "excelente",
    "listo",
    "muy amable",
}


def normalize_message(text: str) -> str:
    return " ".join((text or "").lower().strip().split())


def is_social_message(text: str) -> bool:
    return normalize_message(text).rstrip(".!?") in SOCIAL_MESSAGES


def create_context() -> dict:
    return {
        "active_property": None,
        "last_search": None,
        "last_intent": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def set_active_property(context: dict, property_code: str):
    context["active_property"] = property_code
    context["updated_at"] = datetime.now(timezone.utc).isoformat()


def has_active_property(context: dict) -> bool:
    return bool(context.get("active_property"))


def should_continue_property_flow(context: dict, message: str) -> bool:
    return has_active_property(context) and not is_social_message(message)
