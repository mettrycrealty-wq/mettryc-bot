import os
import logging
import asyncio
import httpx
from datetime import datetime, timedelta
from copy import deepcopy
from typing import Any, Dict, List, Optional

# Importamos las herramientas de texto de nuestro utils.py
from utils import normalizar_telefono, normalizar_texto, tokens_nombre

# Configuramos el logger
logger = logging.getLogger("mettryc-chatbot")

# ============================================================
# CONFIGURACIONES Y ESTADO EN MEMORIA
# ============================================================

GOOGLE_SHEET_TURNOS_URL = os.getenv("GOOGLE_SHEET_TURNOS_URL", "")
SHEETS_TIMEOUT = float(os.getenv("SHEETS_TIMEOUT", "20"))
INTERVALO_ACTUALIZACION_SHEETS = timedelta(
    minutes=int(os.getenv("INTERVALO_ACTUALIZACION_SHEETS_MINUTOS", "60"))
)

# Memoria RAM local para la hoja de cálculo
sheets_cache: Dict[str, Any] = {
    "agentes": [],
    "captadores": {},
    "ultima_actualizacion": None,
}

sheets_refresh_lock = asyncio.Lock()
round_robin_lock = asyncio.Lock()
round_robin_index = -1


# ============================================================
# LÓGICA DE GOOGLE SHEETS Y CAPTADORES
# ============================================================

def sheets_necesita_actualizacion() -> bool:
    ultima = sheets_cache.get(
        "ultima_actualizacion"
    )

    if not ultima:
        return True

    return (
        datetime.now() - ultima
        >= INTERVALO_ACTUALIZACION_SHEETS
    )


def agregar_captador_sheet(
    resultado: dict,
    nombre: Any,
    telefono: Any,
) -> None:
    nombre_limpio = str(nombre or "").strip()
    telefono_limpio = normalizar_telefono(telefono)

    if nombre_limpio and telefono_limpio:
        resultado[nombre_limpio] = telefono_limpio


def procesar_captadores_sheet(
    payload: Any,
) -> Dict[str, str]:
    captadores: Dict[str, str] = {}

    if isinstance(payload, dict):
        for nombre, telefono in payload.items():
            if isinstance(telefono, dict):
                agregar_captador_sheet(
                    captadores,
                    telefono.get("nombre") or nombre,
                    telefono.get("telefono")
                    or telefono.get("phone")
                    or telefono.get("whatsapp"),
                )
            else:
                agregar_captador_sheet(
                    captadores,
                    nombre,
                    telefono,
                )

    elif isinstance(payload, list):
        for registro in payload:
            if not isinstance(registro, dict):
                continue

            agregar_captador_sheet(
                captadores,
                registro.get("nombre")
                or registro.get("name")
                or registro.get("asesor")
                or registro.get("captador"),
                registro.get("telefono")
                or registro.get("phone")
                or registro.get("whatsapp"),
            )

    return captadores


async def sincronizar_google_sheet(
    force: bool = False,
    client: Optional[httpx.AsyncClient] = None
) -> bool:
    if not GOOGLE_SHEET_TURNOS_URL:
        logger.warning(
            "GOOGLE_SHEET_TURNOS_URL no configurada."
        )
        return False

    if (
        not force
        and not sheets_necesita_actualizacion()
    ):
        return False

    async with sheets_refresh_lock:
        if (
            not force
            and not sheets_necesita_actualizacion()
        ):
            return False

        # 🚀 FIX: Obtenemos el cliente HTTP para evitar importación circular
        if client is None:
            from main import http_client
            client = http_client

        try:
            respuesta = await client.get(
                GOOGLE_SHEET_TURNOS_URL,
                timeout=SHEETS_TIMEOUT,
                follow_redirects=True,
            )
            respuesta.raise_for_status()
            payload = respuesta.json()

            if not isinstance(payload, dict):
                raise ValueError(
                    "Sheets no devolvió un objeto JSON."
                )

            agentes = payload.get("agentes", [])

            if not isinstance(agentes, list):
                agentes = []

            captadores = procesar_captadores_sheet(
                payload.get("captadores", {})
            )

            # Compatibilidad si el Apps Script devuelve filas.
            if not captadores:
                captadores = procesar_captadores_sheet(
                    payload.get("captadores_data", [])
                    or payload.get("asesores", [])
                )

            sheets_cache["agentes"] = agentes
            sheets_cache["captadores"] = captadores
            sheets_cache["ultima_actualizacion"] = (
                datetime.now()
            )

            logger.info(
                "Sheets sincronizado agentes=%s captadores=%s",
                len(agentes),
                len(captadores),
            )

            return True

        except Exception as exc:
            logger.error(
                "Error Sheets tipo=%s detalle=%s",
                type(exc).__name__,
                str(exc)[:200],
            )
            return False


def cruzar_captador_con_sheet(
    nombre_wasi: str,
) -> dict:
    """
    Cruza el nombre del captador recibido desde Wasi con el
    Google Sheet.

    Primero intenta igualdad exacta normalizada. Si no encuentra,
    calcula coincidencia por tokens del nombre.
    """
    nombre_normalizado = normalizar_texto(
        nombre_wasi
    )

    captadores = sheets_cache.get(
        "captadores",
        {},
    )

    for nombre_sheet, telefono in captadores.items():
        if (
            normalizar_texto(nombre_sheet)
            == nombre_normalizado
        ):
            return {
                "nombre": nombre_sheet,
                "telefono": telefono,
                "tipo_coincidencia": "exacta",
            }

    tokens_wasi = tokens_nombre(nombre_wasi)
    mejor = None
    mejor_score = 0.0

    for nombre_sheet, telefono in captadores.items():
        tokens_sheet = tokens_nombre(nombre_sheet)

        if not tokens_wasi or not tokens_sheet:
            continue

        interseccion = tokens_wasi.intersection(
            tokens_sheet
        )

        union = tokens_wasi.union(tokens_sheet)
        score_jaccard = (
            len(interseccion) / len(union)
            if union
            else 0
        )

        cobertura = (
            len(interseccion) / len(tokens_wasi)
            if tokens_wasi
            else 0
        )

        score = max(
            score_jaccard,
            cobertura,
        )

        if score > mejor_score:
            mejor_score = score
            mejor = {
                "nombre": nombre_sheet,
                "telefono": telefono,
                "tipo_coincidencia": "aproximada",
            }

    if mejor and mejor_score >= 0.65:
        return mejor

    return {
        "nombre": nombre_wasi or "Asesor Mettryc",
        "telefono": None,
        "tipo_coincidencia": "no_encontrada",
    }


async def asignar_agente_round_robin(client: Optional[httpx.AsyncClient] = None) -> Optional[dict]:
    global round_robin_index

    # Le pasamos el cliente HTTP por si necesita actualizar la hoja en ese momento
    await sincronizar_google_sheet(client=client)

    agentes = [
        deepcopy(agente)
        for agente in sheets_cache["agentes"]
        if isinstance(agente, dict)
        and (
            agente.get("nombre")
            or agente.get("name")
        )
    ]

    if not agentes:
        return None

    async with round_robin_lock:
        round_robin_index = (
            round_robin_index + 1
        ) % len(agentes)

        agente = agentes[round_robin_index]

    if not agente.get("nombre"):
        agente["nombre"] = agente.get("name")

    return agente