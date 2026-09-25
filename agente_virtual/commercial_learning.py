from __future__ import annotations

from typing import Any


COMMERCIAL_SIGNALS = {
    "interesado",
    "alta_intencion",
    "visita",
    "precio",
    "captura_datos",
    "asesor",
    "objecion",
}

NEXT_STEPS = {
    "seguir_explorando",
    "profundizar",
    "mostrar_alternativas",
    "visita",
    "asesor",
    "captura_lead",
}


def normalize_signal(value: Any) -> str | None:
    """Normaliza señales comerciales generadas por Paty."""
    if not value:
        return None

    normalized = str(value).strip().lower()
    return normalized if normalized in COMMERCIAL_SIGNALS else normalized


def normalize_next_step(value: Any) -> str | None:
    """Normaliza acciones siguientes detectadas en conversación."""
    if not value:
        return None

    return str(value).strip().lower()


def apply_commercial_analysis(
    state: dict,
    analysis: Any,
) -> dict:
    """
    Capa de inteligencia comercial de Paty.

    Esta función no responde usuarios ni modifica decisiones comerciales.
    Solo transforma el análisis del turno en memoria estructurada para
    aprendizaje posterior.
    """

    signal = normalize_signal(
        getattr(analysis, "sales_signal", None)
    )
    next_step = normalize_next_step(
        getattr(analysis, "sales_next_step", None)
    )
    objection = getattr(analysis, "objection_type", None)

    state["ultima_senal_comercial"] = signal
    state["siguiente_paso_comercial"] = next_step

    if objection:
        state["ultima_objecion_comercial"] = str(objection)

    state["paty_commercial_learning"] = {
        "signal": signal,
        "next_step": next_step,
        "objection": objection,
    }

    return state
