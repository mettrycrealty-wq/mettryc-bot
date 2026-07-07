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

# --- LLAVE DE API NECESARIA ---
# Asegúrate de que estas variables de entorno estén configuradas en tu servidor
# export OPENROUTER_API_KEY="tu_api_key"
# export WASI_TOKEN="tu_token_wasi"
# export WASI_COMPANY_ID="tu_id_empresa_wasi"
# export GOOGLE_SHEET_TURNOS_URL="url_script_google_sheets"
# export TELEGRAM_BOT_TOKEN="tu_token_bot_telegram"
# export TELEGRAM_ADMIN_ID="tu_admin_id_telegram"
# export API_KEYS_AGENTES="llave1,llave2,llave3" # Para proteger tu webhook

# --- CONFIGURACIÓN ESTRATÉGICA DE MODELOS ---
MODELO_PRINCIPAL = "google/gemini-2.5-flash-lite"
MODELO_RESPALDO = "anthropic/claude-3.5-haiku"

# --- Sistemas de Caché y Memoria ---
cache_inventario = {
    "inventario_texto": "",
    "ultima_actualizacion": datetime.min
}

cache_sheets = {
    "agentes": [],
    "captadores": {},
    "ultimo_indice": -1, # Para Round Robin
    "ultima_actualizacion": datetime.min
}

memoria_conversaciones = {} # {sender_id: [mensaje1, mensaje2, ...]}
clientes_procesados = set() # {sender_id} para evitar duplicados

# --- FUNCIONES DE SOPORTE ---

def obtener_inventario_desde_wasi():
    """
    Descarga la lista completa de propiedades ACTIVAS desde la API de Wasi.
    Retorna un string con cada propiedad en una línea, formateada.
    """
    propiedades_limpias = []
    take = 100 # Número de propiedades por petición
    skip = 0 # Desplazamiento para paginación

    logger.info("Iniciando descarga completa de propiedades ACTIVAS desde Wasi...")

    while True:
        url = f"https://api.wasi.co/v1/property/search?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}&take={take}&skip={skip}&status=1"
        exito_pagina = False
        intentos = 0
        max_intentos = 3

        while intentos < max_intentos and not exito_pagina:
            try:
                response = requests.get(url, timeout=30) # Timeout de 30 segundos
                response.raise_for_status() # Lanza excepción si hay error HTTP (4xx o 5xx)
                data = response.json()

                contador_pagina = 0
                for key, value in data.items():
                    if isinstance(value, dict) and key.isdigit(): # Procesar solo propiedades
                        contador_pagina += 1
                        id_prop = value.get('id_property')
                        # Generar enlace web personalizado; ajustar si la URL base de tu web es diferente
                        enlace_web = f"https://www.mettryc.com/inmueble/{id_prop}"

                        # Obtener datos del asesor encargado si están disponibles
                        user_data = value.get('user_data', {})
                        asesor_encargado = f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip() or "Asesor Mettryc"

                        # Formatear la propiedad de forma amigable para el sistema
                        prop = (
                            f"- [ID: {id_prop}] {value.get('title')} | "
                            f"Ciudad: {value.get('city_label')} | Zona: {value.get('zone_label')} | "
                            f"Venta: {value.get('sale_price_label')} | Renta: {value.get('rent_price_label')} | "
                            f"Área: {value.get('area')}m2 | Hab: {value.get('bedrooms')} | Baños: {value.get('bathrooms')} | "
                            f"Enlace: {enlace_web}"
                        )
                        propiedades_limpias.append(prop)

                exito_pagina = True # Si llegamos aquí, la página fue exitosa

                # Si la cantidad de propiedades recibidas es menor a 'take', es la última página
                if contador_pagina < take:
                    logger.info(f"Descarga completa. Se encontraron {len(propiedades_limpias)} propiedades.")
                    return "\n".join(propiedades_limpias)

                skip += take # Incrementar el desplazamiento para la siguiente petición
                time.sleep(2) # Pequeña pausa entre peticiones para no saturar la API

            except requests.exceptions.RequestException as e:
                intentos += 1
                logger.warning(f"Intento {intentos}/{max_intentos} de descargar propiedades (skip={skip}): {e}")
                time.sleep(5) # Esperar más tiempo si hay un error de red

        if not exito_pagina: # Si fallaron todos los intentos para esta página
            logger.error(f"No se pudo descargar la página de propiedades en skip={skip} después de {max_intentos} intentos.")
            break # Salir del bucle while True

    logger.warning(f"Se descargaron {len(propiedades_limpias)} propiedades antes de un error.")
    return "\n".join(propiedades_limpias)

def obtener_inventario():
    """
    Verifica la caché del inventario y lo actualiza si es necesario.
    Actualiza cada 24 horas o si la caché está vacía.
    """
    if datetime.now() - cache_inventario["ultima_actualizacion"] > timedelta(hours=24) or not cache_inventario["inventario_texto"]:
        inventario_nuevo = obtener_inventario_desde_wasi()
        if inventario_nuevo: # Solo actualiza si la descarga fue exitosa
            cache_inventario["inventario_texto"] = inventario_nuevo
            cache_inventario["ultima_actualizacion"] = datetime.now()
            logger.info("Inventario actualizado en caché.")
    return cache_inventario["inventario_texto"]

def sincronizar_google_sheet():
    """
    Sincroniza los datos de agentes y captadores desde una URL de Google Sheet.
    Se actualiza cada 1 hora o si la caché está vacía.
    """
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL")
    if not script_url:
        logger.warning("La URL del script de Google Sheets (GOOGLE_SHEET_TURNOS_URL) no está configurada.")
        return

    # Actualizar caché si ha pasado más de 1 hora, o si los datos no existen
    if datetime.now() - cache_sheets["ultima_actualizacion"] > timedelta(hours=1) or not cache_sheets["agentes"]:
        try:
            logger.info("Sincronizando datos de agentes desde Google Sheets...")
            response = requests.get(script_url, timeout=15) # Timeout corto para la sincronización
            response.raise_for_status()
            payload_sheet = response.json()

            if isinstance(payload_sheet, dict):
                cache_sheets["agentes"] = payload_sheet.get("agentes", [])
                cache_sheets["captadores"] = payload_sheet.get("captadores", {})
                cache_sheets["ultima_actualizacion"] = datetime.now()
                logger.info(f"✅ Sincronizados {len(cache_sheets['agentes'])} agentes y {len(cache_sheets['captadores'])} captadores.")
            else:
                logger.error("El payload recibido de Google Sheets no es un diccionario.")

        except requests.exceptions.RequestException as e:
            logger.error(f"Error de red al sincronizar Google Sheets: {e}")
        except Exception as e:
            logger.error(f"Error inesperado al procesar Google Sheets: {e}")

def asignar_agente_round_robin():
    """
    Asigna un agente de la lista de forma rotativa (Round Robin).
    Retorna un diccionario con los datos del agente o None si no hay agentes.
    """
    sincronizar_google_sheet() # Asegúrate de tener los datos frescos
    lista_agentes = cache_sheets["agentes"]

    if not lista_agentes:
        logger.warning("No hay agentes disponibles para asignar.")
        return None

    # Calcula el siguiente índice de forma circular
    cache_sheets["ultimo_indice"] = (cache_sheets["ultimo_indice"] + 1) % len(lista_agentes)
    agente_asignado = lista_agentes[cache_sheets["ultimo_indice"]]

    # Validar que el agente tenga el campo 'nombre' y 'telegram_id'
    if agente_asignado.get('nombre') and agente_asignado.get('telegram_id'):
        return agente_asignado
    else:
        logger.warning(f"Agente asignado no tiene datos completos (nombre o telegram_id): {agente_asignado}. Reintentando.")
        return asignar_agente_round_robin() # Intenta con el siguiente hasta que sea válido

def enviar_notificaciones_telegram(agente, telefono_destino, datos_lead_raw):
    """
    Envía notificaciones a Telegram al agente asignado y al administrador.
    """
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")

    if not telegram_token:
        logger.warning("Telegram Bot Token no está configurado. No se enviarán notificaciones.")
        return

    # Formatear enlace de WhatsApp
    telefono_clean = str(telefono_destino).replace('+', '').replace(' ', '') # Limpiar número
    link_wa = f"https://wa.me/{telefono_clean}"

    # Datos para el mensaje (Nombre, Correo, Teléfono)
    # Extraer nombre y correo de los datos crudos por si acaso
    nombre_match = re.search(r'Nombre:\s*([^|]+)', datos_lead_raw)
    correo_match = re.search(r'Correo:\s*([^|]+)', datos_lead_raw)
    nombre_cliente = nombre_match.group(1).strip() if nombre_match else "No especificado"
    correo_cliente = correo_match.group(1).strip() if correo_match else "No especificado"

    mensaje_base = (
        f"\n\n*Datos del Cliente:*\n"
        f"👤 Nombre Completo: {nombre_cliente}\n"
        f"📧 Correo: {correo_cliente}\n"
        f"📲 WhatsApp: {telefono_destino}\n"
        f"🔗 Contactar: {link_wa}"
    )

    # --- Notificación al Agente Asignado ---
    agente_id = str(agente.get("telegram_id", "")).strip()
    if agente_id and agente_id != "None":
        texto_agente = f"👤 *¡Nuevo cliente asignado!*\n\n*Agente a cargo:* {agente.get('nombre')}{mensaje_base}"
        try:
            requests.post(
                f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                json={"chat_id": agente_id, "text": texto_agente, "parse_mode": "Markdown"},
                timeout=10 # Timeout más largo para Telegram
            )
            logger.info(f"Notificación enviada al agente {agente.get('nombre')} (ID: {agente_id}).")
        except Exception as e:
            logger.error(f"Error al notificar al agente {agente.get('nombre')} (ID: {agente_id}): {e}")
    else:
        logger.warning(f"El agente {agente.get('nombre')} no tiene un ID de Telegram válido configurado.")

    # --- Notificación al Administrador ---
    if admin_id:
        texto_admin = f"🤖 *REPORTE ADMIN - Nuevo Lead*\n\n*Agente a cargo:* {agente.get('nombre')}\n*ID Agente:* {agente_id}{mensaje_base}"
        try:
            requests.post(
                f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                json={"chat_id": admin_id, "text": texto_admin, "parse_mode": "Markdown"},
                timeout=10
            )
            logger.info(f"Notificación enviada al administrador (ID: {admin_id}).")
        except Exception as e:
            logger.error(f"Error al notificar al administrador (ID: {admin_id}): {e}")
    else:
        logger.warning("El ID de administrador de Telegram (TELEGRAM_ADMIN_ID) no está configurado.")


# --- FUNCIÓN DE CONSULTA A IA (CON DOBLE MODELO Y MANEJO DE ERRORES) ---
def consultar_ia(historial):
    """
    Consulta la IA usando MODELO_PRINCIPAL, y si falla, usa MODELO_RESPALDO.
    Maneja timeouts y errores generales. Retorna la respuesta o un mensaje de error.
    """
    url_ia = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}", "Content-Type": "application/json"}

    logger.info(f"Consultando IA con modelo: {MODELO_PRINCIPAL}")
    try:
        response = requests.post(url_ia, headers=headers, json={"model": MODELO_PRINCIPAL, "messages": historial}, timeout=45) # Timeout más largo para la IA
        response.raise_for_status()
        data = response.json()
        if 'choices' in data and data['choices']:
            return data['choices'][0]['message']['content']
    except requests.exceptions.Timeout:
        logger.warning("Timeout en la consulta a IA (modelo principal).")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Error de conexión o HTTP en consulta a IA (modelo principal): {e}")
    except Exception as e:
        logger.error(f"Error inesperado en consulta a IA (modelo principal): {e}")

    # --- Si el modelo principal falla, intentar con el modelo de respaldo ---
    logger.info(f"Fallo el modelo principal. Intentando con modelo de respaldo: {MODELO_RESPALDO}")
    try:
        response = requests.post(url_ia, headers=headers, json={"model": MODELO_RESPALDO, "messages": historial}, timeout=45)
        response.raise_for_status()
        data = response.json()
        if 'choices' in data and data['choices']:
            return data['choices'][0]['message']['content']
    except requests.exceptions.Timeout:
        logger.error("Timeout en la consulta a IA (modelo de respaldo).")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error de conexión o HTTP en consulta a IA (modelo de respaldo): {e}")
    except Exception as e:
        logger.error(f"Error inesperado en consulta a IA (modelo de respaldo): {e}")

    # --- Mensaje de fallback si ambos modelos fallan ---
    logger.critical("Ambos modelos de IA fallaron en responder. Retornando mensaje de fallback.")
    return "Lo siento, mi sistema está experimentando una breve pausa. ¿Podrías escribirme de nuevo en un minuto? 🙏"


# --- PROMPTS BASE Y ESPECÍFICOS ---

def obtener_prompt_base():
    inventario = obtener_inventario()
    sincronizar_google_sheet() # Asegura que el directorio esté poblado
    directorio = "\n".join([f"- {k}: WhatsApp {v}" for k, v in cache_sheets["captadores"].items()])

    return f"""
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

CASOS ESPECIALES:
▶ CASO A: MERCADOLIBRE -> Si el mensaje contiene "mercadolibre.com.ve/mlv", responde EXACTAMENTE: "¡Hola! 👋 Esta propiedad se encuentra disponible en el precio publicado. ¿Quieres agendar una visita?". Si piden más info, di que un agente les contactará.
▶ CASO B: RECLUTAMIENTO -> Para unirse envía: https://mettryc.com/blog/unete-al-mettryc-team-y-gana-desde-el-80-al-100-de-comision/18270?page=1. Curso inicial: $60, dura 5 días de 9am a 12pm.
"""

def obtener_prompt_clientes():
    return """
INSTRUCCIONES DEL FLUJO DE VENTAS PASO A PASO (PARA CLIENTE):
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
"""

def obtener_prompt_colegas():
    return """
FLUJO DE ATENCIÓN PARA COLEGAS (PARA COLEGA):
- El objetivo principal es proporcionar los datos del captador de la propiedad solicitada.
- Si un colega pregunta por una propiedad específica (con ID o título), busca en el <INVENTARIO> la propiedad.
- Responde con la ficha técnica de la propiedad solicitada, asegurándote de incluir SIEMPRE los datos del captador.

⚠️ REGLA DE FORMATO OBLIGATORIA PARA FICHA TÉCNICA DE COLEGAS: Usa un único asterisco (*) al inicio y final del título y enlaces 100% crudos (raw links). Prohibido usar doble asterisco (**), almohadillas (###) o corchetes para enlaces.

FICHA TÉCNICA PARA COLEGA:
1. *[Título de la propiedad]*
📍 Zona: [Zona o Ciudad]
💰 Precio: [Precio]
📐 Área: [M2] |  সংখ?: [Habitaciones] | 🛁 Baños: [Baños]
👤 Captación: [Nombre del captador]
👤 Apellido: [Apellido del captador]
📲 WhatsApp: [WhatsApp del captador]
🔗 Ver más: [enlace crudo]

- IMPORTANTE: NO pidas datos de contacto al colega. NO generes ninguna etiqueta de captura de lead (como ###LEAD_CAPTURED###). Simplemente entrega la información solicitada según el formato.
"""

# --- FUNCIÓN PRINCIPAL DEL WEBHOOK ---
@app.post("/webhook")
async def handle_request(request: Request):
    try:
        # --- Verificación de API Key ---
        api_keys_permitidas = os.getenv("API_KEYS_AGENTES", "").split(",")
        received_api_key = request.headers.get("x-api-key")
        if received_api_key not in api_keys_permitidas:
            logger.error(f"Acceso denegado: API Key inválida o faltante. Recibida: {received_api_key}")
            raise HTTPException(status_code=403, detail="Acceso denegado")

        # --- Procesar Payload del Mensaje ---
        data = await request.json()
        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = str(payload.get("sender", "")).strip()
        mensaje_cliente = str(payload.get("message", "")).strip()

        logger.warning(f"Solicitud recibida de {sender}: '{mensaje_cliente}'")

        if not mensaje_cliente:
            logger.warning("Mensaje vacío recibido. Ignorando.")
            return {"replies": []}

        # --- Inicializar Memoria si es la primera vez ---
        if sender not in memoria_conversaciones:
            memoria_conversaciones[sender] = []

        # --- DETECCIÓN INICIAL (CLIENTE VS COLEGA) ---
        es_colega = False
        es_cliente = False

        # Reglas simples para detectar:
        if ("soy colega" in mensaje_cliente.lower() or
            "agente inmobiliario" in mensaje_cliente.lower() or
            "inmobiliario" in mensaje_cliente.lower() and "cliente" not in mensaje_cliente.lower() or
            "asesor" in mensaje_cliente.lower() and "propiedad para mi" not in mensaje_cliente.lower() or
            "oficina" in mensaje_cliente.lower() or
            "captación" in mensaje_cliente.lower()):
            es_colega = True
        elif ("busco" in mensaje_cliente.lower() or
              "quiero" in mensaje_cliente.lower() or
              "necesito" in mensaje_cliente.lower() or
              "mi casa" in mensaje_cliente.lower() or
              "mi apartamento" in mensaje_cliente.lower() or
              "inversión" in mensaje_cliente.lower() and "agente" not in mensaje_cliente.lower() or
              "comprar" in mensaje_cliente.lower() or
              "rentar" in mensaje_cliente.lower()):
            es_cliente = True

        # Si no está claro, se hace la pregunta de clasificación
        if not es_colega and not es_cliente and len(memoria_conversaciones[sender]) < 2: # Solo preguntar si es al inicio
            # Añadir la pregunta al historial para que la IA la vea
            memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
            respuesta_pregunta = "¡Hola! 👋 ¿Buscas una propiedad para ti o eres colega inmobiliario? 😊"
            # No añadir como respuesta de la IA, sino como una pregunta directa de clasificación
            return {"replies": [{"message": respuesta_pregunta}]}

        # --- Construir el Prompt FINAL ---
        prompt_final = obtener_prompt_base() # Prompt con inventario y directorio

        if es_colega:
            prompt_final += obtener_prompt_colegas()
            logger.info(f"Modo COLEGA activado para {sender}.")
        else: # Asumir que es cliente si no es colega o si la detección inicial fue clara para cliente
            prompt_final += obtener_prompt_clientes()
            logger.info(f"Modo CLIENTE activado para {sender}.")

        # --- Preparar Historial para la IA ---
        # Importante: El historial debe incluir el mensaje actual del usuario
        historial_api = [{"role": "system", "content": prompt_final}] + memoria_conversaciones[sender] + [{"role": "user", "content": mensaje_cliente}]

        # --- LLAMADA A LA IA ---
        respuesta_bot = consultar_ia(historial_api)

        # --- POST-PROCESAMIENTO DE LA RESPUESTA DE LA IA ---
        respuesta_final_procesada = respuesta_bot # Copia para la respuesta final

        # --- MANEJO DEL DISPARADOR LEAD_CAPTURED ESPECÍFICO PARA CLIENTES ---
        if "###LEAD_CAPTURED###" in respuesta_bot and not es_colega:
            partes = respuesta_bot.split("###LEAD_CAPTURED###")
            texto_cliente = partes[0].strip()
            datos_lead_raw = partes[1].replace("###", "").strip()

            logger.info(f"Disparador LEAD_CAPTURED detectado para {sender}. Datos: {datos_lead_raw}")

            # --- VALIDACIÓN ROBUSTA DE DATOS CAPTURADOS ---
            palabras_prohibidas_o_placeholder = ["[", "]", "Su Nombre", "Su Correo", "Su WhatsApp", "Dato Real", "Numero Real", "Valor real", "Nombre Real", "Email Real", "Apellido Real", "Nombres Completos", "Email Actual", "Número de Teléfono"]
            nombre_match = re.search(r'Nombre:\s*([^|]+)', datos_lead_raw)
            correo_match = re.search(r'Correo:\s*([^|]+)', datos_lead_raw)
            telefono_match = re.search(r'Telefono:\s*([^|]+)', datos_lead_raw) # Capturar también el teléfono

            nombre_val = nombre_match.group(1).strip() if nombre_match else ""
            correo_val = correo_match.group(1).strip() if correo_match else ""
            telefono_val = telefono_match.group(1).strip() if telefono_match else ""

            nombre_completo_valido = len(nombre_val.split()) >= 2 # Al menos dos palabras para nombre completo
            correo_valido = "@" in correo_val and "." in correo_val and len(correo_val) > 5
            telefono_valido = re.match(r"^\+?\d{7,}$", telefono_val) is not None # Teléfono con mínimo 7 dígitos (con prefijo opcional)

            # --- Lógica de RE-PREGUNTA si los datos son incompletos o inválidos ---
            necesita_insistir = False
            texto_insistencia = ""

            if any(palabra in datos_lead_raw for palabra in palabras_prohibidas_o_placeholder):
                necesita_insistir = True
                texto_insistencia = "¡Excelente elección! Me entusiasma muchísimo ayudarte a encontrar tu propiedad ideal. 😍 Para poder registrar tu ficha VIP en nuestro sistema y asignarte de inmediato al asesor especialista de guardia, por favor confírmame tu Nombre Completo (Nombre y Apellido) junto con tu Correo electrónico. ¡Así procesamos tu solicitud de inmediato! 🤝"
                logger.warning(f"Placeholder o palabra prohibida detectada en LEAD_CAPTURED para {sender}.")

            elif not nombre_completo_valido:
                necesita_insistir = True
                texto_insistencia = f"¡Perfecto! Ya anoté tu interés. Por favor, confírmame también tu Apellido para poder registrar tu Nombre Completo en el sistema Mettryc y abrir tu ficha VIP con éxito. ¡Ya casi estamos listos! 😊"
                logger.warning(f"Nombre incompleto detectado en LEAD_CAPTURED para {sender}. Requiere apellido.")

            elif not correo_valido:
                necesita_insistir = True
                texto_insistencia = f"¡Excelente, {nombre_val}! Ya tengo tu nombre registrado. Por favor, compárteme tu Correo electrónico actual para completar tu ficha VIP en el sistema y que nuestro especialista de guardia te envíe toda la información detallada de inmediato. 📲"
                logger.warning(f"Correo inválido detectado en LEAD_CAPTURED para {sender}. Requiere email válido.")

            elif not telefono_valido: # Si el teléfono capturado no es válido
                necesita_insistir = True
                texto_insistencia = f"¡Genial, {nombre_val}! Tu registro está casi completo. Para que nuestro asesor pueda contactarte directamente por WhatsApp, por favor, ¿podrías confirmarme tu número de teléfono con el código de tu país? ¡Así te contactamos de inmediato! 📲"
                logger.warning(f"Teléfono inválido detectado en LEAD_CAPTURED para {sender}. Requiere teléfono válido.")

            # Si se necesita insistir, la respuesta de la IA se reemplaza
            if necesita_insistir:
                respuesta_final_procesada = texto_insistencia
                # Reconstruir el historial sin la etiqueta de captura, pero incluyendo la respuesta de insistencia
                memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_final_procesada})

            else: # ¡Datos VÁLIDOS y completos para cliente! Proceder con asignación y notificación
                logger.info(f"Datos de LEAD_CAPTURED válidos para {sender}. Iniciando proceso de asignación.")

                # Intentar obtener el número de teléfono del remitente si no vino en el LEAD
                if not telefono_valido:
                    telefono_valido_sender = re.match(r"^\+?\d{7,}$", sender) is not None
                    if telefono_valido_sender:
                        telefono_val = sender
                        logger.info(f"Usando el número del remitente {sender} como teléfono de contacto.")
                    else:
                         logger.error(f"No se pudo obtener un número de teléfono válido ni del sender ni del LEAD para {sender}.")
                         # Si aún así no hay teléfono, no debería haber generado la etiqueta. Esto es una salvaguarda.


                if telefono_valido or (telefono_valido and telefono_val != "No especificado"): # Asegurarse que TELÉFONO es válido
                    if sender not in clientes_procesados: # Evitar procesar el mismo cliente dos veces
                        agente = asignar_agente_round_robin()
                        if agente:
                            # Formatear los datos para la notificación (incluyendo el teléfono capturado)
                            datos_notificacion = f"Nombre: {nombre_val} | Correo: {correo_val} | Telefono: {telefono_val}"
                            enviar_notificaciones_telegram(agente, telefono_val, datos_notificacion)

                            # Mensaje final para el cliente confirmando la asignación
                            respuesta_final_procesada = (
                                f"{texto_cliente}\n\n"
                                f"¡Listo, {nombre_val.split()[0]}! He registrado tus datos en nuestro sistema premium. Nuestro asesor especializado, *{agente['nombre']}*, ya tiene tu caso asignado y te contactará directamente a tu WhatsApp de inmediato para darte una atención 100% personalizada. 🤝✨"
                            )
                            clientes_procesados.add(sender) # Marcar como procesado
                        else:
                            logger.warning(f"No se pudo asignar un agente para {sender}. El cliente recibe solo la confirmación de datos.")
                            # Si no hay agente (ej. lista vacía), solo se confirma que los datos fueron recibidos
                            respuesta_final_procesada = texto_cliente # Devuelve solo la parte del texto antes del `###`
                    else:
                        logger.info(f"Cliente {sender} ya fue procesado previamente. Respondiendo con información genérica.")
                        respuesta_final_procesada = texto_cliente # Cliente ya procesado
                else:
                    logger.error(f"Fallo crítico: LEAD_CAPTURED generado pero teléfono ({telefono_val}) no es válido para {sender}. Re-solicitando.")
                    respuesta_final_procesada = "¡Registro casi completo! Por favor, revisa el número de teléfono que ingresaste, ya que parece no ser válido. Para que podamos contactarte, necesitaría que lo confirmaras con el código de tu país. ¡Gracias! 😊"
        else:
            # Si la etiqueta LEAD_CAPTURED no está presente, la respuesta es directamente la de la IA
            pass # `respuesta_final_procesada` ya tiene el valor de `respuesta_bot`

        # --- GUARDAR EN MEMORIA LA INTERACCIÓN ---
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_final_procesada})

        # Limitar el tamaño de la memoria para evitar consumos excesivos
        max_memoria = 20 # Mantener las últimas 20 interacciones (10 turnos)
        if len(memoria_conversaciones[sender]) > max_memoria:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-max_memoria:]

        # --- LIMPIEZA FINAL DE FORMATO ---
        # Asegura que no se pasen dobles asteriscos (**) a WhatsApp, solo un asterisco (*)
        respuesta_limpia = respuesta_final_procesada.replace("**", "*")

        # --- RETORNAR RESPUESTA ---
        return {"replies": [{"message": respuesta_limpia}]}

    except HTTPException as e:
        logger.error(f"HTTP Exception: {e.status_code} - {e.detail}")
        raise e # Re-lanzar para que FastAPI la maneje
    except Exception as e:
        logger.error(f"Error crítico general en el webhook: {e}", exc_info=True)
        # Siempre retornar una respuesta para evitar que el sistema llamante reintente indefinidamente
        return {"replies": [{"message": "Lo siento, estamos procesando tu solicitud. Por favor, escribe de nuevo."}]}
