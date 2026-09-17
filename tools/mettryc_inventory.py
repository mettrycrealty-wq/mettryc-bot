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

LEGACY_PROPERTY_TYPE_MAP = {
    "house": "casa",
    "houses": "casa",
    "casa": "casa",
    "casas": "casa",
    "apartment": "apartamento",
    "apartments": "apartamento",
    "apartamento": "apartamento",
    "apartamentos": "apartamento",
    "apt": "apartamento",
    "townhouse": "townhouse",
    "townhouses": "townhouse",
    "town house": "townhouse",
    "office": "oficina",
    "offices": "oficina",
    "oficina": "oficina",
    "commercial space": "local comercial",
    "commercial premises": "local comercial",
    "local comercial": "local comercial",
    "clinic": "consultorio",
    "medical office": "consultorio",
    "consultorio": "consultorio",
    "warehouse": "galpon",
    "warehouses": "galpon",
    "galpon": "galpon",
    "galpón": "galpon",
    "land": "terreno",
    "lot": "terreno",
    "terrain": "terreno",
    "terreno": "terreno",
}


def normalize_legacy_property_type(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = " ".join(str(value).strip().lower().split())
    return LEGACY_PROPERTY_TYPE_MAP.get(cleaned, cleaned)


def _property_zone_text(property_item: dict[str, Any]) -> str:
    """Build searchable location text from structured and descriptive fields.

    WASI records can leave ``zona`` as ``N/D`` even when the requested
    neighborhood appears in the listing title or description. This helper
    deliberately reads only location-oriented textual fields so the fallback
    does not accidentally match arbitrary metadata such as phone numbers.
    """

    fields = (
        "titulo",
        "title",
        "nombre",
        "descripcion",
        "description",
        "direccion",
        "address",
        "zona",
        "ciudad",
        "urbanizacion",
        "urbanización",
        "sector",
        "municipio",
        "parroquia",
        "residencial",
    )

    values: list[str] = []
    for field in fields:
        value = property_item.get(field)
        if value not in (None, "", "N/D"):
            values.append(str(value))

    return " ".join(values)


def _zone_matches_text(
    legacy: Any,
    property_item: dict[str, Any],
    requested_zone: str | None,
) -> bool:
    """Return True when the requested zone appears in location text.

    Exact phrase matching is preferred. If that fails, all meaningful zone
    tokens must occur in the same searchable text. The legacy normalizer is
    reused so accents/case differences such as ``Mañongo``/``manongo`` do not
    prevent a match.
    """

    if not requested_zone:
        return False

    searched = legacy.normalizar_texto(requested_zone)
    searchable = legacy.normalizar_texto(_property_zone_text(property_item))

    if not searched or not searchable:
        return False

    if searched in searchable:
        return True

    tokens = {
        token
        for token in searched.split()
        if len(token) >= 2
        and token not in {"el", "la", "los", "las", "de", "del", "en", "zona", "sector", "urbanizacion", "ciudad", "venezuela"}
    }

    if not tokens:
        return False

    return tokens.issubset(set(searchable.split()))


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
        legacy_property_type = normalize_legacy_property_type(criteria.property_type)

        filtros = {
            "tipo_operacion": operation,
            "tipo_propiedad": legacy_property_type,
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

        # Fallback geográfico: cuando la búsqueda estructurada devuelve cero
        # resultados y el usuario sí indicó una zona, repetimos la búsqueda
        # sin el filtro estructurado de zona y recuperamos solo propiedades
        # donde esa zona aparece en título/descripción/dirección u otro campo
        # locacional. Esto cubre registros WASI con zona=N/D pero con la zona
        # claramente indicada en el anuncio, como el caso Mañongo.
        if not propiedades and criteria.zone:
            filtros_fallback = dict(filtros)
            filtros_fallback["zona"] = None

            legacy_state_fallback = {
                **legacy_state,
                "filtros": filtros_fallback,
            }

            candidatos, motivo_fallback = legacy.buscar_mejores_propiedades(
                legacy_state_fallback,
                cantidad=1000,
            )

            propiedades_fallback = [
                item
                for item in candidatos
                if isinstance(item, dict)
                and _zone_matches_text(legacy, item, criteria.zone)
            ]

            if propiedades_fallback:
                propiedades = propiedades_fallback[:5]
                motivo = f"{motivo_fallback}|zona_textual_fallback"

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
