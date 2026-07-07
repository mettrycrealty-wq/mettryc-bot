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

# Caché Global
sheets_cache = {"agentes": [], "captadores": {}, "ultimo_indice": -1, "ultima_actualizacion": datetime.min}
inventario_cache = {"texto": "", "ultima_actualizacion": datetime.min}
memoria_conversaciones = {}
clientes_procesados = set()

# Configuración de Modelos
MODELO_PRINCIPAL = "deepseek/deepseek-chat"
MODELO_RESPALDO = "google/gemini-2.0-flash-lite"

# --- FUNCIONES DE SOPORTE ---

def obtener_inventario_desde_wasi():
    propiedades_limpias = []
    # Parámetros de filtrado: status=1 (Activo) y destacada=1 (si tu CRM lo soporta)
    # Ajusta los parámetros según la documentación de tu API de Wasi
    params = f"&status=1&featured=1" 
    
    take = 100
    skip = 0
    
    while True:
        url = f"https://api.wasi.co/v1/property/search?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}&take={take}&skip={skip}{params}"
        try:
            response = requests.get(url, timeout=30)
            data = response.json()
            contador_pagina = 0
            
            for key, value in data.items():
                if isinstance(value, dict) and key.isdigit():
                    contador_pagina += 1
                    # Filtrado de seguridad
                    if value.get('status') != '1': continue 
                    
                    id_prop = value.get('id_property')
                    prop = f"- [ID: {id_prop}] {value.get('title')} | Ciudad: {value.get('city_label')} | Precio Venta: {value.get('sale_price_label')} | Enlace: https://www.mettryc.com/inmueble/{id_prop}"
                    propiedades_limpias.append(prop)
            
            if contador_pagina < take: break
            skip += take
        except: break
    return "\n".join(propiedades_limpias)

def obtener_inventario():
    if datetime.now() - inventario_cache["ultima_actualizacion"] > timedelta(hours=6) or not inventario_cache["texto"]:
        inventario_cache["texto"] = obtener_inventario_desde_wasi()
        inventario_cache["ultima_actualizacion"] = datetime.now()
    return inventario_cache["texto"]

# ... (Mantén aquí las funciones sincronizar_google_sheet, asignar_agente_round_robin y enviar_notificaciones_telegram igual que antes) ...

def consultar_ia(historial):
    headers = {"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}", "Content-Type": "application/json"}
    payload = {"messages": historial, "temperature": 0.2}
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
        
        inventario = obtener_inventario()
        sincronizar_google_sheet()
        directorio = "\n".join([f"- {k}: WhatsApp {v}" for k, v in sheets_cache["captadores"].items()])
        
        prompt_sistema = f"""
        Eres un Broker Inmobiliario de Mettryc Realty. Trabaja por FASES:
        FASE 1: Detecta si es CLIENTE o COLEGA.
        FASE 2: Si es COLEGA, entrega info del captador del DIRECTORIO. Si es CLIENTE, busca en el INVENTARIO (solo propiedades activas/destacadas) y presenta 3 opciones (formato Título, Ubicación, Precio, Enlace).
        FASE 3: Solo si el cliente muestra interés real, pide Nombre, Correo y WhatsApp.
        Finaliza con: ###LEAD_CAPTURED###Nombre: X | Correo: Y | Telefono: Z###
        
        INVENTARIO: {inventario}
        DIRECTORIO: {directorio}
        """
        
        if sender not in memoria_conversaciones: memoria_conversaciones[sender] = []
        historial = [{"role": "system", "content": prompt_sistema}] + memoria_conversaciones[sender][-10:] + [{"role": "user", "content": mensaje}]
        
        respuesta_bot = consultar_ia(historial)
        
        if "###LEAD_CAPTURED###" in respuesta_bot and sender not in clientes_procesados:
            # Lógica de asignación...
            pass
            
        return {"replies": [{"message": respuesta_bot}]}
    except Exception as e:
        logger.error(f"🔴 ERROR: {e}")
        return {"replies": [{"message": "Estamos procesando..."}]}
