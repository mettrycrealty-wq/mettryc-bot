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

# Sistemas de Caché y Memoria
cache = {
    "inventario_texto": "",
    "ultima_actualizacion": datetime.min
}

sheets_cache = {
    "agentes": [],
    "captadores": {},
    "ultimo_indice": -1,
    "ultima_actualizacion": datetime.min
}

memoria_conversaciones = {}
clientes_procesados = set() 

# Modelos
MODELO_PRINCIPAL = "deepseek/deepseek-chat"
MODELO_RESPALDO = "google/gemini-flash-1.5"

def obtener_inventario_desde_wasi():
    propiedades_limpias = []
    take = 100
    skip = 0
    max_propiedades = 2000
    
    logger.info("Iniciando descarga del inventario desde Wasi...")
    
    while True:
        url = f"https://api.wasi.co/v1/property/search?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}&take={take}&skip={skip}"
        exito_pagina = False
        intentos = 0
        
        while intentos < 3 and not exito_pagina:
            try:
                response = requests.get(url, timeout=30)
                data = response.json()
                
                contador_pagina = 0
                for key, value in data.items():
                    if isinstance(value, dict) and key.isdigit():
                        contador_pagina += 1
                        id_prop = value.get('id_property')
                        enlace_web = f"https://www.mettryc.com/inmueble/{id_prop}"
                        
                        user_data = value.get('user_data', {})
                        asesor_encargado = f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip()
                        if not asesor_encargado:
                            asesor_encargado = "Asesor Mettryc"
                        
                        prop = (
                            f"-[ID: {id_prop}] {value.get('title')} | "
                            f"Ciudad: {value.get('city_label')} | Zona: {value.get('zone_label')} | "
                            f"Venta: {value.get('sale_price_label')} | Renta: {value.get('rent_price_label')} | "
                            f"Área: {value.get('area')}m2 | Hab: {value.get('bedrooms')} | Baños: {value.get('bathrooms')} | "
                            f"Encargado: {asesor_encargado} | "
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
                time.sleep(5)
        
        if not exito_pagina or skip >= max_propiedades:
            break
            
    return "\n".join(propiedades_limpias)

def obtener_inventario():
    if datetime.now() - cache["ultima_actualizacion"] > timedelta(hours=24) or not cache["inventario_texto"]:
        inventario_nuevo = obtener_inventario_desde_wasi()
        if inventario_nuevo: 
            cache["inventario_texto"] = inventario_nuevo
            cache["ultima_actualizacion"] = datetime.now()
    return cache["inventario_texto"]

def sincronizar_google_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL") 
    if not script_url: return

    if datetime.now() - sheets_cache["ultima_actualizacion"] > timedelta(hours=1) or not sheets_cache["agentes"]:
        try:
            response = requests.get(script_url, timeout=15)
            payload_sheet = response.json()
            if isinstance(payload_sheet, dict):
                sheets_cache["agentes"] = payload_sheet.get("agentes", [])
                sheets_cache["captadores"] = payload_sheet.get("captadores", {})
                sheets_cache["ultima_actualizacion"] = datetime.now()
                logger.info(f"✅ Sincronizados {len(sheets_cache['agentes'])} agentes y {len(sheets_cache['captadores'])} captadores.")
        except Exception as e:
            logger.error(f"🔴 Error sincronizando Sheets: {e}")

def asignar_agente_round_robin():
    sincronizar_google_sheet()
    lista_agentes = sheets_cache["agentes"]
    if not lista_agentes: return None
        
    sheets_cache["ultimo_indice"] += 1
    if sheets_cache["ultimo_indice"] >= len(lista_agentes):
        sheets_cache["ultimo_indice"] = 0
        
    return lista_agentes[sheets_cache["ultimo_indice"]]

def enviar_notificaciones_telegram(agente, telefono_destino, datos_lead):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    agente_id = agente.get("telegram_id")
    
    link_wa = f"https://wa.me/{telefono_destino}"
    info_cliente = f"\n\n*Datos del Cliente:*\n{datos_lead}\n\n📲 *Contactar de inmediato:* {link_wa}"
    url_tg = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    
    if telegram_token and agente_id:
        try:
            requests.post(url_tg, json={"chat_id": agente_id, "text": f"👤 *¡Nuevo lead asignado!* \n{info_cliente}", "parse_mode": "Markdown"}, timeout=10)
        except Exception as e: logger.error(f"Error TG Agente: {e}")
            
    if telegram_token and admin_id:
        try:
            requests.post(url_tg, json={"chat_id": admin_id, "text": f"👁️ *REPORTE ADMIN*\n👤 *Agente a cargo:* {agente['nombre']}\n{info_cliente}", "parse_mode": "Markdown"}, timeout=10)
        except Exception as e: logger.error(f"Error TG Admin: {e}")

def consultar_openrouter_con_fallback(historial_mensajes):
    url_ia = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}", "Content-Type": "application/json"}
    
    for intento in range(2):
        try:
            response = requests.post(url_ia, headers=headers, json={"model": MODELO_PRINCIPAL, "messages": historial_mensajes}, timeout=15)
            if response.status_code == 200: return response.json()['choices'][0]['message']['content']
        except Exception: pass
        time.sleep(1.5)
        
    try:
        response = requests.post(url_ia, headers=headers, json={"model": MODELO_RESPALDO, "messages": historial_mensajes}, timeout=15)
        if response.status_code == 200: return response.json()['choices'][0]['message']['content']
    except Exception: pass
        
    raise Exception("Modelos IA no disponibles.")

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        if request.headers.get("x-api-key") not in os.getenv("API_KEYS_AGENTES", "").split(","):
            raise HTTPException(status_code=403, detail="Acceso denegado")

        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = str(payload.get("sender", "")).strip()
        mensaje_cliente = str(payload.get("message", ""))
            
        if not mensaje_cliente.strip(): return {"replies": []}

        inventario = obtener_inventario()
        sincronizar_google_sheet()
        
        if sender not in memoria_conversaciones: memoria_conversaciones[sender] = []
        es_numero_puro = sender.replace("+", "").replace(" ", "").isdigit()
        
        if es_numero_puro:
            requisitos_lead = "su Nombre Completo y su Correo Electrónico"
            etiqueta_lead = "###LEAD_CAPTURED###Nombre: [Valor real] | Correo: [Valor real] | Interés: [Inmueble buscado]###"
        else:
            requisitos_lead = "su Nombre Completo, su Correo Electrónico y su Número de WhatsApp con código de país"
            etiqueta_lead = "###LEAD_CAPTURED###Nombre: [Valor real] | Correo: [Valor real] | Telefono: [Número Confirmado] | Interés: [Inmueble buscado]###"

        directorio_telefonic_str = "\n".join([f"- {k}: WhatsApp {v}" for k, v in sheets_cache["captadores"].items()])

        # PROMPT REESTRUCTURADO Y BLINDADO
        prompt_sistema = f"""
        Eres un Broker Inmobiliario experto de Mettryc Realty.
        TU OBJETIVO PRINCIPAL: Leer el inventario y recomendar EXACTAMENTE lo que el usuario solicita (filtrando estrictamente por ciudad, zona, precio, etc.). No inventes propiedades.

        REGLAS DE ATENCIÓN AL CLIENTE REGULAR:
        1. Sé amable, breve y SIEMPRE proporciona el enlace crudo de la propiedad.
        2. TÚ NO ASIGNAS ASESORES. El sistema lo hará internamente. NUNCA menciones el nombre del "Encargado" a un cliente regular.
        3. Cuando el cliente quiera visitar o más detalles, pídele: {requisitos_lead}.
        4. ÚNICAMENTE cuando el cliente te dé sus datos reales, escribe OBLIGATORIAMENTE esta etiqueta al final de tu mensaje:
        {etiqueta_lead}
        
        REGLA EXCLUSIVA PARA COLEGAS (AGENTES EXTERNOS):
        SOLO si el usuario afirma explícitamente ser "colega" o "agente", revélale el Nombre y WhatsApp del Encargado. A ellos NO les pidas datos ni uses la etiqueta LEAD_CAPTURED.
        
        DIRECTORIO DE CAPTADORES (CONFIDENCIAL):
        {directorio_telefonic_str}

        <INVENTARIO>
        {inventario}
        </INVENTARIO>
        """
        
        historial_api = [{"role": "system", "content": prompt_sistema}] + memoria_conversaciones[sender] + [{"role": "user", "content": mensaje_cliente}]
        respuesta_bot = consultar_openrouter_con_fallback(historial_api)
        
        if "###LEAD_CAPTURED###" in respuesta_bot:
            if "[Valor real]" in respuesta_bot or "[Nombre]" in respuesta_bot:
                logger.warning("Falso positivo detectado.")
                respuesta_bot = respuesta_bot.split("###LEAD_CAPTURED###")[0].strip()
            elif sender not in clientes_procesados:
                try:
                    partes = respuesta_bot.split("###LEAD_CAPTURED###")
                    texto_cliente = partes[0].strip()
                    datos_lead_raw = partes[1].replace("###", "").strip()
                    
                    telefono_final = sender
                    numero_encontrado_en_texto = False
                    
                    # EXTRACCIÓN INVENCIBLE CON REGEX
                    numeros_extraidos = re.findall(r'\+?\d{8,15}', datos_lead_raw)
                    if numeros_extraidos:
                        telefono_final = numeros_extraidos[0]
                        numero_encontrado_en_texto = True

                    if not es_numero_puro and not numero_encontrado_en_texto:
                        logger.warning("Lead cerrado sin número válido. Forzando...")
                        respuesta_bot = "¡Excelente! Ya registré tu nombre y correo. Solo me falta que me confirmes tu número de WhatsApp actual para que el especialista asignado pueda escribirte de inmediato."
                    else:
                        agente = asignar_agente_round_robin()
                        if agente:
                            enviar_notificaciones_telegram(agente, telefono_final, datos_lead_raw)
                            texto_cliente += f"\n\n¡Perfecto! He registrado tus datos. Nuestro asesor especializado, *{agente['nombre']}*, ha sido asignado a tu caso y te contactará de inmediato."
                            clientes_procesados.add(sender)
                        respuesta_bot = texto_cliente
                except Exception as e: logger.error(f"Error en captura: {e}")
            else:
                respuesta_bot = respuesta_bot.split("###LEAD_CAPTURED###")[0].strip()
        
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_bot})
        if len(memoria_conversaciones[sender]) > 20: memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        return {"replies": [{"message": respuesta_bot}]}
    except Exception as e:
        logger.error(f"🔴 ERROR GENERAL: {e}", exc_info=True)
        return {"replies": [{"message": "Estamos procesando tu solicitud, por favor escribe de nuevo."}]}
