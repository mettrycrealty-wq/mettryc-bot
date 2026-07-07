import os
import requests
import logging
import time
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
MODELO_OPENROUTER = "deepseek/deepseek-chat"

# --- FUNCIONES DE SOPORTE ---

def obtener_inventario():
    if datetime.now() - cache["ultima_actualizacion"] > timedelta(hours=24) or not cache["inventario_texto"]:
        # (Lógica de Wasi igual a la que tenías, omitida aquí para brevedad)
        pass 
    return cache["inventario_texto"]

def sincronizar_google_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL") 
    if not script_url: return

    # Si la caché tiene más de 1 hora o está vacía, refrescamos
    if datetime.now() - agentes_cache["ultima_actualizacion"] > timedelta(hours=1) or not agentes_cache["lista"]:
        try:
            response = requests.get(script_url, timeout=15)
            data = response.json()
            # Asignación segura
            agentes_cache["lista"] = data.get("agentes", [])
            agentes_cache["captadores"] = data.get("captadores", {})
            agentes_cache["ultima_actualizacion"] = datetime.now()
            logger.info(f"✅ Sincronizado: {len(agentes_cache['lista'])} agentes.")
        except Exception as e:
            logger.error(f"🔴 Error Sheets: {e}")

def asignar_agente_round_robin():
    sincronizar_google_sheet()
    lista = agentes_cache["lista"]
    if not lista:
        logger.error("🔴 Round Robin: Lista de agentes VACÍA.")
        return None
    
    agentes_cache["ultimo_indice"] += 1
    if agentes_cache["ultimo_indice"] >= len(lista):
        agentes_cache["ultimo_indice"] = 0
    return lista[agentes_cache["ultimo_indice"]]

# --- RUTAS ---

@app.get("/test-telegram")
async def test_telegram():
    # ... (Tu código de test que funcionó) ...
    return {"status": "ok"}

@app.post("/webhook")
async def handle_request(request: Request):
    data = await request.json()
    # (Validación API KEY...)
    payload = data.get("query") if isinstance(data.get("query"), dict) else data
    sender = str(payload.get("sender", "")).strip()
    mensaje_cliente = str(payload.get("message", ""))
    
    # FORZAR SINCRONIZACIÓN ANTES DE ASIGNAR
    sincronizar_google_sheet()
    
    # ... (Lógica de IA y Prompt ...)
    
    # Al momento de asignar:
    agente = asignar_agente_round_robin()
    if agente:
        logger.info(f"Asignando a {agente['nombre']}")
        enviar_notificaciones_telegram(agente, telefono_final, datos_lead_raw)
    else:
        logger.error("No se encontró agente para asignar.")
    
    return {"replies": [{"message": "Respuesta IA..."}]}
