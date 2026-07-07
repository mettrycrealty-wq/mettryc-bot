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

# Almacenamiento optimizado para el JSON unificado de Sheets
sheets_cache = {
    "agentes": [],
    "captadores": {},
    "ultimo_indice": -1,
    "ultima_actualizacion": datetime.min
}

memoria_conversaciones = {}
clientes_procesados = set() 
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
                response = requests.get(url, timeout=30)
                data = response.json()
                
                contador_pagina = 0
                for key, value in data.items():
                    if isinstance(value, dict) and key.isdigit():
                        contador_pagina += 1
                        id_prop = value.get('id_property')
                        enlace_web = f"https://www.mettryc.com/inmueble/{id_prop}"
                        
                        # Extracción automática del asesor encargado desde Wasi
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

# Sincronización limpia de la estructura unificada de Google Sheets
def sincronizar_google_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL") 
    if not script_url:
        logger.warning("GOOGLE_SHEET_TURNOS_URL no configurada.")
        return

    if datetime.now() - sheets_cache["ultima_actualizacion"] > timedelta(hours=1) or not sheets_cache["agentes"]:
        try:
            logger.info("Conectando con Google Apps Script para actualizar Turnos y Captadores...")
            response = requests.get(script_url, timeout=15)
            payload_sheet = response.json()
            
            if isinstance(payload_sheet, dict):
                sheets_cache["agentes"] = payload_sheet.get("agentes", [])
                sheets_cache["captadores"] = payload_sheet.get("captadores", {})
                sheets_cache["ultima_actualizacion"] = datetime.now()
                logger.info(f"✅ Sincronizados {len(sheets_cache['agentes'])} agentes de turno y {len(sheets_cache['captadores'])} teléfonos de captadores.")
        except Exception as e:
            logger.error(f"Error sincronizando Google Sheets unificado: {e}")

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
            requests.post(url_tg, json={"chat_id": agente_id, "text": f"👤 *¡Tienes un nuevo cliente asignado!* \n{info_cliente}", "parse_mode": "Markdown"}, timeout=5)
        except Exception as e: logger.error(f"Error notificar agente: {e}")
            
    if telegram_token and admin_id:
        try:
            requests.post(url_tg, json={"chat_id": admin_id, "text": f"👁️ *REPORTE DE SEGUIMIENTO ADMIN*\n👤 *Agente a cargo:* {agente['nombre']}\n{info_cliente}", "parse_mode": "Markdown"}, timeout=5)
        except Exception as e: logger.error(f"Error notificar admin: {e}")

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
        
        # CORRECCIÓN DE LA VARIABLE: Ahora se llama 'directorio'
        directorio = "\n".join([f"- {k}: WhatsApp {v}" for k, v in sheets_cache["captadores"].items()])

        prompt_sistema = f"""
        Eres Paty, la asistente virtual y especialista en atención al público de Mettryc Realty (la primera Tecnoinmobiliaria de Venezuela, ubicada en Valencia, Carabobo, CC Patio Trigal).
        Tu objetivo principal es dar una atención de primera, filtrar necesidades y capturar leads o atender a colegas.

        REGLAS DE PERSONALIDAD (SÚPER HUMANIZADA):
        1. Eres útil, inteligente y MUY amigable. Usa emojis con naturalidad, sin exagerar.
        2. TUS RESPUESTAS DEBEN SER CORTAS (Trata de no superar las 30 palabras por mensaje, a menos que estés enviando la lista de propiedades). Escribe como chatearía una persona real por WhatsApp.
        3. JAMÁS digas que eres una inteligencia artificial o un bot. Tú eres Paty.
        4. Si te preguntan por detalles muy específicos o ubicaciones exactas que no tienes, responde con naturalidad: "No tengo esa info a la mano en este instante, pero ya mismo te asigno a uno de nuestros asesores para que te dé todos los detalles."

        BASE DE DATOS EN TIEMPO REAL:
        <INVENTARIO>
        {inventario}
        </INVENTARIO>
        
        <DIRECTORIO_CONFIDENCIAL>
        {directorio}
        </DIRECTORIO_CONFIDENCIAL>

        INSTRUCCIONES DE OPERACIÓN (ESTRICTO CUMPLIMIENTO):

        ▶ CASO A: MENSAJES DE MERCADOLIBRE
        - Si el mensaje del usuario contiene "mercadolibre.com.ve/mlv", responde EXACTAMENTE esto: "¡Hola! 👋 Esta propiedad se encuentra disponible en el precio publicado. ¿Quieres agendar una visita?".
        - Tu información sobre propiedades de MercadoLibre se limita al precio. Si piden más detalles, diles que un agente se pondrá en contacto.

        ▶ CASO B: RECLUTAMIENTO (NUEVOS AGENTES)
        - Si alguien quiere trabajar, ser agente o ser parte del equipo, envíale esto: https://mettryc.com/blog/unete-al-mettryc-team-y-gana-desde-el-80-al-100-de-comision/18270?page=1
        - Si preguntan cuánto hay que pagar: "Debes aprobar nuestro curso inicial que tiene un valor de $60. Dura 5 días, de 9 am a 12 pm."
        - Si preguntan por el estatus de su solicitud: "El departamento de reclutamiento está revisando los perfiles. Voy a consultar el estatus de tu solicitud y te avisamos."

        ▶ CASO C: COLEGAS INMOBILIARIOS (MÁXIMA PRIVACIDAD)
        - Si la persona se identifica como "colega", "agente de otra inmobiliaria", etc., tu trato debe ser profesional entre pares.
        - Busca en el <DIRECTORIO_CONFIDENCIAL> quién es el captador de la propiedad que le interesa y dale directamente su Nombre y número de WhatsApp.
        - A LOS COLEGAS NUNCA SE LES PIDE DATOS NI SE GENERA LA ETIQUETA DE LEAD.

        ▶ CASO D: CLIENTES BUSCANDO INMUEBLES (EL FLUJO DE FASES)
        Si es un cliente buscando comprar o alquilar, sigue estas 3 fases en orden:
        - FASE 1 (Filtro): Pregunta qué busca exactamente. Una vez sepas, busca en el <INVENTARIO> las 3 propiedades que más se ajusten.
        - FASE 2 (Recomendación): Muestra las 3 opciones de forma ordenada (Título, Ubicación, Precio, M2, Habitaciones, y el Enlace de mettryc.com). NO PONGAS EL NOMBRE DEL CAPTADOR.
        - FASE 3 (Captura del Lead): SOLO cuando el cliente diga "Me interesa la opción 1", "Quiero visitar", o muestre intención de compra/alquiler, dile con naturalidad: "¡Excelente! Para que uno de nuestros asesores especializados te contacte de inmediato y abra tu ficha, confírmame por favor tu Nombre, Apellido y Correo electrónico." (Asume que el WhatsApp es el número desde el que escribe).

        ⚡ DISPARADOR DE ASIGNACIÓN (SÚPER CRÍTICO) ⚡
        Única y exclusivamente cuando ya tengas el Nombre y el Correo del cliente interesado, añadirás esta etiqueta EXACTA al final de tu mensaje para que el sistema asigne el asesor:
        ###LEAD_CAPTURED###Nombre: [Nombre] | Correo: [Correo] | Telefono: [Su WhatsApp]###
        """
        
        historial_api = [{"role": "system", "content": prompt_sistema}] + memoria_conversaciones[sender] + [{"role": "user", "content": mensaje_cliente}]
        
        url_ia = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}", "Content-Type": "application/json"}
        response = requests.post(url_ia, headers=headers, json={"model": MODELO_OPENROUTER, "messages": historial_api}, timeout=30)
        respuesta_bot = response.json()['choices'][0]['message']['content']
        
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
                            enviar_notificaciones_telegram(agente, telefono_final, datos_lead_raw)
                            texto_cliente += f"\n\n¡Perfecto! He registrado tus datos. Nuestro asesor especializado, *{agente['nombre']}*, ha sido asignado a tu caso y te contactará directamente a tu WhatsApp de inmediato."
                            clientes_procesados.add(sender)
                        respuesta_bot = texto_cliente
                except Exception as e: logger.error(f"Error procesando captura: {e}")
            else:
                respuesta_bot = respuesta_bot.split("###LEAD_CAPTURED###")[0].strip()
        
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_bot})
        
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        return {"replies": [{"message": respuesta_bot}]}
    except Exception as e:
        logger.error(f"Error general en el webhook: {e}")
        return {"replies": [{"message": "Estamos procesando tu solicitud, por favor escribe de nuevo."}]}
