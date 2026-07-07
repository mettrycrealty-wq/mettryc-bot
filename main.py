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

# --- CONFIGURACIÓN ESTRATÉGICA DE MODELOS (INTACTOS) ---
MODELO_PRINCIPAL = "google/gemini-2.5-flash-lite"
MODELO_RESPALDO = "anthropic/claude-3.5-haiku" 

# --- FUNCIONES DE SOPORTE ---

def obtener_inventario_desde_wasi():
    propiedades_limpias = []
    take = 100
    skip = 0
    
    logger.info("Iniciando descarga completa de propiedades ACTIVAS desde Wasi...")
    
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
                
                if contador_pagina < take: 
                    return "\n".join(propiedades_limpias)
                
                skip += take
                time.sleep(2)
            except Exception:
                intentos += 1
                time.sleep(5)
                
        if not exito_pagina: 
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
    except Exception as e: logger.warning(f"Error principal: {e}")

    try:
        response = requests.post(url_ia, headers=headers, json={"model": MODELO_RESPALDO, "messages": historial}, timeout=30)
        data = response.json()
        if 'choices' in data: return data['choices'][0]['message']['content']
    except Exception as e: logger.error(f"Falla total: {e}")
    
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

        sender_limpio = sender.replace("+", "").replace(" ", "")
        es_numero_puro = sender_limpio.isdigit()

        # PROMPT DE ALTA CONVERSIÓN (CHAT MARKETING & HUMANIZADO)
        prompt_sistema = f"""
        Eres Paty, la especialista de atención VIP de Mettryc Realty (la primera Tecnoinmobiliaria de Venezuela, en Valencia, Carabobo, CC Patio Trigal). 
        Tu objetivo es guiar al usuario de forma súper empática y persuasiva usando Chat Marketing para lograr cierres efectivos.

        REGLAS DE PERSONALIDAD Y MARKETING CONVERSACIONAL:
        1. Eres entusiasta, ultra amigable y muy humana. Usa emojis con naturalidad y calidez.
        2. TUS RESPUESTAS DEBEN SER CORTAS (máximo 30 palabras), excepto al enviar fichas de propiedades. Rompe las ideas en frases fluidas.
        3. REGLA DE ORO CONVERSACIONAL: NUNCA hagas múltiples preguntas en un solo mensaje. Pregunta UNA SOLA COSA a la vez, genera rapport celebrando su respuesta y avanza al siguiente paso creando micro-acuerdos.
        4. JAMÁS digas que eres una inteligencia artificial o un bot. Eres Paty.

        BASE DE DATOS EN TIEMPO REAL:
        <INVENTARIO>
        {inventario}
        </INVENTARIO>
        
        <DIRECTORIO_CONFIDENCIAL>
        {directorio}
        </DIRECTORIO_CONFIDENCIAL>

        INSTRUCCIONES DEL FLUJO DE VENTAS PASO A PASO:
        - Paso 1 (Bienvenida y Tipo): Si el usuario inicia la conversación, salúdalo con inmensa alegría y energía positiva. Pregúntale con mucho interés qué TIPO de propiedad busca (casa, apartamento, etc.) y qué ZONA de su preferencia le entusiasma.
        - Paso 2 (Inversión): Valora su respuesta anterior y pregúntale amigablemente cuál es su presupuesto aproximado para filtrar las mejores opciones exclusivas.
        - Paso 3 (Requisitos clave): Pregunta por algún detalle indispensable (ej. habitaciones o baños).
        - Paso 4 (Recomendación VIP): Muestra exactamente 3 opciones del <INVENTARIO> usando ESTRICTAMENTE este formato plano de WhatsApp. 
        
        ⚠️ REGLA DE FORMATO OBLIGATORIA: Tienes TERMINANTEMENTE PROHIBIDO usar doble asterisco (**), almohadillas (###) o corchetes con paréntesis para enlaces. Usa un único asterisco (*) al inicio y final del título para ponerlo en negrita. Enlaces 100% crudos (raw links).

        1. *[Título de la propiedad]*
        📍 Zona: [Zona o Ciudad]
        💰 Precio: [Precio]
        📐 Área: [M2] | 🛏️ Habs: [Habitaciones] | 🛁 Baños: [Baños]
        🔗 Ver más: https://www.instagram.com/p/DTjPFKgDeCe/

        - Paso 5 (Cierre de Alta Conversión): Si el cliente muestra interés en una propiedad o desea agendar una visita, dile con entusiasmo que para asignarle de inmediato al asesor especialista de guardia que abrirá su ficha VIP y gestionará su caso, te confirme por favor su Nombre Completo (Nombre y Apellido) y su Correo electrónico.

        ⚠️ REGLA DE CAPTURA ESTRICTA: Detente al pedir los datos. Si el cliente solo te da su primer nombre, pídele su apellido amablemente. Si no te da el correo, insiste carismáticamente. NO generes la etiqueta final si los datos están incompletos o faltan apellidos o correos reales.

        ⚡ DISPARADOR DE ASIGNACIÓN ⚡
        Únicamente en un nuevo mensaje, cuando el cliente ya te haya facilitado su Nombre Completo (Nombre y Apellido) y Correo Electrónico REALES, añade al final de tu texto de cierre esta etiqueta exacta:
        ###LEAD_CAPTURED###Nombre: [Nombre y Apellido Real] | Correo: [Correo Real] | Telefono: [WhatsApp Real]###

        ▶ CASO A: MERCADOLIBRE -> Si el mensaje contiene "mercadolibre.com.ve/mlv", responde EXACTAMENTE: "¡Hola! 👋 Esta propiedad se encuentra disponible en el precio publicado. ¿Quieres agendar una visita?". Si piden más info, di que un agente les contactará.
        ▶ CASO B: RECLUTAMIENTO -> Para unirse envía: https://mettryc.com/blog/unete-al-mettryc-team-y-gana-desde-el-80-al-100-de-comision/18270?page=1. Curso inicial: $60, dura 5 días de 9am a 12pm.
        ▶ CASO C: COLEGAS -> Si es colega/agente, dale Nombre y WhatsApp del captador desde el <DIRECTORIO_CONFIDENCIAL>. NO PIDES DATOS AL COLEGA NI GENERAS ETIQUETA LEAD.
        """
        
        if sender not in memoria_conversaciones: memoria_conversaciones[sender] = []
        historial_api = [{"role": "system", "content": prompt_sistema}] + memoria_conversaciones[sender] + [{"role": "user", "content": mensaje_cliente}]
        
        respuesta_bot = consultar_ia(historial_api)
        
        # --- BLOQUE DE SEGURIDAD EXTREMA (CONTROL DE CALIDAD) ---
        if "###LEAD_CAPTURED###" in respuesta_bot:
            partes = respuesta_bot.split("###LEAD_CAPTURED###")
            texto_cliente = partes[0].strip()
            datos_lead_raw = partes[1].replace("###", "").strip()
            
            palabras_prohibidas = ["[", "]", "Su Nombre", "Su Correo", "Su WhatsApp", "Dato Real", "Numero Real", "Valor real", "Nombre Real", "Email Real", "Apellido Real"]
            
            # Análisis sintáctico para garantizar Nombre Completo y Correo Real
            nombre_match = re.search(r'Nombre:\s*([^|]+)', datos_lead_raw)
            correo_match = re.search(r'Correo:\s*([^|]+)', datos_lead_raw)
            
            nombre_val = nombre_match.group(1).strip() if nombre_match else ""
            correo_val = correo_match.group(1).strip() if correo_match else ""
            
            palabras_nombre = len(nombre_val.split())
            tiene_correo_valido = "@" in correo_val and "." in correo_val
            
            # Si se detectan plantillas falsas o datos incompletos, interceptamos y forzamos a Paty a insistir
            if any(palabra in datos_lead_raw for palabra in palabras_prohibidas) or palabras_nombre < 2 or not tiene_correo_valido:
                logger.warning(f"Lead Incompleto o Falso Positivo interceptado para {sender}. Nombre palabras: {palabras_nombre}, Correo válido: {tiene_correo_valido}")
                
                if palabras_nombre < 2 and not tiene_correo_valido:
                    respuesta_bot = "¡Excelente elección! Me entusiasma muchísimo ayudarte a encontrar tu propiedad ideal. 😍 Para poder registrar tu ficha VIP en nuestro sistema y asignarte de inmediato al asesor especialista de guardia, por favor confírmame tu Nombre Completo (Nombre y Apellido) junto con tu Correo electrónico. ¡Así procesamos tu solicitud de inmediato! 🤝"
                elif palabras_nombre < 2:
                    respuesta_bot = f"¡Perfecto! Ya anoté tu interés. Por favor, confírmame también tu Apellido para poder registrar tu Nombre Completo en el sistema Mettryc y abrir tu ficha VIP con éxito. ¡Ya casi estamos listos! 😊"
                elif not tiene_correo_valido:
                    respuesta_bot = f"¡Excelente, {nombre_val}! Ya tengo tu nombre registrado. Por favor, compárteme tu Correo electrónico actual para completar tu ficha VIP en el sistema y que nuestro especialista de guardia te envíe toda la información detallada de inmediato. 📲"
                else:
                    respuesta_bot = texto_cliente
                    
            elif sender not in clientes_procesados:
                nums = re.findall(r'\+?\d{8,15}', datos_lead_raw)
                telefono_final = nums[0] if nums else None
                
                if not telefono_final and not es_numero_puro:
                    logger.warning(f"Enlace de número roto evitado para {sender}")
                    respuesta_bot = texto_cliente + "\n\n¡Por último! Como no logro ver tu número de teléfono automáticamente en mi panel, por favor escríbelo por aquí (con el código de tu país) para que nuestro asesor especialista te escriba de inmediato a tu WhatsApp. 📲"
                else:
                    if not telefono_final:
                        telefono_final = sender
                        
                    agente = asignar_agente_round_robin()
                    if agente:
                        enviar_notificaciones_telegram(agente, telefono_final, datos_lead_raw)
                        texto_cliente += f"\n\n¡Listo, {nombre_val}! He registrado tus datos en nuestro sistema premium. Nuestro asesor especializado, *{agente['nombre']}*, ya tiene tu caso asignado y te contactará directamente a tu WhatsApp de inmediato para darte una atención 100% personalizada. 🤝✨"
                        clientes_procesados.add(sender)
                    respuesta_bot = texto_cliente
            else:
                respuesta_bot = texto_cliente
        
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_bot})
        
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        # LIMPIEZA GLOBAL DE FORMATO: Asegura que nunca pasen negritas dobles (**) a WhatsApp
        respuesta_bot_final = respuesta_bot.replace("**", "*")
            
        return {"replies": [{"message": respuesta_bot_final}]}
    except Exception as e:
        logger.error(f"Error crítico general: {e}", exc_info=True)
        return {"replies": [{"message": "Lo siento, estamos procesando tu solicitud. Por favor, escribe de nuevo."}]}
