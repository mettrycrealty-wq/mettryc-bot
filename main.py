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

# --- CONFIGURACIÓN ESTRATÉGICA DE MODELOS ---
# Principal: Alta capacidad (1M tokens) para procesar todo el inventario a bajo costo
MODELO_PRINCIPAL = "google/gemini-2.5-flash-lite"
# Plan B: Alta empatía y fidelidad conversacional en caso de caída del principal
MODELO_RESPALDO = "anthropic/claude-3-5-haiku" 

# --- FUNCIONES DE SOPORTE ---

def obtener_inventario_desde_wasi():
    propiedades_limpias = []
    take = 100
    skip = 0
    
    logger.info("Iniciando descarga completa de propiedades ACTIVAS desde Wasi (lotes de 100)...")
    
    while True:
        url = f"https://api.wasi.co/v1/property/search?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}&take={take}&skip={skip}&status=1"
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
                        asesor_encargado = f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip() or "Asesor Mettryc"
                        
                        prop = (
                            f"-[ID: {id_prop}] {value.get('title')} | "
                            f"Ciudad: {value.get('city_label')} | Zona: {value.get('zone_label')} | "
                            f"Venta: {value.get('sale_price_label')} | Renta: {value.get('rent_price_label')} | "
                            f"Área: {value.get('area')}m2 | Hab: {value.get('bedrooms')} | Baños: {value.get('bathrooms')} | "
                            f"Enlace: {enlace_web}"
                        )
                        propiedades_limpias.append(prop)
                
                exito_pagina = True
                logger.info(f"Descargadas {len(propiedades_limpias)} propiedades activas hasta ahora...")
                
                if contador_pagina < take: 
                    logger.info(f"Descarga finalizada. Total activas: {len(propiedades_limpias)}")
                    return "\n".join(propiedades_limpias)
                
                skip += take
                time.sleep(2)
            except Exception:
                intentos += 1
                logger.warning(f"Reintentando conexión con Wasi... (Intento {intentos})")
                time.sleep(5)
                
        if not exito_pagina: 
            logger.error("Se agotaron los reintentos de conexión con Wasi en esta página.")
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
        except Exception as e: logger.error(f"Error sincronizando Google Sheets: {e}")

def asignar_agente_round_robin():
    sincronizar_google_sheet()
    lista_agentes = sheets_cache["agentes"]
    if not lista_agentes: return None
    sheets_cache["ultimo_indice"] = (sheets_cache["ultimo_indice"] + 1) % len(lista_agentes)
    return lista_agentes[sheets_cache["ultimo_indice"]]

def enviar_notificaciones_telegram(agente, telefono_destino, datos_lead):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    agente_id = str(agente.get("telegram_id", "")).strip()
    link_wa = f"https://wa.me/{telefono_destino.replace('+', '').replace(' ', '')}"
    info_cliente = f"\n\n*Datos del Cliente:*\n{datos_lead}\n\n📲 *Contactar de inmediato:* {link_wa}"
    url_tg = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    
    if telegram_token and agente_id and agente_id != "None":
        try: requests.post(url_tg, json={"chat_id": agente_id, "text": f"👤 *¡Tienes un nuevo cliente asignado!* \n{info_cliente}", "parse_mode": "Markdown"}, timeout=5)
        except Exception as e: logger.error(f"Error notificar agente: {e}")
    if telegram_token and admin_id:
        try: requests.post(url_tg, json={"chat_id": admin_id, "text": f"👁️ *REPORTE ADMIN*\n👤 *Agente a cargo:* {agente.get('nombre')}\n{info_cliente}", "parse_mode": "Markdown"}, timeout=5)
        except Exception as e: logger.error(f"Error notificar admin: {e}")

# --- FUNCIÓN IA BLINDADA ---
def consultar_ia(historial):
    url_ia = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}", "Content-Type": "application/json"}
    
    try:
        response = requests.post(url_ia, headers=headers, json={"model": MODELO_PRINCIPAL, "messages": historial}, timeout=30)
        data = response.json()
        if 'choices' in data: return data['choices'][0]['message']['content']
        else: logger.warning(f"Respuesta sin 'choices' del modelo principal: {data}")
    except Exception as e: logger.warning(f"Error con modelo principal: {e}")

    # Fallback (Plan B) si falla el principal o da error 'choices'
    try:
        logger.info("Activando modelo de respaldo (Plan B - Claude 3.5 Haiku)...")
        response = requests.post(url_ia, headers=headers, json={"model": MODELO_RESPALDO, "messages": historial}, timeout=30)
        data = response.json()
        if 'choices' in data: return data['choices'][0]['message']['content']
    except Exception as e: logger.error(f"Falla total en IA: {e}")
    
    return "Lo siento, mi sistema está experimentando una breve pausa. ¿Podrías escribirme de nuevo en un minuto? 🙏"

# --- WEBHOOK ---
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
        directorio = "\n".join([f"- {k}: WhatsApp {v}" for k, v in sheets_cache["captadores"].items()])

        # EL PROMPT DE PATY
        prompt_sistema = f"""
        Eres Paty, la asistente virtual y especialista en atención al público de Mettryc Realty (la primera Tecnoinmobiliaria de Venezuela, ubicada en Valencia, Carabobo, CC Patio Trigal).
        Tu objetivo principal es dar una atención de primera, filtrar necesidades conversacionalmente y capturar leads o atender a colegas.

        REGLAS DE PERSONALIDAD (SÚPER HUMANIZADA):
        1. Eres útil, inteligente y MUY amigable. Usa emojis con naturalidad.
        2. TUS RESPUESTAS DEBEN SER CORTAS (máximo 30 palabras en conversaciones normales). Escribe como chatearía una persona real por WhatsApp.
        3. REGLA DE ORO CONVERSACIONAL: NUNCA hagas múltiples preguntas en un solo mensaje. Pregunta UNA SOLA COSA a la vez y espera la respuesta del usuario.
        4. JAMÁS digas que eres una inteligencia artificial o un bot. Tú eres Paty.
        5. Si te preguntan por detalles que no tienes: "No tengo esa info a la mano en este instante, pero ya mismo te asigno a uno de nuestros asesores para que te dé todos los detalles."

        BASE DE DATOS EN TIEMPO REAL:
        <INVENTARIO>
        {inventario}
        </INVENTARIO>
        
        <DIRECTORIO_CONFIDENCIAL>
        {directorio}
        </DIRECTORIO_CONFIDENCIAL>

        INSTRUCCIONES DE OPERACIÓN (ESTRICTO CUMPLIMIENTO):

        ▶ CASO A: MENSAJES DE MERCADOLIBRE
        - Si el usuario dice "mercadolibre.com.ve/mlv", responde EXACTAMENTE: "¡Hola! 👋 Esta propiedad se encuentra disponible en el precio publicado. ¿Quieres agendar una visita?". Si piden más info, diles que un agente les contactará.

        ▶ CASO B: RECLUTAMIENTO
        - Para trabajar/ser agente: envía https://mettryc.com/blog/unete-al-mettryc-team-y-gana-desde-el-80-al-100-de-comision/18270?page=1
        - Precio del curso: "Debes aprobar nuestro curso inicial que tiene un valor de $60. Dura 5 días, de 9 am a 12 pm."

        ▶ CASO C: COLEGAS INMOBILIARIOS
        - Si es "colega" o "agente", busca en el <DIRECTORIO_CONFIDENCIAL> el captador de la propiedad y dale su Nombre y WhatsApp. NO PIDES DATOS AL COLEGA.

        ▶ CASO D: CLIENTES BUSCANDO INMUEBLES (EL FLUJO PASO A PASO)
        Para que la conversación sea natural, recaba los requisitos PASO A PASO. (Un mensaje por paso):
        - Paso 1: Saluda y pregunta ÚNICAMENTE en qué zona o ciudad está buscando. (Espera su respuesta).
        - Paso 2: Cuando te diga la zona, pregúntale ÚNICAMENTE cuál es su presupuesto aproximado. (Espera su respuesta).
        - Paso 3: Cuando te diga el presupuesto, pregúntale ÚNICAMENTE si busca alguna característica especial como número de habitaciones. (Espera su respuesta).
        - Paso 4 (La Recomendación): SOLO cuando tengas esos 3 datos, busca en el <INVENTARIO> las 3 propiedades que más se ajusten. 
        
        REGLA DE FORMATO VISUAL PARA PROPIEDADES: Muestra las propiedades usando EXACTAMENTE esta plantilla. Tienes ESTRICTAMENTE PROHIBIDO usar asteriscos (*), numerales (###) o formato de enlaces ocultos. Los enlaces deben ser crudos (raw).

        1. [Título de la propiedad]
        📍 Zona: [Zona o Ciudad]
        💰 Precio: [Precio]
        📐 Área: [M2] | 🛏️ Habs: [Habitaciones] | 🛁 Baños: [Baños]
        🔗 Ver más: [URL Cruda sin corchetes ni paréntesis, ej: https://www.mettryc.com/inmueble/12345]

        - Paso 5 (Captura del Lead): Si el cliente dice que le gusta alguna opción, dile: "¡Excelente! Para que uno de nuestros asesores especializados te contacte de inmediato y abra tu ficha, confírmame por favor tu Nombre, Apellido y Correo electrónico."

        ⚡ DISPARADOR DE ASIGNACIÓN (SÚPER CRÍTICO) ⚡
        Única y exclusivamente cuando ya tengas el Nombre y Correo del cliente interesado, añadirás esta etiqueta EXACTA al final de tu mensaje:
        ###LEAD_CAPTURED###Nombre: [Su Nombre] | Correo: [Su Correo] | Telefono: [Su WhatsApp]###
        """
        if sender not in memoria_conversaciones: memoria_conversaciones[sender] = []
        historial_api = [{"role": "system", "content": prompt_sistema}] + memoria_conversaciones[sender] + [{"role": "user", "content": mensaje_cliente}]
        
        # Llamada a la IA a través de nuestra función blindada
        respuesta_bot = consultar_ia(historial_api)
        
        # Procesamiento de la captura del lead
        if "###LEAD_CAPTURED###" in respuesta_bot:
            # Seguro contra falsos positivos
            if "[Su Nombre real]" in respuesta_bot or "[Nombre]" in respuesta_bot or "[Correo]" in respuesta_bot:
                logger.warning(f"La IA intentó disparar un falso positivo para {sender}. Ignorando etiqueta.")
                respuesta_bot = respuesta_bot.split("###LEAD_CAPTURED###")[0].strip()
            elif sender not in clientes_procesados:
                try:
                    partes = respuesta_bot.split("###LEAD_CAPTURED###")
                    texto_cliente = partes[0].strip()
                    datos_lead_raw = partes[1].replace("###", "").strip()
                    
                    telefono_final = sender
                    nums = re.findall(r'\+?\d{8,15}', datos_lead_raw)
                    if nums: telefono_final = nums[0]

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
        logger.error(f"Error crítico general: {e}", exc_info=True)
        return {"replies": [{"message": "Lo siento, estamos procesando tu solicitud. Por favor, escribe de nuevo."}]}
