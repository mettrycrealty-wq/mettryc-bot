"""Deterministic conversational tools built on the new AI Engine state.

These tools operate on already retrieved Mettryc inventory and do not replace the
legacy business logic. They make common follow-up messages actionable without
forcing the user through a menu.
"""

from __future__ import annotations

import re
from typing import Any

from ai_engine.schemas import ConversationState, ToolResult, UserTurnAnalysis
from tools.mettryc_inventory import search_mettryc_properties


POSITION_MAP = {
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "primera": 1,
    "segundo": 2,
    "segunda": 2,
    "tercero": 3,
    "tercera": 3,
    "cuarto": 4,
    "cuarta": 4,
    "quinto": 5,
    "quinta": 5,
}


def _position_from_reference(reference: str | None) -> int | None:
    if not reference:
        return None
    text = reference.strip().lower()
    if text in POSITION_MAP:
        return POSITION_MAP[text]
    match = re.search(r"\b([1-5])\b", text)
    return int(match.group(1)) if match else None


def _property_code(item: dict[str, Any]) -> str | None:
    for key in ("code", "codigo", "id", "property_code"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return None


async def select_mettryc_property(
    state: ConversationState,
    analysis: UserTurnAnalysis,
) -> ToolResult:
    """Select one of the most recently shown properties by code or position."""

    properties = state.last_properties
    if not properties:
        return ToolResult(
            ok=False,
            name="select_property",
            message="No hay propiedades recientes para seleccionar.",
        )

    requested_code = (analysis.requested_property_code or "").strip()
    if requested_code:
        for item in properties:
            if requested_code == _property_code(item):
                return ToolResult(
                    ok=True,
                    name="select_property",
                    data={"property": item},
                    message=f"Inmueble {requested_code} seleccionado.",
                )

    position = _position_from_reference(analysis.requested_property_reference)
    if position and position <= len(properties):
        item = properties[position - 1]
        return ToolResult(
            ok=True,
            name="select_property",
            data={"property": item, "position": position},
            message=f"Se seleccionó la opción {position}.",
        )

    return ToolResult(
        ok=False,
        name="select_property",
        message="No pude identificar una propiedad de las mostradas anteriormente.",
    )


async def request_mettryc_captador(
    state: ConversationState,
    analysis: UserTurnAnalysis,
) -> ToolResult:
    """Return the captador already stored with the selected/recent property."""

    property_item = state.selected_property
    if not property_item and state.last_properties:
        position = _position_from_reference(analysis.requested_property_reference)
        if position and position <= len(state.last_properties):
            property_item = state.last_properties[position - 1]
        else:
            property_item = state.last_properties[0]

    if not property_item:
        return ToolResult(
            ok=False,
            name="request_captador",
            message="Necesito una propiedad seleccionada para consultar su captador.",
        )

    captador = property_item.get("captador")
    if not isinstance(captador, dict):
        return ToolResult(
            ok=False,
            name="request_captador",
            data={"property": property_item},
            message="La propiedad no trae datos de captación disponibles.",
        )

    return ToolResult(
        ok=True,
        name="request_captador",
        data={
            "property_code": _property_code(property_item),
            "captador": captador,
        },
        message="Datos de captación obtenidos del inventario.",
    )


async def more_mettryc_properties(
    state: ConversationState,
    analysis: UserTurnAnalysis,
) -> ToolResult:
    """Fetch another batch using the existing search adapter as the exclusion list."""

    return await search_mettryc_properties(state, analysis)
