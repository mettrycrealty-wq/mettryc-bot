import os
import requests
import logging
import time
import re
from datetime import datetime, timedelta
from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = FastAPI()

sheets_cache = {"agentes": [], "captadores": {}, "ultimo_indice": -1, "ultima_actualizacion": datetime.min}
memoria_conversaciones = {}
clientes_procesados = set()

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
            logger.info(f"✅ Sincronizados {len(sheets_cache['agentes'])} agentes.")
        except Exception as e: logger.error(f"🔴 Error Sheets: {e}")

def asignar_agente_round_robin():
    sincronizar_google_sheet()
    if not sheets_cache["agentes"]: return None
    sheets_cache["ultimo_indice"] = (sheets_cache["ultimo_indice"] + 1) % len(sheets_cache["agentes"])
    return sheets_cache["agentes"][sheets_cache["ultimo_indice"]]

def enviar_notificaciones_telegram(agente, telefono_destino, datos_lead):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    agente_id = str(agente.get("telegram_id", "")).strip()
    msg = f"👤 *¡Nuevo lead asignado!* \n\n*Datos:* {datos_lead}\n📲 *Contacto:* https://wa.me/{telefono_destino}"
    
    for chat_id in [agente_id, admin_id]:
        if chat_id and chat_id != "None":
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                          json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=10)

def consultar_ia(historial):
    headers = {"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}", "Content-Type": "application/json"}
    # Intento 1: DeepSeek
    try:
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={"model": "deepseek/deepseek-chat", "messages": historial}, timeout=15)
        return resp.json()['choices'][0]['message']['content']
    except:
        # Intento 2: Plan B (Gemini)
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={"model": "google/gemini-2.5-flash-lite", "messages": historial}, timeout=15)
        return resp.json()['choices'][0]['message']['content']

# --- WEBHOOK ---

@app.post("/webhook")
async def handle_request(request: Request):
    respuesta_bot = "Estamos procesando tu solicitud..."
    try:
        data = await request.json()
        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = str(payload.get("sender", "")).strip()
        mensaje = str(payload.get("message", ""))
        
        sincronizar_google_sheet()
        directorio = "\n".join([f"- {k}: {v}" for k, v in sheets_cache["captadores"].items()])
        
        prompt = f"Eres Broker de Mettryc. Directorio: {directorio}. Regla: Si hay datos reales, pon al final: ###LEAD_CAPTURED###Nombre: X | Correo: Y | Telefono: Z###"
        
        if sender not in memoria_conversaciones: memoria_conversaciones[sender] = []
        historial = [{"role": "system", "content": prompt}] + memoria_conversaciones[sender] + [{"role": "user", "content": mensaje}]
        
        respuesta_bot = consultar_ia(historial)
        
        if "###LEAD_CAPTURED###" in respuesta_bot:
            datos = respuesta_bot.split("###LEAD_CAPTURED###")[1].replace("###", "")
            nums = re.findall(r'\+?\d{8,15}', datos)
            telefono = nums[0] if nums else sender
            agente = asignar_agente_round_robin()
            if agente:
                enviar_notificaciones_telegram(agente, telefono, datos)
                respuesta_bot += "\n\n¡He asignado tu caso a un asesor!"
                
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_bot})
        return {"replies": [{"message": respuesta_bot}]}
    except Exception as e:
        logger.error(f"🔴 ERROR: {e}")
        return {"replies": [{"message": "Estamos procesando..."}]}
