from __future__ import annotations

from typing import Any

from .commercial_learning import apply_commercial_analysis


def enrich_analysis(state: dict[str, Any], analysis: Any) -> None:
    """
    Adapter aislado para conectar el análisis comercial de Paty.

    Este módulo no decide comportamiento conversacional. Solo enruta
    señales comerciales detectadas por el análisis hacia la capa de
    aprendizaje.
    """
    apply_commercial_analysis(
        state,
        analysis,
    )
