import os
import logging
import asyncio
import httpx
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

# Configuramos el logger
logger = logging.getLogger("mettryc-chatbot")

# ============================================================
# CONFIGURACIONES DE WASI
# ============================================================

WASI_TOKEN = os.getenv("WASI_TOKEN", "")
# 🚀 FIX: Wasi API exige el ID_COMPANY para devolver resultados reales
ID_COMPANY = os.getenv("ID_COMPANY", "") 

WASI_API_URL = "https://api.wasi.co/v1/property/search"
WASI_TIMEOUT = float(os.getenv("WASI_TIMEOUT", "45"))
INTERVALO_ACTUALIZACION_WASI = timedelta(
    minutes=int(os.getenv("INTERVALO_ACTUALIZACION_WASI_MINUTOS", "120"))
)

inventory_cache: Dict[str, Any] = {
    "inventario": [],
    "ultima_actualizacion": None,
}

wasi_refresh_lock = asyncio.Lock()

def wasi_necesita_actualizacion() -> bool:
    ultima = inventory_cache.get("ultima_actualizacion")
    if not ultima:
        return True
    return datetime.now() - ultima >= INTERVALO_ACTUALIZACION_WASI

async def sincronizar_inventario_wasi(
    force: bool = False,
    client: Optional[httpx.AsyncClient] = None
) -> bool:
    if not WASI_TOKEN or not ID_COMPANY:
        logger.error("❌ [WASI] Faltan credenciales: WASI_TOKEN o ID_COMPANY no están configurados.")
        return False

    if not force and not wasi_necesita_actualizacion():
        return False

    async with wasi_refresh_lock:
        if not force and not wasi_necesita_actualizacion():
            return False

        if client is None:
            from main import http_client
            client = http_client

        try:
            logger.info("📡 [WASI] Iniciando sincronización del inventario...")
            
            respuesta = await client.get(
                WASI_API_URL,
                params={"id_company": ID_COMPANY, "wasi_token": WASI_TOKEN, "limit": 1000},
                timeout=WASI_TIMEOUT,
            )
            respuesta.raise_for_status()
            
            datos = respuesta.json()
            
            if datos.get("status") == "error":
                logger.error(f"❌ [WASI] Error de API: {datos.get('message')}")
                return False

            # 🚀 FIX: Extraemos el diccionario anidado correctamente
            items_crudos = datos.get("result", {}).get("items", {})
            
            # Wasi devuelve un Diccionario de Diccionarios (ej: {"0": {...}, "1": {...}}), NO una lista
            propiedades = []
            if isinstance(items_crudos, dict):
                propiedades = [v for k, v in items_crudos.items() if isinstance(v, dict)]
            elif isinstance(items_crudos, list):
                propiedades = [v for v in items_crudos if isinstance(v, dict)]

            inventory_cache["inventario"] = propiedades
            inventory_cache["ultima_actualizacion"] = datetime.now()

            logger.info(f"✅ [WASI] Inventario Wasi cargado propiedades={len(propiedades)}")
            return True

        except Exception as exc:
            logger.error(f"❌ [WASI] Error de sincronización: {type(exc).__name__} - {str(exc)[:100]}")
            return False
