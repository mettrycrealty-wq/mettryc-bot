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
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=10)

def consultar_ia(historial):
    headers = {"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}", "Content-Type": "application/json"}
    payload = {"messages": historial, "temperature": 0.3} # Temperatura baja para que sea más lógica
    try:
        payload["model"] = MODELO_PRINCIPAL
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=20)
        return resp.json()['choices'][0]['message']['content']
    except:
        payload["model"] = MODELO_RESPALDO
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=20)
        return resp.json()['choices'][0]['message']['content']

# --- WEBHOOK ---

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = str(payload.get("sender", "")).strip()
        mensaje = str(payload.get("message", ""))
        
        if not mensaje.strip(): return {"replies": []}

        sincronizar_google_sheet()
        inventario = "..." # (Tu lógica de obtención de inventario aquí)
        directorio = "\n".join([f"- {k}: {v}" for k, v in sheets_cache["captadores"].items()])
        
        # PROMPT BLINDADO: Instrucciones claras para evitar asignaciones prematuras
        prompt_sistema = f"""
        Eres Broker Inmobiliario de Mettryc Realty. 
        INVENTARIO: {inventario}
        DIRECTORIO (Solo para colegas): {directorio}

        FLUJO OBLIGATORIO:
        1. Identifica si es cliente o colega.
        2. Si es COLEGA: Dale la info del captador del directorio y termina.
        3. Si es CLIENTE: Busca en el inventario las 3 propiedades más similares a lo que pide. Preséntalas brevemente.
        4. SOLO si el cliente muestra interés en una propiedad, pídele sus datos.
        5. NUNCA menciones al captador si es cliente.
        6. SOLO cuando tengas Nombre, Correo y Teléfono del cliente, escribe al final: ###LEAD_CAPTURED###Nombre: X | Correo: Y | Telefono: Z###
        """
        
        if sender not in memoria_conversaciones: memoria_conversaciones[sender] = []
        historial = [{"role": "system", "content": prompt_sistema}] + memoria_conversaciones[sender][-10:] + [{"role": "user", "content": mensaje}]
        
        respuesta_bot = consultar_ia(historial)
        
        # Procesamiento solo si la IA generó la etiqueta final
        if "###LEAD_CAPTURED###" in respuesta_bot and sender not in clientes_procesados:
            datos = respuesta_bot.split("###LEAD_CAPTURED###")[1].replace("###", "")
            nums = re.findall(r'\+?\d{8,15}', datos)
            telefono = nums[0] if nums else sender
            agente = asignar_agente_round_robin()
            if agente:
                enviar_notificaciones_telegram(agente, telefono, datos)
                respuesta_bot += "\n\n¡Un asesor te contactará pronto!"
                clientes_procesados.add(sender)
        
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_bot})
        return {"replies": [{"message": respuesta_bot}]}
    except Exception as e:
        logger.error(f"🔴 ERROR: {e}")
        return {"replies": [{"message": "Estamos procesando..."}]}
