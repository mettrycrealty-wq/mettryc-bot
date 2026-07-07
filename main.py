import os
import requests
import logging
import time
import re
from datetime import datetime, timedelta
from fastapi import FastAPI, Request

# Configuración de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Caché Global
sheets_cache = {"agentes": [], "captadores": {}, "ultimo_indice": -1, "ultima_actualizacion": datetime.min}
inventario_cache = {"texto": "", "ultima_actualizacion": datetime.min}
memoria_conversaciones = {}
clientes_procesados = set()

# --- 1. DEFINICIÓN DE FUNCIONES (DEBEN IR ARRIBA) ---

def sincronizar_google_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL")
    if not script_url: return
    if datetime.now() - sheets_cache["ultima_actualizacion"] > timedelta(minutes=30) or not sheets_cache["agentes"]:
        try:
            response = requests.get(script_url, timeout=15)
            data = response.json()
            sheets_cache["agentes"] = data.get("agentes", [])
            sheets_cache["captadores"] = data.get("captadores", {})
            sheets_cache["ultima_actualizacion"] = datetime.now()
            logger.info(f"✅ Sincronizados {len(sheets_cache['agentes'])} agentes.")
        except Exception as e: logger.error(f"🔴 Error Sheets: {e}")

def obtener_inventario():
    if datetime.now() - inventario_cache["ultima_actualizacion"] > timedelta(hours=12) or not inventario_cache["texto"]:
        # Aquí coloca tu lógica de consulta a Wasi
        inventario_cache["texto"] = "ID:123 - Apartamento en Valencia..."
        inventario_cache["ultima_actualizacion"] = datetime.now()
    return inventario_cache["texto"]

def asignar_agente_round_robin():
    sincronizar_google_sheet()
    if not sheets_cache["agentes"]: return None
    sheets_cache["ultimo_indice"] = (sheets_cache["ultimo_indice"] + 1) % len(sheets_cache["agentes"])
    return sheets_cache["agentes"][sheets_cache["ultimo_indice"]]

def enviar_notificaciones_telegram(agente, telefono_destino, datos_lead):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    agente_id = str(agente.get("telegram_id", "")).strip()
    link_wa = f"https://wa.me/{telefono_destino.replace('+', '').replace(' ', '')}"
    msg = f"👤 *¡Nuevo lead asignado!* \n\n*Datos:* {datos_lead}\n📲 *Contacto:* {link_wa}"
    
    for chat_id in [agente_id, admin_id]:
        if chat_id and chat_id != "None":
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                          json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=10)

def consultar_ia(historial):
    # Lógica de OpenRouter / Gemini
    return "Respuesta de prueba"

# --- 2. WEBHOOK (ESTO DEBE IR AL FINAL) ---

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        # Llamamos a las funciones declaradas arriba
        sincronizar_google_sheet()
        inventario = obtener_inventario()
        
        # El resto de tu lógica...
        return {"replies": [{"message": "Procesando..."}]}
    except Exception as e:
        logger.error(f"🔴 ERROR: {e}")
        return {"replies": [{"message": "Error interno."}]}
