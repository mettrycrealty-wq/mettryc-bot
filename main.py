import os
import requests
import logging
import time
import re
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Caché
sheets_cache = {"agentes": [], "captadores": {}, "ultimo_indice": -1, "ultima_actualizacion": datetime.min}
memoria_conversaciones = {}
clientes_procesados = set()
MODELO_PRINCIPAL = "deepseek/deepseek-chat"
MODELO_RESPALDO = "google/gemini-2.0-flash-lite"

# --- FUNCIONES DE SOPORTE ---

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
            logger.info(f"✅ Agentes: {len(sheets_cache['agentes'])}, Captadores: {len(sheets_cache['captadores'])}")
        except Exception as e: logger.error(f"🔴 Error Sheets: {e}")

def asignar_agente_round_robin():
    sincronizar_google_sheet()
    if not sheets_cache["agentes"]: return None
    sheets_cache["ultimo_indice"] = (sheets_cache["ultimo_indice"] + 1) % len(sheets_cache["agentes"])
    return sheets_cache["agentes"][sheets_cache["ultimo_indice"]]

def enviar_notificaciones_telegram(agente, telefono_destino, datos_lead):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    agente_id = str(agente.get("telegram_id", "")).strip()
    
    # Notificación al Agente
    if telegram_token and agente_id and agente_id != "None":
        msg = f"👤 *¡Nuevo lead asignado!* \n\n*Datos:* {datos_lead}\n📲 *Contacto:* https://wa.me/{telefono_destino}"
        requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", 
                      json={"chat_id": agente_id, "text": msg, "parse_mode": "Markdown"}, timeout=10)
        logger.info(f"Telegram enviado a {agente.get('nombre')} (ID: {agente_id})")

# --- WEBHOOK ---

@app.post("/webhook")
async def handle_request(request: Request):
    # Definimos respuesta_bot desde el inicio para evitar NameError
    respuesta_bot = "" 
    try:
        data = await request.json()
        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = str(payload.get("sender", "")).strip()
        mensaje_cliente = str(payload.get("message", ""))
        
        if not mensaje_cliente.strip(): return {"replies": []}

        # Sincronización y Directorio
        sincronizar_google_sheet()
        inventario = obtener_inventario()
        directorio = "\n".join([f"- {k}: {v}" for k, v in sheets_cache["captadores"].items()])
        
        # PROMPT DE SISTEMA (Definido aquí para asegurar su alcance)
        prompt_sistema = f"""
        Eres Broker Inmobiliario experto de Mettryc Realty.
        INVENTARIO: {inventario}
        DIRECTORIO CONFIDENCIAL: {directorio}
        REGLA: Si el cliente da datos reales, al final pon: ###LEAD_CAPTURED###Nombre: X | Correo: Y | Telefono: Z###
        """
        
        historial = [{"role": "system", "content": prompt_sistema}] + memoria_conversaciones.get(sender, []) + [{"role": "user", "content": mensaje_cliente}]
        
        # LLAMADA A IA
        respuesta_bot = consultar_ia(historial)
        
        # PROCESAMIENTO DE LEAD (Solo si respuesta_bot contiene algo)
        if respuesta_bot and "###LEAD_CAPTURED###" in respuesta_bot:
            # ... (Toda tu lógica de asignación que ya sabes que funciona) ...
            agente = asignar_agente_round_robin()
            if agente:
                enviar_notificaciones_telegram(agente, telefono_final, datos_lead)
                # ...
        
        return {"replies": [{"message": respuesta_bot}]}
    
    except Exception as e:
        logger.error(f"🔴 ERROR EN WEBHOOK: {e}", exc_info=True)
        return {"replies": [{"message": "Estamos procesando tu solicitud, por favor escribe de nuevo."}]}
