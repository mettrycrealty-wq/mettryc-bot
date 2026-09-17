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
    """Build searchable location text from structured and descriptive fields."""

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
    """Return True when the requested zone appears in location-oriented text."""

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
        and token not in {
            "el", "la", "los", "las", "de", "del", "en", "zona",
            "sector", "urbanizacion", "ciudad", "venezuela",
        }
    }

    return bool(tokens) and tokens.issubset(set(searchable.split()))


def _textual_zone_fallback(
    legacy: Any,
    state: ConversationState,
    filtros: dict[str, Any],
    requested_zone: str,
) -> list[dict[str, Any]]:
    """Search the already loaded WASI inventory, bypassing only the broken structured zone gate."""

    candidatos: list[dict[str, Any]] = []
    enviados = {
        str(item.get("id"))
        for item in state.last_properties
        if isinstance(item, dict) and item.get("id")
    }

    filtros_sin_zona = dict(filtros)
    filtros_sin_zona["zona"] = None

    for original in legacy.inventory_cache.get("inventario", []):
        if not isinstance(original, dict):
            continue

        property_id = str(original.get("id") or "")
        if not property_id or property_id in enviados:
            continue

        if not original.get("activa", True):
            continue

        if not legacy.coincide_tipo(
            original,
            filtros_sin_zona.get("tipo_propiedad"),
        ):
            continue

        if not legacy.ciudad_coincide(
            original,
            filtros_sin_zona.get("ciudad"),
        ):
            continue

        propiedad = legacy.evaluar_propiedad(original, filtros_sin_zona)
        if not propiedad:
            continue

        if not _zone_matches_text(legacy, propiedad, requested_zone):
            continue

        candidatos.append(propiedad)

    candidatos.sort(
        key=lambda item: (
            item.get("_coincidencia") == "exacta",
            item.get("_score", 0),
        ),
        reverse=True,
    )

    return candidatos


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
        # resultados y el usuario indicó una zona, buscamos directamente en
        # el inventario WASI ya cargado. Se conservan operación, tipo, ciudad,
        # presupuesto, habitaciones, baños, puestos y características; solo
        # se reemplaza temporalmente el filtro estructurado de zona por una
        # coincidencia textual en campos locacionales.
        if not propiedades and criteria.zone:
            propiedades_fallback = _textual_zone_fallback(
                legacy,
                state,
                filtros,
                criteria.zone,
            )

            if propiedades_fallback:
                propiedades = propiedades_fallback[:5]
                motivo = f"{motivo}|zona_textual_fallback"

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


def _infer_property_type(legacy: Any, property_item: dict[str, Any]) -> str:
    """Infer a human-readable property type when WASI's type label is empty."""

    raw_type = str(property_item.get("tipo_propiedad_wasi") or "").strip()
    normalized_type = legacy.normalizar_tipo_propiedad(raw_type)

    if normalized_type and normalized_type not in {"n/d", "nd", "n d"}:
        return normalized_type

    checks = (
        ("casa", "Casa"),
        ("apartamento", "Apartamento"),
        ("townhouse", "Townhouse"),
        ("oficina", "Oficina"),
        ("local", "Local comercial"),
        ("galpon", "Galpón"),
        ("terreno", "Terreno"),
        ("penthouse", "Penthouse"),
    )

    for candidate, label in checks:
        try:
            if legacy.coincide_tipo(property_item, candidate):
                return label
        except Exception:
            continue

    return raw_type if raw_type else "No disponible"


def _rich_property_summary(legacy: Any, property_item: dict[str, Any]) -> dict[str, Any]:
    """Build a detailed, AI-safe view from the normalized WASI property."""

    precio_venta = property_item.get("precio_venta")
    precio_alquiler = property_item.get("precio_alquiler")
    caracteristicas = []

    for field in (
        "caracteristicas_generales",
        "caracteristicas_internas",
        "caracteristicas_externas",
    ):
        value = property_item.get(field) or []
        if isinstance(value, list):
            caracteristicas.extend(str(item) for item in value if item)

    seen = set()
    caracteristicas_limpias = []
    for item in caracteristicas:
        key = legacy.normalizar_texto(item)
        if key and key not in seen:
            seen.add(key)
            caracteristicas_limpias.append(item)

    return {
        "id": str(property_item.get("id") or ""),
        "titulo": property_item.get("titulo") or "Propiedad Mettryc",
        "tipo": _infer_property_type(legacy, property_item),
        "tipo_wasi": property_item.get("tipo_propiedad_wasi") or "N/D",
        "operacion": (
            "venta" if legacy.convertir_float(precio_venta) > 0
            else "alquiler" if legacy.convertir_float(precio_alquiler) > 0
            else "desconocida"
        ),
        "ciudad": property_item.get("ciudad") or "N/D",
        "zona": property_item.get("zona") or "N/D",
        "direccion": property_item.get("direccion_publica") or "N/D",
        "precio_venta": legacy.convertir_float(precio_venta) or None,
        "precio_venta_label": property_item.get("precio_venta_label") or "N/D",
        "precio_alquiler": legacy.convertir_float(precio_alquiler) or None,
        "precio_alquiler_label": property_item.get("precio_alquiler_label") or "N/D",
        "area": property_item.get("area") or "N/D",
        "area_construida": property_item.get("area_construida"),
        "area_terreno": property_item.get("area_terreno"),
        "habitaciones": property_item.get("habitaciones") or "N/D",
        "banos": property_item.get("banos") or "N/D",
        "garajes": property_item.get("garajes") or "N/D",
        "caracteristicas": caracteristicas_limpias,
        "descripcion": property_item.get("descripcion") or "",
        "observaciones": property_item.get("observaciones") or "",
        "captador": {
            "nombre": property_item.get("captador_wasi") or "Asesor Mettryc",
            "telefono": property_item.get("telefono_captador_wasi") or None,
        },
        "imagenes": property_item.get("imagenes") or [],
        "video": property_item.get("video"),
        "enlace": property_item.get("enlace"),
    }


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

        summary = _rich_property_summary(legacy, property_item)
        return ToolResult(
            ok=True,
            name="property_detail",
            data={"property": summary},
            message=f"Detalle completo del inmueble {code} obtenido del inventario real.",
        )
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="property_detail",
            data=None,
            message=f"Error consultando el inmueble {code}: {type(exc).__name__}: {exc}",
        )
