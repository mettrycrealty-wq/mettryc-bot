import os
import requests
import logging
import time
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

# Configuración de Modelos (Principal y Respaldo)
MODELO_PRINCIPAL = "deepseek/deepseek-chat"
MODELO_RESPALDO = "google/gemini-flash-1.5" # Respaldo ultra-confiable si DeepSeek falla

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
                response = requests.get(url, timeout=30)
                data = response.json()
                
                contador_pagina = 0
                for key, value in data.items():
                    if isinstance(value, dict) and key.isdigit():
                        contador_pagina += 1
                        id_prop = value.get('id_property')
                        enlace_web = f"https://www.mettryc.com/inmueble/{id_prop}"
                        
                        user_data = value.get('user_data', {})
                        nombre_asesor = user_data.get('first_name', '')
                        apellido_asesor = user_data.get('last_name', '')
                        asesor_encargado = f"{nombre_asesor} {apellido_asesor}".strip()
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
    if not script_url:
        return

    if datetime.now() - sheets_cache["ultima_actualizacion"] > timedelta(hours=1) or not sheets_cache["agentes"]:
        try:
            response = requests.get(script_url, timeout=15)
            payload_sheet = response.json()
            if isinstance(payload_sheet, dict):
                sheets_cache["agentes"] = payload_sheet.get("agentes", [])
                sheets_cache["captadores"] = payload_sheet.get("captadores", {})
                sheets_cache["ultima_actualizacion"] = datetime.now()
                logger.info(f"✅ Sincronizados {len(sheets_cache['agentes'])} agentes de turno y {len(sheets_cache['captadores'])} captadores.")
        except Exception as e:
            logger.error(f"Error sincronizando Google Sheets: {e}")

def asignar_agente_round_robin():
    sincronizar_google_sheet()
    lista_agentes = sheets_cache["agentes"]
    if not lista_agentes:
        return None
        
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
            msg_agente = f"👤 *¡Tienes un nuevo cliente asignado!* \n{info_cliente}"
            r = requests.post(url_tg, json={"chat_id": agente_id, "text": msg_agente, "parse_mode": "Markdown"}, timeout=10)
            logger.info(f"Respuesta TG Agente ({agente['nombre']}): {r.status_code} - {r.text}")
        except Exception as e: 
            logger.error(f"🔴 Error crítico enviando Telegram al agente {agente['nombre']}: {e}")
            
    if telegram_token and admin_id:
        try:
            msg_admin = f"👁️ *REPORTE DE SEGUIMIENTO ADMIN*\n👤 *Agente a cargo:* {agente['nombre']}\n{info_cliente}"
            r = requests.post(url_tg, json={"chat_id": admin_id, "text": msg_admin, "parse_mode": "Markdown"}, timeout=10)
        except Exception as e: 
            logger.error(f"🔴 Error crítico enviando Telegram al administrador: {e}")

# NUEVA FUNCIÓN BLINDADA PARA INTERACTUAR CON OPENROUTER (CON REINTENTOS Y FALLBACK)
def consultar_openrouter_con_fallback(historial_mensajes):
    url_ia = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}", "Content-Type": "application/json"}
    
    # Intento 1 y 2 con el modelo principal (DeepSeek)
    for intento in range(2):
        try:
            logger.info(f"Intentando conectar con OpenRouter (Modelo: {MODELO_PRINCIPAL}, Intento {intento + 1})...")
            response = requests.post(url_ia, headers=headers, json={"model": MODELO_PRINCIPAL, "messages": historial_mensajes}, timeout=15)
            if response.status_code == 200:
                return response.json()['choices'][0]['message']['content']
            else:
                logger.warning(f"OpenRouter devolvió código {response.status_code}. Reintentando...")
        except Exception as e:
            logger.warning(f"Timeout o error de conexión en intento {intento + 1}: {e}")
        time.sleep(1.5)
        
    # Intento de Fallback con el modelo de respaldo (Gemini / Llama) si DeepSeek está colapsado
    try:
        logger.info(f"🚨 ACTIVANDO MODELO DE RESPALDO: Cambiando a {MODELO_RESPALDO} debido a saturación...")
        response = requests.post(url_ia, headers=headers, json={"model": MODELO_RESPALDO, "messages": historial_mensajes}, timeout=15)
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content']
    except Exception as e:
        logger.error(f"🔴 El modelo de respaldo también falló: {e}")
        
    raise Exception("Todos los proveedores de Inteligencia Artificial están fuera de servicio momentáneamente.")

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        if request.headers.get("x-api-key") not in os.getenv("API_KEYS_AGENTES", "").split(","):
            raise HTTPException(status_code=403, detail="Acceso denegado")

        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = str(payload.get("sender", "")).strip()
        mensaje_cliente = str(payload.get("message", ""))
            
        if not mensaje_cliente.strip():
            return {"replies": []}

        inventario = obtener_inventario()
        sincronizar_google_sheet()
        
        if sender not in memoria_conversaciones:
            memoria_conversaciones[sender] = []
        
        es_numero_puro = sender.replace("+", "").replace(" ", "").isdigit()
        
        if es_numero_puro:
            requisitos_lead = "su Nombre Completo y su Correo Electrónico"
            etiqueta_lead = "###LEAD_CAPTURED###Nombre: [Valor real] | Correo: [Valor real] | Interés: [Inmueble buscado]###"
        else:
            requisitos_lead = "su Nombre Completo, su Correo Electrónico y que te confirme OBLIGATORIAMENTE su Número de WhatsApp (con su código de país, ej: +58...)"
            etiqueta_lead = "###LEAD_CAPTURED###Nombre: [Valor real] | Correo: [Valor real] | Telefono: [Número Confirmado] | Interés: [Inmueble buscado]###"

        directorio_telefonic_str = "\n".join([f"- {k}: WhatsApp {v}" for k, v in sheets_cache["captadores"].items()])

        prompt_sistema = f"""
        Eres un Broker Inmobiliario experto de Mettryc Realty.
        INVENTARIO DISPONIBLE DE LA EMPRESA:
        {inventario}
        
        DIRECTORIO INTERNO DE TELÉFONOS DE CAPTADORES (CONFIDENCIAL):
        {directorio_telefonic_str}
        
        REGLAS DE ATENCIÓN:
        1. Al inicio de la conversación y durante las consultas, sé amable, muestra las opciones y responde de forma breve. Siempre proporciona el enlace crudo de la propiedad sin modificaciones. NO pidas ningún dato de entrada.
        2. Mantén un flujo de venta natural.
        
        3. REGLA DE COLEGAS / AGENTES INMOBILIARIOS EXTERNOS (MÁXIMA PRIVACIDAD): 
        En el inventario verás a un "Encargado" por propiedad. El nombre y el número telefónico de WhatsApp de ese encargado (que se encuentran en tu Directorio Interno) son INFORMACIÓN SECRETA.
        - Si detectas que el usuario que escribe se identifica explícitamente como OTRO AGENTE INMOBILIARIO o COLEGA de otra inmobiliaria, puedes y debes revelarle amigablemente el Nombre del Encargado y su número de WhatsApp directo para que puedan coordinar la operación compartida de inmediato. A los colegas NO les pidas datos de cierre ni generes etiquetas de lead captured.
        - Si es un cliente regular, NUNCA reveles el nombre del encargado y mucho menos su número de teléfono.
        
        ESTRATEGIA DE ASIGNACIÓN PARA CLIENTES REGULARES (SÚPER CRÍTICA):
        Solo cuando un CLIENTE REGULAR decida avanzar (visita o detalles específicos), pídele: {requisitos_lead}. ESPERA A QUE EL CLIENTE RESPONDA CON LOS DATOS REALES.
        
        ÚNICAMENTE CUANDO EL CLIENTE YA TE HAYA DADO SUS DATOS REALES (NO ANTES), escribe esta estructura exacta al final de tu mensaje:
        {etiqueta_lead}
        
        REGLA ANTI-ERRORES: Si el remitente es un nombre guardado, NO puedes generar la etiqueta ###LEAD_CAPTURED### hasta que el cliente escriba explícitamente su número de teléfono con dígitos en el chat.
        """
        
        historial_api = [{"role": "system", "content": prompt_sistema}] + memoria_conversaciones[sender] + [{"role": "user", "content": mensaje_cliente}]
        
        # Llamada con el nuevo sistema blindado de reintentos
        respuesta_bot = consultar_openrouter_con_fallback(historial_api)
        
        if "###LEAD_CAPTURED###" in respuesta_bot:
            if "[Valor real]" in respuesta_bot or "[Número Confirmado]" in respuesta_bot or "[Nombre]" in respuesta_bot:
                logger.warning(f"La IA intentó disparar un falso positivo para {sender}. Ignorando etiqueta.")
                respuesta_bot = respuesta_bot.split("###LEAD_CAPTURED###")[0].strip()
            
            elif sender not in clientes_procesados:
                try:
                    partes = respuesta_bot.split("###LEAD_CAPTURED###")
                    texto_cliente = partes[0].strip()
                    datos_lead_raw = partes[1].replace("###", "").strip()
                    
                    telefono_final = sender
                    numero_encontrado_en_texto = False
                    
                    if "Telefono:" in datos_lead_raw:
                        try:
                            sub_partes = datos_lead_raw.split("|")
                            for parte in sub_partes:
                                if "Telefono:" in parte:
                                    num_extraido = parte.split(":")[1].strip()
                                    num_limpio = "".join([c for c in num_extraido if c.isdigit() or c == "+"])
                                    if any(c.isdigit() for c in num_limpio):
                                        telefono_final = num_limpio
                                        numero_encontrado_en_texto = True
                        except Exception: pass

                    if not es_numero_puro and not numero_encontrado_en_texto:
                        logger.warning(f"La IA intentó cerrar el lead para '{sender}' sin recolectar el teléfono real. Forzando solicitud.")
                        respuesta_bot = "¡Excelente! Ya tengo tu nombre y correo registrados para asignarte un asesor inmobiliario. Solo me faltaría que me confirmes, por favor, tu número de WhatsApp actual (con el código de tu país) para que el especialista asignado pueda abrir tu ficha y escribirte de inmediato."
                    else:
                        agente = asignar_agente_round_robin()
                        if agente:
                            logger.info(f"Asignando lead a: {agente['nombre']} - Enviando Telegram...")
                            enviar_notificaciones_telegram(agente, telefono_final, datos_lead_raw)
                            texto_cliente += f"\n\n¡Perfecto! He registrado tus datos. Nuestro asesor especializado, *{agente['nombre']}*, ha sido asignado a tu caso y te contactará directamente a tu WhatsApp de inmediato."
                            clientes_procesados.add(sender)
                        else:
                            logger.error("🔴 No se pudo asignar agente: La lista de agentes de Google Sheet está vacía.")
                        respuesta_bot = texto_cliente
                except Exception as e: 
                    logger.error(f"🔴 Error procesando la captura del lead: {e}", exc_info=True)
            else:
                respuesta_bot = respuesta_bot.split("###LEAD_CAPTURED###")[0].strip()
        
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_bot})
        
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        return {"replies": [{"message": respuesta_bot}]}
    except Exception as e:
        logger.error(f"🔴 ERROR GENERAL EN WEBHOOK: {e}", exc_info=True)
        return {"replies": [{"message": "Estamos procesando tu solicitud, por favor escribe de nuevo."}]}
