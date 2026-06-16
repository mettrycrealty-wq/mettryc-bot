import os
import csv
import requests
import logging
import time
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Sistemas de Caché
cache = {
    "inventario_texto": "",
    "ultima_actualizacion": datetime.min
}

agentes_cache = {
    "lista": [],
    "ultimo_indice": -1,
    "ultima_actualizacion": datetime.min
}

memoria_conversaciones = {}
MODELO_OPENROUTER = "deepseek/deepseek-chat"

def obtener_inventario_desde_wasi():
    propiedades_limpias = []
    take = 100
    skip = 0
    max_propiedades = 2000
    
    logger.info("Iniciando descarga completa del inventario desde Wasi...")
    
    while True:
        url = f"https://api.wasi.co/v1/property/search?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}&take={take}&skip={skip}"
        exito_pagina = False
        intentos = 0
        
        while intentos < 3 and not exito_pagina:
            try:
                logger.info(f"Consultando Wasi (Propiedad {skip} a {skip + take})...")
                response = requests.get(url, timeout=30)
                data = response.json()
                
                contador_pagina = 0
                for key, value in data.items():
                    if isinstance(value, dict) and key.isdigit():
                        contador_pagina += 1
                        id_prop = value.get('id_property')
                        enlace_web = f"https://www.mettryc.com/inmueble/{id_prop}"
                        
                        prop = (
                            f"-[ID: {id_prop}] {value.get('title')} | "
                            f"Ciudad: {value.get('city_label')} | Zona: {value.get('zone_label')} | "
                            f"Venta: {value.get('sale_price_label')} | Renta: {value.get('rent_price_label')} | "
                            f"Área: {value.get('area')}m2 | Hab: {value.get('bedrooms')} | Baños: {value.get('bathrooms')} | "
                            f"Enlace: {enlace_web}"
                        )
                        propiedades_limpias.append(prop)
                
                exito_pagina = True
                
                if contador_pagina < take:
                    return "\n".join(propiedades_limpias)
                
                skip += take
                time.sleep(2)
                
            except Exception as e:
                intentos += 1
                logger.warning(f"Intento {intentos} fallido en skip {skip}: {e}. Esperando 5 segundos...")
                time.sleep(5)
        
        if not exito_pagina:
            logger.error(f"Se agotaron los reintentos en skip {skip}. Continuando con lo obtenido.")
            break
            
        if skip >= max_propiedades:
            break
            
    logger.info(f"¡Éxito! Se almacenaron {len(propiedades_limpias)} propiedades.")
    return "\n".join(propiedades_limpias)

def obtener_inventario():
    if datetime.now() - cache["ultima_actualizacion"] > timedelta(hours=24):
        inventario_nuevo = obtener_inventario_desde_wasi()
        if inventario_nuevo: 
            cache["inventario_texto"] = inventario_nuevo
            cache["ultima_actualizacion"] = datetime.now()
    return cache["inventario_texto"]

def obtener_agentes_desde_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL") 
    if not script_url:
        logger.warning("GOOGLE_SHEET_TURNOS_URL no configurada.")
        return agentes_cache["lista"]

    if datetime.now() - agentes_cache["ultima_actualizacion"] > timedelta(hours=1) or not agentes_cache["lista"]:
        try:
            logger.info("Conectando con Google Apps Script para actualizar agentes...")
            response = requests.get(script_url, timeout=15)
            lista_nueva = response.json()
            
            if isinstance(lista_nueva, list) and len(lista_nueva) > 0:
                agentes_cache["lista"] = lista_nueva
                agentes_cache["ultima_actualizacion"] = datetime.now()
                logger.info(f"✅ Sincronizados {len(lista_nueva)} agentes.")
        except Exception as e:
            logger.error(f"Error cargando agentes: {e}")
            
    return agentes_cache["lista"]

def asignar_agente_round_robin():
    lista_agentes = obtener_agentes_desde_sheet()
    if not lista_agentes:
        return None
        
    agentes_cache["ultimo_indice"] = (agentes_cache["ultimo_indice"] + 1) % len(lista_agentes)
    return lista_agentes[agentes_cache["ultimo_indice"]]

def enviar_notificaciones_telegram(agente, whatsapp_cliente, datos_lead):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    agente_id = agente.get("telegram_id")
    
    mensaje_base = f"🚨 *NUEVO LEAD ASIGNADO* 🚨\n\n*Datos del Cliente:*\n{datos_lead}\n\n📲 *Contactar ahora:*\n[Abrir WhatsApp](https://wa.me/{whatsapp_cliente})"
    
    url_tg = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    
    # 1. Notificar al Agente
    if telegram_token and agente_id:
        try:
            msg_agente = f"Hola {agente['nombre']},\n\n" + mensaje_base
            requests.post(url_tg, json={"chat_id": agente_id, "text": msg_agente, "parse_mode": "Markdown"}, timeout=5)
            logger.info(f"Notificación enviada al agente: {agente['nombre']}")
        except Exception as e:
            logger.error(f"Error enviando Telegram al agente: {e}")
            
    # 2. Notificar al Admin con el nombre del agente asignado
    if telegram_token and admin_id:
        try:
            msg_admin = f"👁️ *COPIA PARA ADMIN*\n👤 *Agente asignado:* {agente['nombre']}\n\n" + mensaje_base
            requests.post(url_tg, json={"chat_id": admin_id, "text": msg_admin, "parse_mode": "Markdown"}, timeout=5)
            logger.info("Notificación enviada al administrador.")
        except Exception as e:
            logger.error(f"Error enviando Telegram al admin: {e}")

def consultar_ia(mensajes):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
        "Content-Type": "application/json"
    }
    try:
        respuesta = requests.post(url, headers=headers, json={"model": MODELO_OPENROUTER, "messages": mensajes})
        return respuesta.json()['choices'][0]['message']['content']
    except Exception as e:
        logger.error(f"Error OpenRouter: {e}")
        return "Estamos experimentando alta demanda, intenta en un momento."

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        api_key = request.headers.get("x-api-key")
        
        if api_key not in os.getenv("API_KEYS_AGENTES", "").split(","):
            raise HTTPException(status_code=403, detail="Acceso denegado")

        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = payload.get("sender", "cliente_general")
        mensaje_cliente = str(payload.get("message", ""))
            
        if not mensaje_cliente.strip():
            return {"replies": []}

        inventario = obtener_inventario()
        
        if sender not in memoria_conversaciones:
            memoria_conversaciones[sender] = []
        
        prompt_sistema = f"""
        Eres un Broker Inmobiliario de Mettryc Realty.
        INVENTARIO DISPONIBLE:
        {inventario}
        
        REGLAS:
        1. RESPUESTAS CORTAS. Máximo 3 opciones con enlace crudo.
        2. CAPTURA DE LEADS: Si hay interés, pide Nombre y Correo.
        3. En el mensaje donde obtengas Nombre y Correo, incluye:
        ###LEAD_CAPTURED###Nombre: [Nombre] | Correo: [Correo] | Interés: [Lo que busca]###
        """
        
        historial_api = [{"role": "system", "content": prompt_sistema}]
        historial_api.extend(memoria_conversaciones[sender])
        historial_api.append({"role": "user", "content": mensaje_cliente})
        
        respuesta_bot = consultar_ia(historial_api)
        
        if "###LEAD_CAPTURED###" in respuesta_bot:
            try:
                partes = respuesta_bot.split("###LEAD_CAPTURED###")
                texto_cliente = partes[0].strip()
                datos_lead_raw = partes[1].replace("###", "").strip()
                
                agente = asignar_agente_round_robin()
                
                if agente:
                    enviar_notificaciones_telegram(agente, sender, datos_lead_raw)
                    texto_cliente += f"\n\n¡Perfecto! He registrado tus datos. Nuestro asesor, *{agente['nombre']}*, ha sido notificado y te contactará de inmediato."
                
                respuesta_bot = texto_cliente
            except Exception as e:
                logger.error(f"Error procesando lead: {e}")
        
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_bot})
        
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        return {"replies": [{"message": respuesta_bot}]}
    
    except Exception as e:
        logger.error(f"Error general: {e}")
        return {"replies": [{"message": "Estamos procesando tu solicitud, por favor intenta nuevamente."}]}
