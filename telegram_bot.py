import os
import logging
import httpx
from typing import Optional

# Importamos las herramientas necesarias que ya movimos a utils.py
from utils import normalizar_telefono, formato_moneda

# Configuramos el logger para este archivo
logger = logging.getLogger("mettryc-chatbot")

# ============================================================
# CONFIGURACIONES DE TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_TIMEOUT = float(os.getenv("TELEGRAM_TIMEOUT", "15"))

TELEGRAM_ADMIN_IDS = [
    valor.strip()
    for valor in os.getenv(
        "TELEGRAM_ADMIN_IDS",
        os.getenv("TELEGRAM_ADMIN_ID", ""),
    ).split(",")
    if valor.strip()
]

# ============================================================
# LÓGICA DE TELEGRAM
# ============================================================

async def enviar_telegram(
    chat_id: str,
    mensaje: str,
    client: Optional[httpx.AsyncClient] = None
) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False

    # 🚀 FIX: Si no recibe el cliente HTTP, lo toma de main para evitar errores de importación
    if client is None:
        from main import http_client
        client = http_client

    try:
        respuesta = await client.post(
            (
                "https://api.telegram.org/bot"
                f"{TELEGRAM_BOT_TOKEN}/sendMessage"
            ),
            json={
                "chat_id": chat_id,
                "text": mensaje,
                "disable_web_page_preview": True,
            },
            timeout=TELEGRAM_TIMEOUT,
        )

        respuesta.raise_for_status()
        payload = respuesta.json()

        return bool(payload.get("ok"))

    except Exception as exc:
        logger.error(
            "Error Telegram chat=%s tipo=%s",
            str(chat_id)[-4:],
            type(exc).__name__,
        )
        return False


def resumen_filtros(estado: dict) -> str:
    filtros = estado.get("filtros", {})
    lineas = []

    etiquetas = {
        "tipo_operacion": "Operación",
        "tipo_propiedad": "Tipo",
        "zona": "Zona",
        "habitaciones_min": "Habitaciones mínimas",
        "banos_min": "Baños mínimos",
        "garajes_min": "Puestos de estacionamiento"
    }

    for campo, etiqueta in etiquetas.items():
        valor = filtros.get(campo)

        if valor not in [None, "", []]:
            lineas.append(
                f"- {etiqueta}: {valor}"
            )

    if filtros.get("presupuesto_max"):
        lineas.append(
            "- Presupuesto: "
            + formato_moneda(
                filtros["presupuesto_max"]
            )
        )

    if filtros.get("caracteristicas"):
        lineas.append(
            "- Características: "
            + ", ".join(
                filtros["caracteristicas"]
            )
        )

    return "\n".join(lineas) or "- Sin filtros específicos"


async def notificar_lead(
    estado: dict,
    client: Optional[httpx.AsyncClient] = None
) -> bool:
    lead = estado.get("lead", {})
    propiedad = estado.get("propiedad_interes")
    agente = estado.get("agente_asignado")

    propiedad_texto = "No especificada"

    if propiedad:
        propiedad_texto = (
            f"{propiedad.get('titulo')}\n"
            f"ID: {propiedad.get('id')}\n"
            f"{propiedad.get('enlace')}"
        )

    whatsapp = normalizar_telefono(
        lead.get("whatsapp")
    )

    # 🚀 Enlace Limpio
    mensaje = (
        "🏠 NUEVO LEAD METTRYC\n\n"
        f"ID: {estado.get('lead_id')}\n"
        f"Nombre: {lead.get('nombre')}\n"
        f"Correo: {lead.get('correo')}\n"
        f"WhatsApp: {whatsapp}\n"
        f"Contacto: https://wa.me/{whatsapp}\n\n"
        "📋 BÚSQUEDA\n"
        f"{resumen_filtros(estado)}\n\n"
        "⭐ PROPIEDAD DE INTERÉS\n"
        f"{propiedad_texto}\n\n"
        "👤 AGENTE ASIGNADO\n"
        f"{agente.get('nombre') if agente else 'Sin asignar'}"
    )

    destinos = set(TELEGRAM_ADMIN_IDS)

    if agente:
        telegram_id = (
            agente.get("telegram_id")
            or agente.get("telegram")
            or agente.get("chat_id")
        )

        if telegram_id:
            destinos.add(
                str(telegram_id).strip()
            )

    resultados = []

    for destino in destinos:
        resultados.append(
            await enviar_telegram(
                destino,
                mensaje,
                client
            )
        )

    return any(resultados)