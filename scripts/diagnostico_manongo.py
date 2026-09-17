from __future__ import annotations

import asyncio
from pathlib import Path
import sys

# Permite ejecutar desde la raiz del repositorio.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main() -> None:
    import httpx
    import main as legacy

    if legacy.http_client is None:
        timeout = getattr(legacy, "WASI_TIMEOUT", 40.0)
        legacy.http_client = httpx.AsyncClient(timeout=timeout)

    print("\n=== DIAGNOSTICO REAL: MANONGO ===")
    print("Actualizando inventario WASI...\n")

    try:
        await legacy.actualizar_inventario(force=True)
    except Exception as exc:
        print(f"ERROR actualizando WASI: {type(exc).__name__}: {exc}")
        return

    inventario = legacy.inventory_cache.get("inventario", [])
    print(f"Inventario total cargado: {len(inventario)}")

    activas = [p for p in inventario if p.get("activa")]
    print(f"Propiedades activas: {len(activas)}")

    zona_buscada = "Mañongo"
    tipo_buscado = "casa"
    operacion = "venta"

    print(f"\nBuscando diagnostico para: {tipo_buscado} en {zona_buscada} ({operacion})")

    encontradas_zona = []
    for p in inventario:
        texto = " | ".join(
            str(p.get(campo) or "")
            for campo in ("zona", "ciudad", "titulo", "direccion", "descripcion")
        )
        if legacy.normalizar_texto(zona_buscada) in legacy.normalizar_texto(texto):
            encontradas_zona.append(p)

    print(f"\nPropiedades cuyo texto contiene 'Mañongo': {len(encontradas_zona)}")

    for i, p in enumerate(encontradas_zona[:30], 1):
        tipo = p.get("tipo_propiedad_wasi")
        ciudad = p.get("ciudad")
        zona = p.get("zona")
        activa = p.get("activa")
        precio_venta = legacy.obtener_precio(p, "venta")
        tipo_ok = legacy.coincide_tipo(p, tipo_buscado)
        zona_ok = legacy.zona_coincide(zona_buscada, zona, ciudad)
        activa_ok = bool(activa)
        precio_ok = precio_venta > 0

        print(
            f"\n[{i}] id={p.get('id')} | activa={activa_ok} | "
            f"tipo={tipo!r} | ciudad={ciudad!r} | zona={zona!r} | "
            f"venta={precio_venta}"
        )
        print(
            f"    coincide_tipo(casa)={tipo_ok} | "
            f"zona_coincide(Mañongo)={zona_ok} | "
            f"precio_venta>0={precio_ok}"
        )
        print(f"    titulo={p.get('titulo')!r}")

    filtros = {
        "tipo_operacion": operacion,
        "tipo_propiedad": tipo_buscado,
        "ciudad": None,
        "zona": zona_buscada,
        "presupuesto_max": None,
        "habitaciones_min": None,
        "banos_min": None,
        "garajes_min": None,
        "caracteristicas": [],
    }

    estado = {
        "filtros": filtros,
        "propiedades_enviadas": [],
    }

    try:
        resultado, motivo = legacy.buscar_mejores_propiedades(estado, cantidad=10)
        print(f"\nResultado final del buscador legacy: {len(resultado)}")
        print(f"Motivo: {motivo}")
        for i, p in enumerate(resultado, 1):
            print(
                f"  {i}. id={p.get('id')} | titulo={p.get('titulo')!r} | "
                f"ciudad={p.get('ciudad')!r} | zona={p.get('zona')!r} | "
                f"tipo={p.get('tipo_propiedad_wasi')!r} | "
                f"venta={p.get('precio_venta')}"
            )
    except Exception as exc:
        print(f"\nERROR ejecutando buscar_mejores_propiedades: {type(exc).__name__}: {exc}")

    print("\n=== FIN DEL DIAGNOSTICO ===")


if __name__ == "__main__":
    asyncio.run(main())
