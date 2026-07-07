import os
import requests
import logging
import time
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Configuración
sheets_cache = {"agentes": [], "captadores": {}, "ultimo_indice": -1, "ultima_actualizacion": datetime.min}
memoria_conversaciones = {}
clientes_procesados = set()

# Modelos
MODELO_PRINCIPAL = "deepseek/deepseek-chat"
MODELO_RESPALDO = "google/gemini-2.0-flash-lite" # Tu nuevo Plan B

def consultar_ia(historial):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}", "Content-Type": "application/json"}
    
    # Intento con modelo principal
    try:
        response = requests.post(url, headers=headers, json={"model": MODELO_PRINCIPAL, "messages": historial}, timeout=15)
        if response.status_code == 200 and 'choices' in response.json():
            return response.json()['choices'][0]['message']['content']
    except Exception as e:
        logger.warning(f"Falla en {MODELO_PRINCIPAL}: {e}")

    # Plan B: Gemini Flash Lite
    try:
        logger.info(f"Activando Plan B: {MODELO_RESPALDO}")
        response = requests.post(url, headers=headers, json={"model": MODELO_RESPALDO, "messages": historial}, timeout=15)
        if response.status_code == 200 and 'choices' in response.json():
            return response.json()['choices'][0]['message']['content']
    except Exception as e:
        logger.error(f"Falla total en IA: {e}")
    
    return "Lo siento, estamos teniendo problemas técnicos. Por favor, escribe 'asesor' para contactar a un humano."

def sincronizar_google_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL")
    if not script_url or (datetime.now() - sheets_cache["ultima_actualizacion"] < timedelta(minutes=30) and sheets_cache["agentes"]):
        return
    try:
        response = requests.get(script_url, timeout=10)
        data = response.json()
        sheets_cache["agentes"] = data.get("agentes", [])
        sheets_cache["captadores"] = data.get("captadores", {})
        sheets_cache["ultima_actualizacion"] = datetime.now()
    except Exception as e:
        logger.error(f"Error Sheets: {e}")

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        # ... (Validación de API KEY) ...
        
        sincronizar_google_sheet()
        
        # ... (Construcción del prompt con directorio_telefonic_str) ...
        
        historial_api = [{"role": "system", "content": prompt_sistema}] + memoria_conversaciones.get(sender, []) + [{"role": "user", "content": mensaje_cliente}]
        
        # LLAMADA A LA NUEVA FUNCIÓN CON PLAN B
        respuesta_bot = consultar_ia(historial_api)
        
        # ... (Resto de tu lógica de captura de lead y Telegram) ...
        
        return {"replies": [{"message": respuesta_bot}]}
    except Exception as e:
        logger.error(f"Error general: {e}")
        return {"replies": [{"message": "Estamos procesando tu solicitud."}]}
