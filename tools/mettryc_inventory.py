"""Inventory tools for the new Mettryc AI Engine.

This module is an adapter around the proven inventory/search code in ``main.py``.
It deliberately does not modify the legacy conversation flow.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import httpx

from ai_engine.schemas import ConversationState, ToolResult, UserTurnAnalysis


LEGACY_OPERATION_MAP = {
    "sale": "venta",
    "rent": "alquiler",
    "unknown": None,
}


async def search_mettryc_properties(
    state: ConversationState,
    analysis: UserTurnAnalysis,
) -> ToolResult:
    """Run the existing Mettryc/WASI search with the new engine state."""

    try:
        import main as legacy
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="search_properties",
            message=f"No se pudo cargar la lógica de inventario existente: {exc}",
        )

    if legacy.http_client is None:
        timeout = getattr(legacy, "WASI_TIMEOUT", 40.0)
        legacy.http_client = httpx.AsyncClient(timeout=timeout)

    try:
        await legacy.actualizar_inventario(force=False)

        criteria = state.criteria
        operation = LEGACY_OPERATION_MAP.get(criteria.operation)

        filtros = {
            "tipo_operacion": operation,
            "tipo_propiedad": criteria.property_type,
            "ciudad": criteria.city,
            "zona": criteria.zone,
            "presupuesto_max": criteria.max_budget,
            "habitaciones_min": criteria.bedrooms,
            "banos_min": criteria.bathrooms,
            "garajes_min": criteria.parking,
            "caracteristicas": list(criteria.features),
        }

        legacy_state: dict[str, Any] = {
            "filtros": filtros,
            "propiedades_enviadas": [
                str(item.get("id"))
                for item in state.last_properties
                if isinstance(item, dict) and item.get("id")
            ],
        }

        propiedades, motivo = legacy.buscar_mejores_propiedades(
            legacy_state,
            cantidad=5,
        )

        safe_properties: list[dict[str, Any]] = []
        for property_item in propiedades:
            original = deepcopy(property_item)
            summary = (
                legacy.resumen_propiedad_para_ia(original)
                if hasattr(legacy, "resumen_propiedad_para_ia")
                else original
            )
            summary = deepcopy(summary or {})

            captador_nombre = str(
                original.get("captador_wasi")
                or summary.get("captador_wasi")
                or "Asesor Mettryc"
            )

            try:
                await legacy.sincronizar_google_sheet()
                captador = legacy.cruzar_captador_con_sheet(captador_nombre)
            except Exception:
                captador = {
                    "nombre": captador_nombre,
                    "telefono": original.get("telefono_captador_wasi") or None,
                    "tipo_coincidencia": "wasi_fallback",
                }

            summary["captador"] = {
                "nombre": captador.get("nombre") or captador_nombre,
                "telefono": captador.get("telefono")
                or original.get("telefono_captador_wasi")
                or None,
                "tipo_coincidencia": captador.get("tipo_coincidencia"),
            }
            safe_properties.append(summary)

        return ToolResult(
            ok=True,
            name="search_properties",
            data={
                "properties": safe_properties,
                "count": len(safe_properties),
                "search_reason": motivo,
                "source": "Mettryc/WASI + legacy search",
                "criteria_used": filtros,
            },
            message=(
                f"Se encontraron {len(safe_properties)} propiedades usando la búsqueda real de Mettryc."
                if safe_properties
                else "La búsqueda real no devolvió propiedades con estos criterios."
            ),
        )

    except Exception as exc:
        return ToolResult(
            ok=False,
            name="search_properties",
            data=None,
            message=f"Error al consultar el inventario Mettryc: {type(exc).__name__}: {exc}",
        )


async def get_mettryc_property_detail(
    state: ConversationState,
    analysis: UserTurnAnalysis,
) -> ToolResult:
    """Retrieve a real property detail by code when the engine requests it."""

    try:
        import main as legacy
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="property_detail",
            message=f"No se pudo cargar la lógica existente: {exc}",
        )

    if legacy.http_client is None:
        timeout = getattr(legacy, "WASI_TIMEOUT", 40.0)
        legacy.http_client = httpx.AsyncClient(timeout=timeout)

    code = analysis.requested_property_code
    if not code and analysis.requested_property_reference:
        code = legacy.extraer_codigo_inmueble(analysis.requested_property_reference)

    if not code:
        return ToolResult(
            ok=False,
            name="property_detail",
            message="No se identificó un código de inmueble para consultar.",
        )

    try:
        await legacy.actualizar_inventario(force=False)
        property_item = await legacy.consultar_detalle_propiedad_wasi(code)
        if not property_item:
            return ToolResult(
                ok=False,
                name="property_detail",
                message=f"No encontré el inmueble {code} en el inventario disponible.",
            )

        summary = legacy.resumen_propiedad_para_ia(property_item)
        return ToolResult(
            ok=True,
            name="property_detail",
            data={"property": summary},
            message=f"Detalle del inmueble {code} obtenido del inventario real.",
        )
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="property_detail",
            data=None,
            message=f"Error consultando el inmueble {code}: {type(exc).__name__}: {exc}",
        )
