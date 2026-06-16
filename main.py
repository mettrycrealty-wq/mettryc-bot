import os
import requests
import logging
import time
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Sistemas de Caché
cache = {"inventario_texto": "", "ultima_actualizacion": datetime.min}
agentes_cache = {"lista": [], "ultimo_indice": -1, "ultima_actualizacion": datetime.min}
memoria_conversaciones = {}
MODELO_OPENROUTER = "deepseek/deepseek-chat"

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
                    id_prop = value.get('id_property')
                    enlace = f"https://www.mettryc.com/inmueble/{id_prop}"
                    prop = f"-[ID: {id_prop}] {value.get('title')} | Ciudad: {value.get('city_label')} | Enlace: {enlace}"
                    propiedades_limpias.append(prop)
            if contador_pagina < take: break
            skip += take
            if skip >= max_propiedades: break
            time.sleep(2)
        except Exception: break
    return "\n".join(propiedades_limpias)

def obtener_agentes_desde_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL") 
    if not script_url: return agentes_cache["lista"]
    if datetime.now() - agentes_cache["ultima_actualizacion"] > timedelta(hours=1) or not agentes_cache["lista"]:
        try:
            response = requests.get(script_url, timeout=15)
            agentes_cache["lista"] = response.json()
            agentes_cache["ultima_actualizacion"] = datetime.now()
        except Exception as e: logger.error(f"Error cargando agentes: {e}")
    return agentes_cache["lista"]

def asignar_agente_round_robin():
    lista = obtener_agentes_desde_sheet()
    if not lista: return None
    agentes_cache["ultimo_indice"] = (agentes_cache["ultimo_indice"] + 1) % len(lista)
    return lista[agentes_cache["ultimo_indice"]]

def enviar_notificaciones_telegram(agente, whatsapp_cliente, datos_lead):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    agente_id = agente.get("telegram_id")
    
    # Si el cliente es un contacto guardado, whatsapp_cliente trae un nombre, usamos un aviso
    # Nota: Si capturamos el número en la conversación, deberías actualizar whatsapp_cliente
    link_wa = f"https://wa.me/{whatsapp_cliente}"
    info_cliente = f"\n\n*Datos del Cliente:*\n{datos_lead}\n\n📲 *Contactar:* {link_wa}"
    
    url_tg = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    
    if telegram_token and agente_id:
        try:
            requests.post(url_tg, json={"chat_id": agente_id, "text": f"👤 *¡Tienes un nuevo cliente!* (Asignación Directa)\n{info_cliente}", "parse_mode": "Markdown"}, timeout=5)
        except Exception as e: logger.error(f"Error Telegram agente: {e}")
            
    if telegram_token and admin_id:
        try:
            # Notificación mejorada para el Admin incluyendo nombre del agente
            msg_admin = f"👁️ *REPORTE ADMIN*\n👤 *Agente asignado:* {agente['nombre']}\n{info_cliente}"
            requests.post(url_tg, json={"chat_id": admin_id, "text": msg_admin, "parse_mode": "Markdown"}, timeout=5)
        except Exception as e: logger.error(f"Error Telegram admin: {e}")

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = str(payload.get("sender", ""))
        mensaje_cliente = str(payload.get("message", ""))
        
        # Validación de Contacto Guardado (si sender tiene letras, es un nombre)
        es_numero_valido = sender.replace("+", "").replace(" ", "").isdigit()
        if not es_numero_valido and "###LEAD_CAPTURED###" not in mensaje_cliente:
             # Si no es número y no estamos capturando lead, pedir número
             if "confirmado" not in mensaje_cliente.lower():
                return {"replies": [{"message": "¡Hola! Para poder asignarte un asesor y enviarte la información, por favor confírmame tu número de WhatsApp (ej: +58414...)"}]}

        inventario = obtener_inventario()
        if sender not in memoria_conversaciones: memoria_conversaciones[sender] = []
        
        prompt_sistema = f"""Eres Broker Inmobiliario. INVENTARIO: {inventario}. 
        REGLAS: Si el cliente es un contacto guardado (enviaste un nombre), pídale primero su número de WhatsApp.
        Una vez tengas Nombre, Correo y Número, termina con:
        ###LEAD_CAPTURED###Nombre: [Nombre] | Correo: [Correo] | Telefono: [Numero] | Interés: [Lo que busca]###"""
        
        historial = [{"role": "system", "content": prompt_sistema}] + memoria_conversaciones[sender] + [{"role": "user", "content": mensaje_cliente}]
        
        url_ia = "https://openrouter.ai/api/v1/chat/completions"
        respuesta = requests.post(url_ia, headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}", "Content-Type": "application/json"}, 
                                  json={"model": MODELO_OPENROUTER, "messages": historial}).json()['choices'][0]['message']['content']
        
        if "###LEAD_CAPTURED###" in respuesta:
            partes = respuesta.split("###LEAD_CAPTURED###")
            datos_lead = partes[1].replace("###", "").strip()
            agente = asignar_agente_round_robin()
            if agente:
                enviar_notificaciones_telegram(agente, sender, datos_lead)
                respuesta = partes[0].strip() + f"\n\n¡Perfecto! El asesor {agente['nombre']} te contactará."
        
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta})
        return {"replies": [{"message": respuesta}]}
    except Exception as e:
        logger.error(f"Error: {e}")
        return {"replies": [{"message": "Por favor intenta de nuevo."}]}


