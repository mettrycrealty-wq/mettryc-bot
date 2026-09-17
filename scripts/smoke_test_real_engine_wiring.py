from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

# Allow running this file directly from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_engine.app import create_mettryc_ai_engine  # noqa: E402


class FakeLLM:
    """Offline model stub: analysis first, natural reply second."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_fallback(self, messages, **kwargs):
        self.calls += 1
        if kwargs.get("force_json"):
            return """
            {
              "role": "colleague",
              "intent": "property_search",
              "criteria": {
                "operation": "sale",
                "property_type": "casa",
                "country": "Venezuela",
                "state": "Carabobo",
                "city": "Valencia",
                "zone": "Mañongo",
                "min_budget": null,
                "max_budget": 200000,
                "bedrooms": 4,
                "bathrooms": null,
                "parking": null,
                "min_m2": null,
                "max_m2": null,
                "features": []
              },
              "requested_property_code": null,
              "requested_property_reference": null,
              "needs_human": false,
              "reasoning_summary": "Corredor busca una casa para un cliente en Mañongo hasta 200000."
            }
            """
        return "Encontré una opción en Mañongo dentro del presupuesto indicado."


async def main() -> None:
    # Reemplazamos temporalmente el módulo legacy main por un stub controlado.
    legacy = types.ModuleType("main")
    legacy.WASI_TIMEOUT = 40.0
    legacy.http_client = None
    legacy.actualizar_inventario = _actualizar_inventario
    legacy.buscar_mejores_propiedades = _buscar_mejores_propiedades
    legacy.resumen_propiedad_para_ia = _resumen_propiedad_para_ia
    legacy.sincronizar_google_sheet = _sincronizar_google_sheet
    legacy.cruzar_captador_con_sheet = _cruzar_captador_con_sheet
    legacy.consultar_detalle_propiedad_wasi = _consultar_detalle
    sys.modules["main"] = legacy

    engine = create_mettryc_ai_engine()
    engine.llm = FakeLLM()

    result = await engine.process(
        "Hola, soy corredor. Busco una casa en Mañongo para un cliente, hasta 200 mil, mínimo 4 habitaciones."
    )

    assert result.state.role == "colleague"
    assert result.state.criteria.zone == "Mañongo"
    assert result.state.criteria.max_budget == 200000
    assert result.state.criteria.bedrooms == 4
    assert result.tool_results and result.tool_results[0].name == "search_properties"
    assert result.tool_results[0].ok is True
    assert result.tool_results[0].data["properties"]
    assert result.tool_results[0].data["properties"][0]["captador"]["nombre"] == "Ana Ejemplo"
    assert result.reply

    print("\n✅ REAL ENGINE WIRING TEST OK")
    print(f"Rol: {result.state.role}")
    print(f"Intención: {result.state.intent}")
    print(f"Zona: {result.state.criteria.zone}")
    print(f"Presupuesto máximo: {result.state.criteria.max_budget}")
    print(f"Habitaciones mínimas: {result.state.criteria.bedrooms}")
    print(f"Propiedades recibidas: {len(result.state.last_properties)}")
    print(f"Respuesta: {result.reply}")


async def _actualizar_inventario(*, force=False):
    return True


async def _buscar_mejores_propiedades(state, cantidad=5):
    return [
        {
            "id": "A1",
            "titulo": "Casa en Mañongo",
            "precio_venta": 190000,
            "captador_wasi": "Ana Ejemplo",
        }
    ][:cantidad]


def _resumen_propiedad_para_ia(propiedad):
    return {
        "id": propiedad["id"],
        "titulo": propiedad["titulo"],
        "precio_venta": propiedad["precio_venta"],
        "captador_wasi": propiedad.get("captador_wasi"),
    }


async def _sincronizar_google_sheet(*, force=False):
    return True


def _cruzar_captador_con_sheet(nombre_wasi):
    return {
        "nombre": nombre_wasi,
        "telefono": "+584120000001",
        "tipo_coincidencia": "exacta",
    }


async def _consultar_detalle(codigo):
    return {"id": codigo}


if __name__ == "__main__":
    asyncio.run(main())
