"""Offline test for the Mettryc inventory adapter.

This test does not call WASI, Google Sheets or OpenRouter. It injects a tiny
fake ``main`` module so we can verify the translation layer safely.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

# Allow ``python scripts/...py`` to work from the repository root without
# requiring PYTHONPATH to be set manually.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_engine.schemas import ConversationState, PropertyCriteria, UserTurnAnalysis
from tools.mettryc_inventory import search_mettryc_properties


async def main() -> None:
    fake_main = types.ModuleType("main")
    fake_main.WASI_TIMEOUT = 40.0
    fake_main.http_client = object()
    fake_main.last_filters = None

    async def actualizar_inventario(force: bool = False) -> bool:
        return True

    def buscar_mejores_propiedades(estado: dict, cantidad: int):
        fake_main.last_filters = estado["filtros"]
        assert cantidad == 5
        return [
            {
                "id": "12345",
                "titulo": "Casa de prueba",
                "precio_venta": 185000,
                "zona": "Mañongo",
                "captador_wasi": "Ana Ejemplo",
                "telefono_captador_wasi": "+58 412 0000000",
            }
        ], "1 coincidencia de prueba"

    def resumen_propiedad_para_ia(propiedad: dict):
        return {
            "id": propiedad["id"],
            "titulo": propiedad["titulo"],
            "precio_venta": propiedad["precio_venta"],
            "zona": propiedad["zona"],
            "captador_wasi": propiedad["captador_wasi"],
        }

    async def sincronizar_google_sheet(force: bool = False) -> bool:
        return True

    def cruzar_captador_con_sheet(nombre: str) -> dict:
        return {
            "nombre": nombre,
            "telefono": "+584120000001",
            "tipo_coincidencia": "exacta",
        }

    fake_main.actualizar_inventario = actualizar_inventario
    fake_main.buscar_mejores_propiedades = buscar_mejores_propiedades
    fake_main.resumen_propiedad_para_ia = resumen_propiedad_para_ia
    fake_main.sincronizar_google_sheet = sincronizar_google_sheet
    fake_main.cruzar_captador_con_sheet = cruzar_captador_con_sheet
    fake_main.extraer_codigo_inmueble = lambda value: "12345"
    fake_main.consultar_detalle_propiedad_wasi = None

    sys.modules["main"] = fake_main

    state = ConversationState(
        role="colleague",
        intent="property_search",
        criteria=PropertyCriteria(
            operation="sale",
            property_type="casa",
            city="Valencia",
            zone="Mañongo",
            max_budget=200000,
            bedrooms=4,
        ),
    )
    analysis = UserTurnAnalysis(role="colleague", intent="property_search", criteria=state.criteria)

    result = await search_mettryc_properties(state, analysis)

    assert result.ok
    assert result.data["count"] == 1
    assert fake_main.last_filters["tipo_operacion"] == "venta"
    assert fake_main.last_filters["tipo_propiedad"] == "casa"
    assert fake_main.last_filters["ciudad"] == "Valencia"
    assert fake_main.last_filters["zona"] == "Mañongo"
    assert fake_main.last_filters["presupuesto_max"] == 200000
    assert fake_main.last_filters["habitaciones_min"] == 4
    assert result.data["properties"][0]["captador"]["telefono"] == "+584120000001"

    print("\n✅ INVENTORY ADAPTER TEST OK")
    print("Filtros traducidos:", fake_main.last_filters)
    print("Propiedades recibidas:", result.data["count"])
    print("Captador:", result.data["properties"][0]["captador"])


if __name__ == "__main__":
    asyncio.run(main())
