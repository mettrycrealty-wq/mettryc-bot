"""Diagnostico puntual del fallback textual Mañongo para AI Engine v1."""

from __future__ import annotations

import asyncio

import main
from tools.mettryc_inventory import _property_zone_text, _zone_matches_text


TARGET_ID = "10294059"


async def main_async() -> None:
    print("=== DIAGNOSTICO FALLBACK TEXTUAL: MANONGO ===")

    if main.http_client is None:
        import httpx
        main.http_client = httpx.AsyncClient(timeout=getattr(main, "WASI_TIMEOUT", 40.0))

    await main.actualizar_inventario(force=True)
    inventario = main.inventory_cache.get("inventario", [])
    print(f"Inventario cargado: {len(inventario)}")

    propiedad = next(
        (p for p in inventario if str(p.get("id")) == TARGET_ID),
        None,
    )

    if not propiedad:
        print(f"NO ENCONTRADO: id={TARGET_ID}")
        return

    filtros = {
        "tipo_operacion": "venta",
        "tipo_propiedad": "casa",
        "ciudad": None,
        "zona": "Mañongo",
        "presupuesto_max": None,
        "habitaciones_min": 0,
        "banos_min": 0,
        "garajes_min": 0,
        "caracteristicas": [],
    }
    filtros_sin_zona = dict(filtros)
    filtros_sin_zona["zona"] = None

    print("ID:", propiedad.get("id"))
    print("ACTIVA:", propiedad.get("activa"))
    print("TIPO WASI:", propiedad.get("tipo_propiedad_wasi"))
    print("TITULO:", propiedad.get("titulo"))
    print("CIUDAD:", propiedad.get("ciudad"))
    print("ZONA:", propiedad.get("zona"))
    print("PRECIO VENTA:", propiedad.get("precio_venta"))

    print("\n--- PRUEBAS ---")
    print("coincide_tipo(casa):", main.coincide_tipo(propiedad, "casa"))
    print("ciudad_coincide(None):", main.ciudad_coincide(propiedad, None))
    print("obtener_precio(venta):", main.obtener_precio(propiedad, "venta"))
    print("_zone_matches_text(Mañongo):", _zone_matches_text(main, propiedad, "Mañongo"))
    print("ZONA TEXTUAL:", _property_zone_text(propiedad))

    evaluada = main.evaluar_propiedad(propiedad, filtros_sin_zona)
    print("evaluar_propiedad(sin_zona):", bool(evaluada))
    if evaluada:
        print("  score:", evaluada.get("_score"))
        print("  coincidencia:", evaluada.get("_coincidencia"))

    print("\n--- BUSQUEDA GLOBAL SIN ZONA ---")
    legacy_state = {
        "filtros": filtros_sin_zona,
        "propiedades_enviadas": [],
    }
    candidatos, motivo = main.buscar_mejores_propiedades(legacy_state, cantidad=1000)
    print("candidatos legacy sin zona:", len(candidatos), "motivo:", motivo)
    target = next((p for p in candidatos if str(p.get("id")) == TARGET_ID), None)
    print("target dentro de candidatos:", bool(target))


if __name__ == "__main__":
    asyncio.run(main_async())
