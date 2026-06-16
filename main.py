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
                    logger.info("Inventario completo alcanzado.")
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
    if datetime.now() - cache["ultima_actualizacion"] > timedelta(hours=24) or not cache["inventario_texto"]:
        inventario_nuevo = obtener_inventario_desde_wasi()
        if inventario_nuevo: 
            cache["inventario_texto"] = inventario_nuevo
            cache["ultima_actualizacion"] = datetime.now()
    return cache["inventario_texto"]

def obtener_agentes_desde_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL") 
    if not script_url: 
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
    
    link_wa = f"https://wa.me/{whatsapp_cliente}"
    info_cliente = f"\n\n*Datos del Cliente:*\n{datos_lead}\n\n📲 *Chat Original / Contactar:* {link_wa}"
    
    url_tg = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    
    # 1. Notificación al Asesor Asignado
    if telegram_token and agente_id:
        try:
            msg_agente = f"👤 *¡Tienes un nuevo cliente asignado!* \n{info_cliente}"
            requests.post(url_tg, json={"chat_id": agente_id, "text": msg_agente, "parse_mode": "Markdown"}, timeout=5)
            logger.info(f"Notificación enviada al agente: {agente['nombre']}")
        except Exception as e:
            logger.error(f"Error enviando Telegram al agente: {e}")
            
    # 2. Copia de Monitoreo al Administrador con el nombre del Asesor
    if telegram_token and admin_id:
        try:
            msg_admin = f"👁️ *REPORTE ADMIN - SEGUIMIENTO*\n👤 *Asesor Asignado:* {agente['nombre']}\n{info_cliente}"
            requests.post(url_tg, json={"chat_id": admin_id, "text": msg_admin, "parse_mode": "Markdown"}, timeout=5)
            logger.info("Notificación de seguimiento enviada al administrador.")
        except Exception as e:
            logger.error(f"Error enviando Telegram al admin: {e}")

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        if request.headers.get("x-api-key") not in os.getenv("API_KEYS_AGENTES", "").split(","):
            raise HTTPException(status_code=403, detail="Acceso denegado")

        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = str(payload.get("sender", ""))
        mensaje_cliente = str(payload.get("message", ""))
            
        if not mensaje_cliente.strip(): 
            return {"replies": []}

        inventario = obtener_inventario()
        if sender not in memoria_conversaciones: 
            memoria_conversaciones[sender] = []
        
        prompt_sistema = f"""
        Eres un Broker Inmobiliario virtual de Mettryc Realty altamente eficiente.
        
        INVENTARIO REAL DISPONIBLE:
        {inventario}
        
        REGLAS DE OPERACIÓN:
        1. RESPUESTAS CORTAS Y ATRACTIVAS. Entrega máximo 3 alternativas de inmuebles que coincidan con la búsqueda, utilizando siempre su enlace crudo original.
        2. FLUJO NATURAL DE VENTAS. Deja que el cliente te pregunte sobre zonas, precios, metrajes o características y responde con fluidez y amabilidad basándote únicamente en el inventario. No exijas datos de contacto de buenas a primeras.
        3. MOMENTO DE CAPTURA (CUANDO HAY INTERÉS):
           Solo si el cliente demuestra interés real en realizar una visita, conocer la dirección exacta, recibir asistencia personalizada de un humano o cerrar una negociación, detendrás la venta e iniciarás el protocolo de registro solicitando los datos uno a uno:
           - Primero, su Nombre Completo.
           - Segundo, su Correo Electrónico.
           - Tercero, pídele que te confirme su Número de WhatsApp de contacto (explícale cordialmente que es indispensable para que el asesor asignado le agende de inmediato, ya que el sistema centralizado solo registra nombres provisorios).
        
        4. Al consolidar Nombre, Correo y Número de WhatsApp válidos en la conversación, añade OBLIGATORIAMENTE la etiqueta al cierre de tu mensaje:
        ###LEAD_CAPTURED###Nombre: [Nombre] | Correo: [Correo] | Telefono: [Número de WhatsApp de contacto] | Interés: [Breve descripción de lo que busca]###
        """
        
        historial_api = [{"role": "system", "content": prompt_sistema}] + memoria_conversaciones[sender] + [{"role": "user", "content": mensaje_cliente}]
        
        url_ia = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
            "Content-Type": "application/json"
        }
        
        respuesta_raw = requests.post(url_ia, headers=headers, json={"model": MODELO_OPENROUTER, "messages": historial_api}, timeout=30)
        respuesta_bot = respuesta_raw.json()['choices'][0]['message']['content']
        
        if "###LEAD_CAPTURED###" in respuesta_bot:
            try:
                partes = respuesta_bot.split("###LEAD_CAPTURED###")
                texto_cliente = partes[0].strip()
                datos_lead_raw = partes[1].replace("###", "").strip()
                
                agente = asignar_agente_round_robin()
                if agente:
                    enviar_notificaciones_telegram(agente, sender, datos_lead_raw)
                    texto_cliente += f"\n\n¡Perfecto! Sus datos han sido registrados. Nuestro especialista, *{agente['nombre']}*, ha tomado el caso y se pondrá en contacto directo con usted a la brevedad."
                
                respuesta_bot = texto_cliente
            except Exception as e:
                logger.error(f"Error procesando el cierre de lead: {e}")
        
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_bot})
        
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        return {"replies": [{"message": respuesta_bot}]}
        
    except Exception as e:
        logger.error(f"Error general: {e}")
        return {"replies": [{"message": "Estamos procesando tu solicitud, por favor intenta nuevamente en un momento."}]}

