import os
import requests
import logging
import time
import re
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()

# Sistemas de Caché y Memoria
cache = {"inventario_texto": "", "ultima_actualizacion": datetime.min}
agentes_cache = {"lista": [], "captadores": {}, "ultimo_indice": -1, "ultima_actualizacion": datetime.min}
memoria_conversaciones = {}
clientes_procesados = set() 
MODELO_PRINCIPAL = "deepseek/deepseek-chat"
MODELO_RESPALDO = "google/gemini-flash-1.5"

# --- FUNCIONES DE SOPORTE ---

def obtener_inventario_desde_wasi():
    propiedades_limpias = []
    take = 100
    skip = 0
    max_propiedades = 2000
    while True:
        url = f"https://api.wasi.co/v1/property/search?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}&take={take}&skip={skip}"
        try:
            response = requests.get(url, timeout=30)
            data = response.json()
            contador_pagina = 0
            for key, value in data.items():
                if isinstance(value, dict) and key.isdigit():
                    contador_pagina += 1
                    user_data = value.get('user_data', {})
                    asesor_encargado = f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip() or "Asesor Mettryc"
                    prop = f"-[ID: {value.get('id_property')}] {value.get('title')} | Encargado: {asesor_encargado} | Enlace: https://www.mettryc.com/inmueble/{value.get('id_property')}"
                    propiedades_limpias.append(prop)
            if contador_pagina < take: break
            skip += take
        except: break
    return "\n".join(propiedades_limpias)

def obtener_inventario():
    if datetime.now() - cache["ultima_actualizacion"] > timedelta(hours=24) or not cache["inventario_texto"]:
        cache["inventario_texto"] = obtener_inventario_desde_wasi()
        cache["ultima_actualizacion"] = datetime.now()
    return cache["inventario_texto"]

def sincronizar_google_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL") 
    if not script_url: return
    if datetime.now() - agentes_cache["ultima_actualizacion"] > timedelta(hours=1) or not agentes_cache["lista"]:
        try:
            response = requests.get(script_url, timeout=15)
            data = response.json()
            if isinstance(data, dict):
                agentes_cache["lista"] = data.get("agentes", [])
                agentes_cache["captadores"] = data.get("captadores", {})
                agentes_cache["ultima_actualizacion"] = datetime.now()
                logger.info(f"✅ Sincronizado: {len(agentes_cache['lista'])} agentes.")
        except Exception as e: logger.error(f"🔴 Error Sheets: {e}")

def asignar_agente_round_robin():
    sincronizar_google_sheet()
    if not agentes_cache["lista"]: return None
    agentes_cache["ultimo_indice"] = (agentes_cache["ultimo_indice"] + 1) % len(agentes_cache["lista"])
    return agentes_cache["lista"][agentes_cache["ultimo_indice"]]

def enviar_notificaciones_telegram(agente, telefono_destino, datos_lead):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    agente_id = agente.get("telegram_id")
    info_cliente = f"\n\n*Datos del Cliente:*\n{datos_lead}\n\n📲 *Contactar:* https://wa.me/{telefono_destino}"
    
    for target in [agente_id, admin_id]:
        if target:
            requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", 
                          json={"chat_id": target, "text": f"👤 *¡Nuevo lead asignado!* {info_cliente}", "parse_mode": "Markdown"}, timeout=10)

def consultar_ia(historial):
    # (Lógica de OpenRouter con fallback, omitida por brevedad, usa la que tenías que sí funcionaba)
    pass

# --- RUTAS ---

@app.get("/test-telegram")
async def test_tg():
    # ... (Tu función de prueba exitosa) ...
    return {"status": "ok"}

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = str(payload.get("sender", "")).strip()
        mensaje_cliente = str(payload.get("message", ""))
        
        inventario = obtener_inventario()
        sincronizar_google_sheet()
        
        # ... (Toda tu lógica de IA y captura de Lead) ...
        # IMPORTANTE: Aquí abajo, cuando la IA capture el lead:
        agente = asignar_agente_round_robin()
        if agente:
            enviar_notificaciones_telegram(agente, telefono_final, datos_lead_raw)
            # ...
        
        return {"replies": [{"message": "Respuesta IA"}]}
    except Exception as e:
        logger.error(f"Error: {e}")
        return {"replies": [{"message": "Procesando..."}]}
