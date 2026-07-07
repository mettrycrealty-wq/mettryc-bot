import os
import requests
import logging
import time
import re
import json # Aseguramos la importación de json
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# --- CONFIGURACIÓN DE TIEMPO DE ACTUALIZACIÓN ---
INTERVALO_ACTUALIZACION_INVENTARIO = timedelta(hours=24)
INTERVALO_ACTUALIZACION_SHEETS = timedelta(hours=1)

# --- SISTEMAS DE CACHÉ Y MEMORIA ---
cache = {
    "inventario_completo": [], # Ahora almacena diccionarios de propiedades
    "ultima_actualizacion_inventario": datetime.min
}

sheets_cache = {
    "agentes": [],
    "captadores": {},
    "ultimo_indice_agente": -1,
    "ultima_actualizacion_sheets": datetime.min
}

memoria_conversaciones = {}
clientes_procesados = set()
# --- FIN CONFIGURACIÓN ALMACENAMIENTO ---


# --- CONFIGURACIÓN ESTRATÉGICA DE MODELOS (INTACTOS) ---
MODELO_PRINCIPAL = "google/gemini-2.5-flash-lite"
MODELO_RESPALDO = "anthropic/claude-3.5-haiku"

# --- FUNCIONES DE SOPORTE ---

def obtener_inventario_desde_wasi():
    """Descarga TODAS las propiedades activas desde Wasi.co y las retorna como lista de diccionarios."""
    propiedades = []
    take = 100
    skip = 0
    total_obtenidas = 0

    logger.info("Iniciando descarga completa de propiedades ACTIVAS desde Wasi...")

    while True:
        url = (
            f"https://api.wasi.co/v1/property/search?"
            f"wasi_token={os.getenv('WASI_TOKEN')}&"
            f"id_company={os.getenv('WASI_COMPANY_ID')}&"
            f"take={take}&skip={skip}&status=1"
        )
        exito_pagina = False
        intentos = 0

        while intentos < 3 and not exito_pagina:
            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status() # Lanza excepción para respuestas de error HTTP
                data = response.json()
                contador_pagina = 0

                for key, value in data.items():
                    if isinstance(value, dict) and key.isdigit():
                        contador_pagina += 1
                        id_prop = value.get('id_property')
                        enlace_web = f"https://www.mettryc.com/inmueble/{id_prop}"

                        user_data = value.get('user_data', {})
                        asesor_encargado = f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip() or "Asesor Mettryc"
                        telefono_asesor = user_data.get('phone', '')

                        propiedades.append({
                            "id": id_prop,
                            "titulo": value.get('title', 'Sin título'),
                            "ciudad": value.get('city_label', 'N/D'),
                            "zona": value.get('zone_label', 'N/D'),
                            "venta": value.get('sale_price_label', 'N/D'),
                            "renta": value.get('rent_price_label', 'N/D'),
                            "area": value.get('area', 'N/D'),
                            "habitaciones": value.get('bedrooms', 'N/D'),
                            "banos": value.get('bathrooms', 'N/D'),
                            "enlace": enlace_web,
                            "captador": asesor_encargado, # Usamos "captador" para ambos roles según el prompt original
                            "telefono_captador": telefono_asesor
                        })

                exito_pagina = True
                total_obtenidas += contador_pagina
                logger.info(f"Descargadas {contador_pagina} propiedades en esta página. Total acumulado: {total_obtenidas}")

                if contador_pagina < take:
                    logger.info(f"Descarga completa. Total de propiedades obtenidas: {total_obtenidas}")
                    return propiedades # Terminamos bucle si la última página no está completa

                skip += take
                time.sleep(2) # Evitar sobrecargar la API

            except requests.exceptions.RequestException as e:
                intentos += 1
                logger.warning(f"Intento {intentos}/3 de obtener inventario. Error: {e}. Reintentando en 5 segundos...")
                time.sleep(5)
            except Exception as e:
                logger.error(f"Error inesperado al procesar página del inventario: {e}", exc_info=True)
                break # Salir del bucle si hay un error no recuperable

        if not exito_pagina:
            logger.error(f"No se pudo obtener el inventario después de {intentos} intentos.")
            break # Salir si fallaron todos los intentos de una página

    return propiedades

def obtener_inventario_cache():
    """Retorna el inventario desde caché o lo actualiza si ha pasado mucho tiempo, siempre que la obtención sea exitosa."""
    # Actualizar si el caché está vacío o ha pasado mucho tiempo
    if not cache["inventario_completo"] or \
       datetime.now() - cache["ultima_actualizacion_inventario"] > INTERVALO_ACTUALIZACION_INVENTARIO:
        
        logger.info("Intentando actualizar inventario desde Wasi...")
        inventario_nuevo = obtener_inventario_desde_wasi()
        
        # Solo actualizamos caché si obtuvimos propiedades nuevas y no hubo errores graves
        if inventario_nuevo: # Si la lista no está vacía
            cache["inventario_completo"] = inventario_nuevo
            cache["ultima_actualizacion_inventario"] = datetime.now()
            logger.info(f"Inventario actualizado y almacenado en caché con {len(inventario_nuevo)} propiedades.")
        else:
            logger.warning("La obtención del inventario devolvió lista vacía o falló. Se mantiene el inventario anterior en caché (si existe).")
            # Si el caché estaba vacío y falló, aseguramos que se lance un error o mensaje
            if not cache["inventario_completo"]:
                logger.error("No se pudo obtener inventario inicial y caché está vacía. El bot no podrá mostrar propiedades.")
    else:
        logger.debug("Usando inventario desde caché.")
    return cache["inventario_completo"]

def sincronizar_google_sheet():
    """Sincroniza la lista de agentes y captadores desde la URL de Google Sheets."""
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL")
    if not script_url:
        logger.warning("La URL de Google Sheets (GOOGLE_SHEET_TURNOS_URL) no está configurada. No se sincronizarán agentes/captadores.")
        return

    # Actualizar si ha pasado el intervalo o si los datos están vacíos
    if datetime.now() - sheets_cache["ultima_actualizacion_sheets"] > INTERVALO_ACTUALIZACION_SHEETS or \
       not sheets_cache["agentes"] or not sheets_cache["captadores"]:
        
        logger.info("Intentando sincronizar Google Sheets para agentes y captadores...")
        try:
            response = requests.get(script_url, timeout=15)
            response.raise_for_status()
            payload_sheet = response.json()

            if isinstance(payload_sheet, dict):
                # Validar la estructura básica esperada
                if "agentes" in payload_sheet and isinstance(payload_sheet["agentes"], list) and \
                   "captadores" in payload_sheet and isinstance(payload_sheet["captadores"], dict):
                    
                    sheets_cache["agentes"] = payload_sheet["agentes"]
                    # Limpiar y validar captadores si es necesario
                    sheets_cache["captadores"] = {k: v for k, v in payload_sheet["captadores"].items() if isinstance(v, str)}
                    sheets_cache["ultima_actualizacion_sheets"] = datetime.now()
                    logger.info(f"✅ Sincronizados {len(sheets_cache['agentes'])} agentes y {len(sheets_cache['captadores'])} captadores desde Google Sheets.")
                else:
                    logger.warning(f"Formato inesperado en payload de Google Sheets (faltan 'agentes' o 'captadores', o no son listas/diccionarios): {payload_sheet}")
            else:
                logger.warning(f"La respuesta de Google Sheets no es un diccionario JSON válido. Contenido: {payload_sheet}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Error de red al sincronizar Google Sheets: {e}")
        except Exception as e:
            logger.error(f"Error inesperado al sincronizar Google Sheets: {e}", exc_info=True)
    else:
        logger.debug("Usando datos de agentes/captadores desde caché.")

def asignar_agente_round_robin():
    """Asigna un agente usando el método Round Robin."""
    sincronizar_google_sheet() # Asegura que los datos estén actualizados
    lista_agentes = sheets_cache["agentes"]
    if not lista_agentes:
        logger.warning("No hay agentes disponibles en caché para asignar.")
        return None

    sheets_cache["ultimo_indice_agente"] = (sheets_cache["ultimo_indice_agente"] + 1) % len(lista_agentes)
    agente_asignado = lista_agentes[sheets_cache["ultimo_indice_agente"]]
    logger.info(f"Agente asignado (Round Robin): {agente_asignado.get('nombre', 'Sin Nombre')}")
    return agente_asignado

def enviar_notificaciones_telegram(agente, telefono_destino, datos_lead_dict):
    """Envía notificaciones a Telegram para el agente asignado y el administrador."""
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    
    if not telegram_token:
        logger.warning("TELEGRAM_BOT_TOKEN no configurado. No se enviarán notificaciones a Telegram.")
        return

    # Asegurarse que el teléfono tenga formato usable para el enlace de WhatsApp
    telefono_limpio = re.sub(r'\D', '', str(telefono_destino)) # Convertir a string por si acaso
    link_wa = f"https://wa.me/{telefono_limpio}" if telefono_limpio else "#"

    # Convertir el diccionario de datos del lead a un string legible
    datos_lead_str = "\n".join([f"- **{k.capitalize()}**: {v}" for k, v in datos_lead_dict.items()])

    mensaje_agente = (
        f"👤 *¡Nuevo Cliente VIP Asignado!*\n\n"
        f"Tu cliente potencial es: **{agente.get('nombre', 'Sin nombre')}**\n\n"
        f"*A continuación, los detalles del lead:*\n{datos_lead_str}\n\n"
        f"📲 *Contacta de inmediato:* {link_wa}"
    )
    
    mensaje_admin = (
        f"👁️ *REPORTE ADMIN: Nuevo Lead Capturado*\n\n"
        f"👤 *Agente a cargo:* **{agente.get('nombre', 'Sin nombre')}**\n"
        f"*Detalles del Lead:*\n{datos_lead_str}\n"
        f"📲 *Link WhatsApp:* {link_wa}"
    )

    try:
        if agente and agente.get("telegram_id"):
            agente_id = str(agente["telegram_id"]).strip()
            if agente_id and agente_id != "None":
                requests.post(
                    f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                    json={"chat_id": agente_id, "text": mensaje_agente, "parse_mode": "Markdown"},
                    timeout=5
                )
                logger.info(f"Notificación enviada a Telegram para agente {agente_id}.")
            else:
                 logger.warning(f"Agente {agente.get('nombre')} no tiene un telegram_id válido.")
        else:
             logger.warning(f"Agente {agente.get('nombre')} no tiene telegram_id. No se notificará al agente.")

    except Exception as e:
        logger.error(f"Error al notificar al agente por Telegram: {e}")

    try:
        if admin_id:
            requests.post(
                f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                json={"chat_id": admin_id, "text": mensaje_admin, "parse_mode": "Markdown"},
                timeout=5
            )
            logger.info(f"Notificación enviada a Telegram para admin ID {admin_id}.")
    except Exception as e:
        logger.error(f"Error al notificar al admin por Telegram: {e}")

# --- FUNCIÓN IA BLINDADA ---
def consultar_ia(historial, max_tokens_respuesta=150):
    """
    Consulta a la IA, intentando con modelos principal y de respaldo.
    Limita la longitud de la respuesta en tokens.
    """
    url_ia = "https://openrouter.ai/api/v1/chat/completions"
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logger.error("OPENROUTER_API_KEY no configurado. La IA no estará disponible.")
        return "Lo siento, mi sistema de inteligencia artificial no está disponible en este momento. Por favor, inténtelo más tarde."

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    modelos = [MODELO_PRINCIPAL, MODELO_RESPALDO]
    respuesta_ia = None

    for modelo in modelos:
        try:
            logger.info(f"Consultando IA con modelo: {modelo}")
            response = requests.post(
                url_ia,
                headers=headers,
                json={
                    "model": modelo,
                    "messages": historial,
                    "max_tokens": max_tokens_respuesta # Limita las tokens de respuesta
                },
                timeout=30 # Timeout generoso para la llamada a la IA
            )
            response.raise_for_status() # Lanza excepción si hay errores HTTP
            data = response.json()

            if 'choices' in data and data['choices']:
                respuesta_ia = data['choices'][0]['message']['content']
                if respuesta_ia: # Si se obtuvo una respuesta no vacía
                    logger.info(f"Respuesta obtenida de la IA con modelo {modelo}.")
                    return respuesta_ia.strip() # Devuelve la respuesta limpia
            else:
                logger.warning(f"La IA ({modelo}) devolvió una respuesta vacía o sin 'choices'. Payload: {data}")

        except requests.exceptions.RequestException as e:
            logger.warning(f"Error de red o HTTP al consultar IA (modelo {modelo}): {e}")
        except Exception as e:
            logger.error(f"Error inesperado al procesar respuesta de IA (modelo {modelo}): {e}", exc_info=True)

    logger.error("Todos los intentos de consulta a la IA han fallado.")
    return "Lo siento, mi sistema de inteligencia artificial está temporalmente no disponible. ¿Podrías intentar de nuevo en unos minutos? 🙏"

# --- FUNCIONES DE CLASIFICACIÓN Y BÚSQUEDA ---

def detectar_rol_y_intencion(mensaje_usuario: str, historial_conversacion: list) -> dict:
    """
    Usa IA para detectar si el usuario es un cliente o un colega inmobiliario
    y cuál es su intención principal (buscar propiedad, obtener info, etc.).
    """
    # Prompt para la IA con llaves escapadas para el ejemplo JSON
    prompt_deteccion = f"""
    Eres un asistente de clasificación de usuarios para un chatbot inmobiliario.
    Analiza el siguiente mensaje y el historial de la conversación para determinar:
    1. EL ROL DEL USUARIO: 'cliente' o 'colega_inmobiliario'.
    2. LA INTENCIÓN PRINCIPAL: 'buscar_propiedad', 'pedir_ficha_completa', 'solicitar_captador', 'info_general', 'otro'.

    Prioriza la clasificación como 'colega_inmobiliario' si hay menciones de comisiones, captaciones, otros agentes, MLS, etc.
    Si la intención es buscar propiedades, intenta identificar zonas o filtros (tipo, precio, habitaciones).

    Formato de salida JSON (obligatorio y solo el JSON):
    {{"rol": "...", "intencion": "...", "filtros": {{"zona": "...", "tipo": "...", "precio_max": 100000, "habitaciones_min": 2}} }}

    Ejemplo de colega: {{"rol": "colega_inmobiliario", "intencion": "pedir_ficha_completa", "filtros": {{}}}}
    Ejemplo de cliente buscando: {{"rol": "cliente", "intencion": "buscar_propiedad", "filtros": {{"zona": "Las Mercedes", "tipo": "apartamento", "precio_max": 150000, "habitaciones_min": 3}}}}
    Ejemplo de cliente pidiendo info: {{"rol": "cliente", "intencion": "info_general", "filtros": {{}}}}
    """

    # Preparamos los mensajes para la IA
    mensajes_para_ia = [{"role": "system", "content": prompt_deteccion}]
    # Agregamos un resumen del historial reciente para contexto (últimos 4 intercambios)
    mensajes_para_ia.extend(historial_conversacion[-4:])
    mensajes_para_ia.append({"role": "user", "content": f"Analiza este mensaje: \"{mensaje_usuario}\""})

    # Llamamos a la IA, solicitando una respuesta más corta enfocada en el JSON
    respuesta_raw = consultar_ia(mensajes_para_ia, max_tokens_respuesta=250) 

    try:
        # Buscamos el bloque JSON dentro de la respuesta de la IA
        match = re.search(r'\{.*?\}', respuesta_raw, re.DOTALL)
        if match:
            respuesta_json = match.group(0)
            datos_extraccion = json.loads(respuesta_json)
            
            # Asegurar la estructura de filtros (rellenar con valores por defecto si faltan)
            filtros_raw = datos_extraccion.get("filtros", {})
            datos_extraccion["filtros"] = {
                "zona": filtros_raw.get("zona", ""),
                "tipo": filtros_raw.get("tipo", ""),
                # Asegurarse que precio_max sea número o None
                "precio_max": filtros_raw.get("precio_max") if isinstance(filtros_raw.get("precio_max"), (int, float)) else None,
                # Asegurarse que habitaciones_min sea entero o None
                "habitaciones_min": filtros_raw.get("habitaciones_min") if isinstance(filtros_raw.get("habitaciones_min"), int) else None,
            }
            
            # Validar el rol principal: si no es cliente ni colega, por defecto es cliente
            if datos_extraccion.get("rol") not in ["cliente", "colega_inmobiliario"]:
                logger.warning(f"Rol detectado inválido: '{datos_extraccion.get('rol')}'. Asumiendo 'cliente'.")
                datos_extraccion["rol"] = "cliente"
            
            # Si no hay intención detectada, por defecto es info_general
            if not datos_extraccion.get("intencion"):
                datos_extraccion["intencion"] = "info_general"

            return datos_extraccion
        else:
            logger.warning(f"No se encontró un bloque JSON válido en la respuesta de la IA de detección. Respuesta cruda: {respuesta_raw}")
            # Si no se encuentra JSON, por defecto asumimos que es un cliente con intención general.
            return {"rol": "cliente", "intencion": "info_general", "filtros": {}}

    except json.JSONDecodeError:
        logger.error(f"Error al decodificar JSON recibido de la IA (detección de rol/intención). Respuesta cruda: {respuesta_raw}")
        return {"rol": "cliente", "intencion": "info_general", "filtros": {}} # Fallback a cliente
    except Exception as e:
        logger.error(f"Error inesperado procesando la respuesta de la IA para detección de rol/intención: {e}", exc_info=True)
        return {"rol": "cliente", "intencion": "info_general", "filtros": {}} # Fallback a cliente

def formatear_ficha_propiedad(propiedad: dict, es_colega: bool = False) -> str:
    """Formatea la información de una propiedad para mostrarla a cliente o colega."""
    lineas = [
        f"*{propiedad.get('titulo', 'Propiedad sin título')}*",
        f"📍 Zona: {propiedad.get('zona', 'N/D')} | Ciudad: {propiedad.get('ciudad', 'N/D')}",
        f"💰 Venta: {propiedad.get('venta', 'N/D')} | Renta: {propiedad.get('renta', 'N/D')}",
        f"📐 Área: {propiedad.get('area', 'N/D')}m² | 🛏️ Hab: {propiedad.get('habitaciones', 'N/D')} | 🛁 Baños: {propiedad.get('banos', 'N/D')}",
        f"🔗 Ver más: {propiedad.get('enlace', '#')}"
    ]
    if es_colega:
        lineas.append(f"👤 Captador: {propiedad.get('captador', 'N/D')} | 📲 WhatsApp Captador: {propiedad.get('telefono_captador', 'N/D')}")
    
    return "\n".join(lineas)

def parsear_precio(precio_str: str) -> float:
    """Intenta convertir un string de precio a float (ej. $150,000.00 -> 150000.00). Devuelve 0.0 si es inválido o N/D."""
    if not precio_str or precio_str.lower() == 'n/d': return 0.0
    # Limpia caracteres no numéricos exceptuando el punto decimal
    precio_limpio = re.sub(r'[^\d.]', '', precio_str)
    try:
        return float(precio_limpio)
    except ValueError:
        logger.warning(f"No se pudo parsear el precio: '{precio_str}'. Retornando 0.0.")
        return 0.0 

def elegir_top_n_propiedades(propiedades_disponibles: list, intencion: dict, n: int = 3) -> list:
    """
    Filtra y ordena las propiedades basándose en la intención detectada (filtros).
    """
    filtros = intencion.get("filtros", {})
    zona_buscada = filtros.get("zona", "").lower()
    tipo_buscado = filtros.get("tipo", "").lower() # No se usa directamente en Wasi, pero puede ser útil para IA
    precio_max = filtros.get("precio_max")
    habitaciones_min = filtros.get("habitaciones_min")

    propiedades_filtradas = propiedades_disponibles

    # 1. Filtrar por Zona si está especificada
    if zona_buscada:
        propiedades_filtradas = [
            p for p in propiedades_filtradas
            if zona_buscada in p.get('zona', '').lower() or zona_buscada in p.get('ciudad', '').lower()
        ]

    # Ranking general de relevancia (basado en cantidad de datos disponibles y algunos filtros)
    def score_propiedad(p):
        score = 0
        if p.get('zona', '') != 'N/D': score += 2
        if p.get('ciudad', '') != 'N/D': score += 1
        if p.get('venta', 'N/D') != 'N/D' or p.get('renta', 'N/D') != 'N/D': score += 1
        if p.get('area', 'N/D') != 'N/D': score += 1
        if p.get('habitaciones', 'N/D') not in ['N/D', 0]: score += 2 # Contar si hay habitaciones
        if p.get('banos', 'N/D') not in ['N/D', 0]: score += 1 # Contar si hay baños
        return score

    propiedades_ordenadas_relevancia = sorted(propiedades_filtradas, key=score_propiedad, reverse=True)

    # 2. Aplicar filtros de precio y habitaciones si existen (después de ordenar por relevancia general)
    propiedades_finales = []
    
    for p in propiedades_ordenadas_relevancia:
        precio_prop_venta = parsear_precio(p.get('venta'))
        precio_prop_renta = parsear_precio(p.get('renta'))
        habitaciones_prop = p.get('habitaciones', 0) # Obtener cantidad de habitaciones, default a 0

        # Comprobar precio: debe ser menor o igual al máximo si se especificó
        precio_ok = True
        if precio_max is not None:
            # Comprobar si el precio de venta O el de renta se ajustan al presupuesto
            precio_ok = (precio_prop_venta > 0 and precio_prop_venta <= precio_max) or \
                        (precio_prop_renta > 0 and precio_prop_renta <= precio_max)

        # Comprobar habitaciones: debe ser igual o mayor al mínimo si se especificó
        habitaciones_ok = True
        if habitaciones_min is not None and isinstance(habitaciones_min, int):
            habitaciones_ok = (isinstance(habitaciones_prop, int) and habitaciones_prop >= habitaciones_min) or \
                              (habitaciones_prop == 'N/D' and habitaciones_min == 0) # Caso especial si no hay dato

        # Si cumple todos los filtros, añadir a la lista final
        if precio_ok and habitaciones_ok:
            propiedades_finales.append(p)
        
        # Cortar si ya tenemos suficientes propiedades
        if len(propiedades_finales) >= n:
            break

    return propiedades_finales[:n] # Asegurarnos de devolver máximo 'n'

# --- LLAMADA CENTRAL DEL WEBHOOK ---
@app.post("/webhook")
async def handle_request(request: Request):
    try:
        # Validar la API Key del remitente
        if request.headers.get("x-api-key") not in os.getenv("API_KEYS_AGENTES", "").split(","):
            logger.error("Acceso denegado: API Key inválida o no proporcionada.")
            raise HTTPException(status_code=403, detail="Acceso denegado. API Key inválida.")

        data = await request.json()
        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        
        sender = str(payload.get("sender", "")).strip()
        mensaje_cliente = str(payload.get("message", ""))
        
        if not mensaje_cliente.strip():
            logger.info("Mensaje vacío recibido. Ignorando.")
            return {"replies": []} # No hacer nada si el mensaje está vacío

        # --- LÓGICA PRINCIPAL DEL FLUJO ---
        
        # 1. Cargar datos necesarios (inventario y agentes/captadores)
        inventario_disponible = obtener_inventario_cache()
        if not inventario_disponible:
            logger.warning("No hay inventario disponible. El bot no podrá responder con propiedades.")
            # Responderemos de forma genérica si no hay inventario
            return {"replies": [{"message": "¡Hola! Actualmente no tengo acceso al inventario de propiedades. Por favor, inténtalo de nuevo más tarde."}]}
        
        sincronizar_google_sheet() # Asegura que sheets_cache esté actualizado
        
        # 2. Detectar Rol e Intención del usuario usando IA
        if sender not in memoria_conversaciones:
            memoria_conversaciones[sender] = []
        
        deteccion = detectar_rol_y_intencion(mensaje_cliente, memoria_conversaciones[sender])
        rol_usuario = deteccion.get("rol", "cliente") # Por defecto, asumimos cliente
        intencion_usuario = deteccion.get("intencion", "info_general")
        filtros_busqueda = deteccion.get("filtros", {})
        
        logger.info(f"Usuario {sender}: Rol='{rol_usuario}', Intención='{intencion_usuario}', Filtros={filtros_busqueda}")

        respuesta_final_chatbot = "" # Variable para almacenar la respuesta

        # --- SECCIÓN PARA CLIENTE ---
        if rol_usuario == "cliente":
            if intencion_usuario == "buscar_propiedad":
                # Buscar y seleccionar las mejores propiedades
                propiedades_seleccionadas = elegir_top_n_propiedades(inventario_disponible, deteccion, n=3)
                
                if not propiedades_seleccionadas:
                    respuesta_final_chatbot = "¡Hola! 👋 Gracias por escribirnos. Exploré nuestro inventario pero no encuentro propiedades que se ajusten exactamente a tu búsqueda ahora mismo. ¿Te gustaría intentar con otras zonas o filtros?"
                else:
                    # Formatear fichas para cliente
                    fichas_formateadas = [formatear_ficha_propiedad(p, es_colega=False) for p in propiedades_seleccionadas]
                    
                    # Construir texto de respuesta principal: Máximo 30 palabras.
                    texto_principal = "¡Hola! 👋 ¡Qué gusto atenderte! Encontré estas 3 propiedades que creo que te encantarán. ✨"
                    
                    # Unir texto principal y las fichas
                    respuesta_final_chatbot = texto_principal + "\n\n" + "\n\n".join(fichas_formateadas)
                    # Continuaremos la conversación para intentar capturar datos en el siguiente paso si el usuario responde.

            else: # Intención: info_general, saludo, etc.
                # Usamos IA para generar una respuesta corta y amigable (max 30 palabras).
                prompt_respuesta_cliente = f"""
                Eres Paty, tu asistente de atención VIP de Mettryc Realty. Responde de forma cálida y amigable, máximo 30 palabras.
                Tu objetivo es dar una respuesta útil y hacer una pregunta abierta para continuar la conversación.
                Contexto: El usuario es un CLIENTE. Su última interacción fue: "{mensaje_cliente}".
                """
                historial_ia_cliente = [{"role": "system", "content": prompt_respuesta_cliente}] + memoria_conversaciones[sender][-4:] # Añadir contexto reciente
                respuesta_final_chatbot = consultar_ia(historial_ia_cliente, max_tokens_respuesta=60) # ~30 palabras

        # --- SECCIÓN PARA COLEGA INMOBILIARIO ---
        elif rol_usuario == "colega_inmobiliario":
            # Si el colega pide ficha completa o busca propiedades
            if intencion_usuario == "pedir_ficha_completa" or intencion_usuario == "buscar_propiedad": 
                propiedades_seleccionadas = elegir_top_n_propiedades(inventario_disponible, deteccion, n=3)
                
                if not propiedades_seleccionadas:
                    respuesta_final_chatbot = "Hola colega. Revisé nuestro inventario pero hoy no encuentro propiedades que se ajusten a tu búsqueda. ¿Puedo ayudarte con algo más?"
                else:
                    # Formatear fichas para el colega (incluye datos del captador)
                    fichas_formateadas = [formatear_ficha_propiedad(p, es_colega=True) for p in propiedades_seleccionadas]
                    
                    # Responder con las fichas (sin límite de palabras para las fichas)
                    respuesta_final_chatbot = "¡Hola colega! 👋 Te comparto estas opciones de nuestro inventario que creo pueden interesarte:\n\n" + "\n\n".join(fichas_formateadas)
                    
            else: # Caso de colega con otra intención (general)
                prompt_respuesta_colega = f"""
                Eres un asistente profesional para colegas inmobiliarios de Mettryc Realty. Responde de forma cordial y eficiente, máximo 30 palabras.
                Contexto: El usuario es un COLEGA INMOBILIARIO. Su última interacción fue: "{mensaje_cliente}".
                """
                historial_ia_colega = [{"role": "system", "content": prompt_respuesta_colega}] + memoria_conversaciones[sender][-4:]
                respuesta_final_chatbot = consultar_ia(historial_ia_colega, max_tokens_respuesta=60)

        # --- LÓGICA DE CAPTURA DE LEAD Y ASIGNACIÓN (APLICA SOLO A CLIENTES INTERESADOS) ---
        # Este bloque se ejecuta si el usuario es cliente, buscó propiedades, y en su *último mensaje* parece proporcionar datos.
        # NOTA: La detección de datos aquí es básica. Un sistema más robusto requeriría un flujo de conversación dedicado.
        if rol_usuario == "cliente" and intencion_usuario == "buscar_propiedad" and sender not in clientes_procesados:
            # Intentos rudimentarios de extraer datos clave (Nombre, Correo, Teléfono)
            nombre_match = re.search(r"(?:mi nombre es|soy|me llamo)\s+([A-Za-zÀ-ÖØ-ÿ'-]+(?:\s+[A-Za-zÀ-ÖØ-ÿ'-]+)+)", mensaje_cliente, re.IGNORECASE)
            correo_match = re.search(r"([\w\.-]+@[\w\.-]+\.[\w]+)", mensaje_cliente, re.IGNORECASE)
            telefono_match = re.search(r"(?:mi? teléfono es|mi? celular es|mi? wsp es|tel:|cel:)\s*([\+?\d\s()-]+)", mensaje_cliente, re.IGNORECASE)

            datos_lead_dict = {}
            if nombre_match:
                datos_lead_dict["nombre"] = nombre_match.group(1).strip()
            if correo_match:
                datos_lead_dict["correo"] = correo_match.group(1).strip()
            if telefono_match:
                datos_lead_dict["telefono"] = telefono_match.group(1).strip()
            elif sender.isdigit() and len(sender) > 5: # Si el remitente es un número podría ser el teléfono
                 datos_lead_dict["telefono"] = sender

            # Condición para proceder con captura y asignación: REQUIERE Nombre y Correo
            if "nombre" in datos_lead_dict and "correo" in datos_lead_dict:
                
                # Validación simple de que el nombre tenga al menos Dos Palabras
                if len(datos_lead_dict["nombre"].split()) < 2:
                    logger.warning(f"Nombre incompleto de {sender}: '{datos_lead_dict['nombre']}'. Pidiendo más datos.")
                    # Si el nombre es incompleto, la respuesta del chatbot debe insistir
                    respuesta_final_chatbot = f"¡Excelente elección, {datos_lead_dict['nombre']}! Para poder registrar tu ficha VIP y asignarte el asesor especialista, por favor, confírmame también tu Apellido. 😊"
                else:
                    # Si tenemos nombre completo y correo, procedemos a asignar agente y notificar
                    agente_asignado = asignar_agente_round_robin()
                    if agente_asignado:
                        telefono_final = datos_lead_dict.get("telefono", sender) # Usar número capturado o remitente
                        
                        # Enviar notificación
                        enviar_notificaciones_telegram(agente_asignado, telefono_final, {
                            "Nombre": datos_lead_dict["nombre"],
                            "Correo": datos_lead_dict["correo"],
                            "Telefono": telefono_final, # Usamos el número ya limpio o de sender
                            "Origen": "Chatbot Inmobiliario"
                        })
                        
                        clientes_procesados.add(sender) # Marcar como cliente procesado para evitar re-asignación
                        
                        # Respuesta de confirmación al cliente
                        respuesta_final_chatbot = (
                            f"¡Perfecto, {datos_lead_dict['nombre'].split()[0]}! ✨"
                            f" He registrado tus datos VIP en nuestro sistema. "
                            f"Nuestro asesor especializado, *{agente_asignado['nombre']}*, se pondrá en contacto contigo de inmediato para brindarte atención personalizada. "
                            f"¡Gracias por confiar en Mettryc Realty! 🤝"
                        )
                    else:
                        # Si no se pudo asignar agente (ej. lista vacía)
                        respuesta_final_chatbot = "¡Genial! Hemos registrado tu interés. Estamos asignando un asesor para tu atención inmediata. Por favor, espera un momento mientras te conectamos. Gracias por tu paciencia."
            
            # Si faltan datos críticos (Nombre o Correo), y NO se ha asignado, podemos pedir más info
            elif "nombre" not in datos_lead_dict or "correo" not in datos_lead_dict:
                 if not "nombre" in datos_lead_dict:
                     respuesta_final_chatbot = "¡Me alegra que te interesen estas opciones! Para poder registrar tu ficha VIP y asignarte al asesor, por favor, confírmame tu Nombre Completo (Nombre y Apellido). 😊"
                 elif "correo" not in datos_lead_dict:
                      respuesta_final_chatbot = f"¡Excelente, {datos_lead_dict['nombre'].split()[0]}! Ya tengo tu nombre. Por favor, compárteme tu Correo Electrónico actual para completar tu ficha VIP y que nuestro especialista se comunique contigo. 📲"

        # --- FIN LÓGICA CAPTURA ---

        # --- ACTUALIZAR MEMORIA DE CONVERSACIÓN ---
        # Guardamos el último par de mensajes usuario-bot
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
        # Solo añadimos la respuesta si no estaba ya (evitar duplicados si hubo re-ejecución)
        if not memoria_conversaciones[sender] or memoria_conversaciones[sender][-1]["content"] != respuesta_final_chatbot:
            memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_final_chatbot})
        
        # Limitar el tamaño de la memoria para optimizar
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        # --- RETORNAR RESPUESTA FINAL ---
        # Una última limpieza para asegurar formato de WhatsApp (minimizar dobles asteriscos)
        respuesta_limpia = respuesta_final_chatbot.replace("**", "*")

        return {"replies": [{"message": respuesta_limpia}]}

    except HTTPException as e:
        # Si es un error esperado por HTTP (ej. 403), lo relanzamos.
        raise e
    except Exception as e:
        # Captura cualquier otro error inesperado.
        logger.error(f"Error crítico e inesperado en la ruta /webhook: {e}", exc_info=True)
        # Devolvemos un mensaje genérico de error al usuario.
        return {"replies": [{"message": "Lo siento, hemos encontrado un inconveniente técnico. Por favor, intenta escribir tu mensaje de nuevo en un momento."}]}
