import os
import requests
import logging
import time
import re
import json 
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
    "inventario_completo": [], # Lista de diccionarios de propiedades
    "ultima_actualizacion_inventario": datetime.min
}

sheets_cache = {
    "agentes": [], # Lista de agentes con 'nombre' y 'telegram_id'
    "captadores": {}, # Diccionario: {nombre_captador: telefono_captador}
    "ultimo_indice_agente": -1,
    "ultima_actualizacion_sheets": datetime.min
}

memoria_conversaciones = {}
clientes_procesados = set() 
# --- FIN CONFIGURACIÓN ALMACENAMIENTO ---


# --- CONFIGURACIÓN ESTRATÉGICA DE MODELOS ---
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
        url_base = f"https://api.wasi.co/v1/property/search?"
        params = {
            "wasi_token": os.getenv('WASI_TOKEN'),
            "id_company": os.getenv('WASI_COMPANY_ID'),
            "take": take,
            "skip": skip,
            "status": 1 # 1: Activo
        }
        url = url_base + "&".join([f"{k}={v}" for k, v in params.items() if v is not None])

        exito_pagina = False
        intentos = 0

        while intentos < 3 and not exito_pagina:
            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status() 
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
                            "id": id_prop, "titulo": value.get('title', 'Sin título'), "ciudad": value.get('city_label', 'N/D'),
                            "zona": value.get('zone_label', 'N/D'), "venta": value.get('sale_price_label', 'N/D'),
                            "renta": value.get('rent_price_label', 'N/D'), "area": value.get('area', 'N/D'),
                            "habitaciones": value.get('bedrooms', 'N/D'), "banos": value.get('bathrooms', 'N/D'),
                            "enlace": enlace_web, 
                            "captador_propiedad": asesor_encargado, 
                            "telefono_captador_propiedad": telefono_asesor,
                            "precio_venta_float": parsear_precio(value.get('sale_price_label')), # Parseo directo
                            "precio_renta_float": parsear_precio(value.get('rent_price_label')), # Parseo directo
                            "tipo_propiedad_wasi": value.get('type_label', 'Indefinido') # Añadir tipo de propiedad de Wasi
                        })

                exito_pagina = True
                total_obtenidas += contador_pagina
                logger.info(f"Descargadas {contador_pagina} propiedades en esta página. Total acumulado: {total_obtenidas}")

                if contador_pagina < take:
                    logger.info(f"Descarga completa: Última página tiene menos de {take} registros. Total de propiedades obtenidas: {total_obtenidas}")
                    return propiedades 

                skip += take
                time.sleep(2) 

            except requests.exceptions.RequestException as e:
                intentos += 1
                logger.warning(f"Intento {intentos}/3 de obtener inventario. Error: {e}. Reintentando en 5 seg.")
                time.sleep(5)
            except Exception as e:
                logger.error(f"Error inesperado procesando inventario de Wasi: {e}", exc_info=True)
                break 

        if not exito_pagina:
            logger.error(f"No se pudo obtener inventario de Wasi después de {intentos} intentos.")
            break 

    return propiedades

def obtener_inventario_cache():
    """Retorna inventario desde caché o lo actualiza si está obsoleto/vacío y la obtención es exitosa."""
    ahora = datetime.now()
    inventario_actual = cache["inventario_completo"]
    ultima_act = cache["ultima_actualizacion_inventario"]
    
    necesita_actualizacion = not inventario_actual or (ahora - ultima_act > INTERVALO_ACTUALIZACION_INVENTARIO)

    if necesita_actualizacion:
        logger.info("Cache de inventario obsoleta o vacía. Intentando actualizar desde Wasi...")
        inventario_nuevo = obtener_inventario_desde_wasi()
        
        if inventario_nuevo: # Solo actualizar si la obtención fue exitosa y devolvió propiedades
            cache["inventario_completo"] = inventario_nuevo
            cache["ultima_actualizacion_inventario"] = ahora
            logger.info(f"Inventario actualizado en caché con {len(inventario_nuevo)} propiedades.")
        else:
            logger.warning("La obtención del inventario de Wasi devolvió lista vacía o falló. Se mantiene el inventario anterior (si existe).")
            if not inventario_actual:
                logger.error("No se pudo obtener inventario inicial y la caché está vacía.")
    else:
        logger.debug("Usando inventario de propiedades desde caché.")
    return cache["inventario_completo"]

def sincronizar_google_sheet():
    """Sincroniza agentes y captadores desde la URL de Google Sheets SECRETA."""
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL")
    if not script_url:
        logger.warning("GOOGLE_SHEET_TURNOS_URL no configurada. No se sincronizarán datos de agentes/captadores.")
        return

    ahora = datetime.now()
    # Actualizar si ha pasado el intervalo O si los datos importantes de caché están vacíos
    si_necesita_actualizacion = (ahora - sheets_cache["ultima_actualizacion_sheets"] > INTERVALO_ACTUALIZACION_SHEETS) or \
                                (not sheets_cache["agentes"] or not sheets_cache["captadores"])
    
    if si_necesita_actualizacion:
        logger.info("Sincronizando Google Sheets para datos de agentes y captadores...")
        try:
            response = requests.get(script_url, timeout=15)
            response.raise_for_status()
            payload_sheet = response.json()

            # Validar estructura esperada del JSON
            if isinstance(payload_sheet, dict) and \
               "agentes" in payload_sheet and isinstance(payload_sheet["agentes"], list) and \
               "captadores" in payload_sheet and isinstance(payload_sheet["captadores"], dict):
                
                sheets_cache["agentes"] = payload_sheet["agentes"] # Lista de dicts con 'nombre' y 'telegram_id'
                # Limpiar captadores: asegura que sea un dict {nombre: telefono}
                sheets_cache["captadores"] = {
                    k: str(v) for k, v in payload_sheet["captadores"].items() if isinstance(v, (str, int, float)) and k and str(v).strip()
                }
                sheets_cache["ultima_actualizacion_sheets"] = ahora
                logger.info(f"✅ Sincronizados {len(sheets_cache['agentes'])} agentes y {len(sheets_cache['captadores'])} captadores desde Google Sheets.")
            else:
                logger.warning(f"Datos de Google Sheets con formato inesperado. Esperado dict con 'agentes' (list) y 'captadores' (dict). Recibido: {payload_sheet}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Error de red al sincronizar Google Sheets: {e}")
        except json.JSONDecodeError:
            logger.error(f"Error al decodificar JSON de Google Sheets.")
        except Exception as e:
            logger.error(f"Error inesperado sincronizando Google Sheets: {e}", exc_info=True)
    else:
        logger.debug("Usando datos de agentes/captadores desde caché de Sheets.")

def asignar_agente_round_robin():
    """Asigna un agente de forma cíclica (Round Robin) usando los datos de Sheets."""
    sincronizar_google_sheet() # Asegura que los datos de agentes estén actualizados
    lista_agentes = sheets_cache["agentes"]
    
    if not lista_agentes:
        logger.warning("No hay agentes disponibles en caché para asignar.")
        return None

    # Calcular el próximo índice de agente
    sheets_cache["ultimo_indice_agente"] = (sheets_cache["ultimo_indice_agente"] + 1) % len(lista_agentes)
    agente_asignado = lista_agentes[sheets_cache["ultimo_indice_agente"]]
    
    logger.info(f"Agente asignado (Round Robin): {agente_asignado.get('nombre', 'Sin Nombre')}")
    return agente_asignado

def obtener_telefono_captador_de_sheet(nombre_captador: str) -> str:
    """Busca el teléfono de un captador en la caché de Google Sheets."""
    if not sheets_cache["captadores"]: # Si la caché de captadores está vacía, intenta sincronizar
        sincronizar_google_sheet()
        
    telefono = sheets_cache["captadores"].get(nombre_captador)
    if telefono:
        logger.debug(f"Teléfono encontrado en Sheet para captador '{nombre_captador}': {telefono}")
        return telefono
    else:
        logger.warning(f"Teléfono NO encontrado en sheets_cache para captador: '{nombre_captador}'.")
        return "N/D"

def enviar_notificaciones_telegram(agente, telefono_destino, datos_lead_dict):
    """Envía notificaciones a Telegram para agente asignado y administrador."""
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    
    if not telegram_token:
        logger.warning("TELEGRAM_BOT_TOKEN no configurado. Notificaciones a Telegram deshabilitadas.")
        return

    # Limpiar y validar teléfono para enlace WhatsApp
    telefono_limpio = re.sub(r'\D', '', str(telefono_destino)) 
    link_wa = f"https://wa.me/{telefono_limpio}" if telefono_limpio and len(telefono_limpio) >= 7 else "#"

    # Construir string legible de datos del lead
    datos_lead_str_list = []
    for k, v in datos_lead_dict.items():
        if v: # Incluir solo si tiene valor
            datos_lead_str_list.append(f"- **{k.capitalize()}**: {v}")
    datos_lead_str = "\n".join(datos_lead_str_list) or "No hay detalles adicionales."

    # Mensajes de Telegram
    mensaje_agente = f"👤 *¡Nuevo Cliente VIP Asignado!*\n\nTu cliente potencial es: **{agente.get('nombre', 'Sin nombre')}**\n\n*Detalles del Lead:*\n{datos_lead_str}\n\n📲 *Contacta de inmediato (WhatsApp):* {link_wa}"
    mensaje_admin = f"👁️ *REPORTE ADMIN: Nuevo Lead Capturado*\n\n👤 *Agente a cargo:* **{agente.get('nombre', 'Sin nombre')}**\n*Detalles del Lead:*\n{datos_lead_str}\n📲 *Link WhatsApp:* {link_wa}"

    # Enviar al agente
    if agente and agente.get("telegram_id"):
        agente_id = str(agente["telegram_id"]).strip()
        if agente_id and agente_id != "None" and agente_id.lstrip('-').isdigit(): # Validación básica de ID válido
            try:
                requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", json={"chat_id": agente_id, "text": mensaje_agente, "parse_mode": "Markdown"}, timeout=5)
                logger.info(f"Notificación a Telegram enviada para agente ID: {agente_id}.")
            except Exception as e:
                logger.error(f"Error notificando a Telegram (Agente ID {agente_id}): {e}")
        else:
             logger.warning(f"Agente '{agente.get('nombre')}' tiene telegram_id inválido ({agente.get('telegram_id')}).")
    else:
         logger.warning(f"Agente '{agente.get('nombre')}' no tiene telegram_id configurado.")

    # Enviar al administrador
    if admin_id and admin_id.lstrip('-').isdigit(): # Validación básica de ID válido
        try:
            requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", json={"chat_id": admin_id, "text": mensaje_admin, "parse_mode": "Markdown"}, timeout=5)
            logger.info(f"Notificación a Telegram enviada al administrador ID: {admin_id}.")
        except Exception as e:
            logger.error(f"Error notificando a Telegram (Admin ID {admin_id}): {e}")
    else:
        logger.warning("TELEGRAM_ADMIN_ID no configurado o inválido.")

# --- FUNCIÓN IA CENTRALIZADA Y BLINDADA ---
def consultar_ia(historial: list, max_tokens_respuesta: int = 150) -> str:
    """
    Consulta IA, prueba modelos principal y de respaldo. Maneja errores y limita tokens de respuesta.
    """
    url_ia = "https://openrouter.ai/api/v1/chat/completions"
    api_key = os.getenv("OPENROUTER_API_KEY")
    
    if not api_key:
        logger.error("Acceso a IA deshabilitado: OPENROUTER_API_KEY no configurado.")
        return "Lo siento, mi sistema de IA no está disponible. Inténtalo más tarde."

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    modelos_a_probar = [MODELO_PRINCIPAL, MODELO_RESPALDO]

    for modelo in modelos_a_probar:
        try:
            logger.info(f"Intentando consulta a IA con modelo: {modelo}")
            response = requests.post(
                url_ia,
                headers=headers,
                json={"model": modelo, "messages": historial, "max_tokens": max_tokens_respuesta},
                timeout=30
            )
            response.raise_for_status() 
            data = response.json()

            if 'choices' in data and data['choices']:
                respuesta_ia = data['choices'][0]['message']['content']
                if respuesta_ia and respuesta_ia.strip(): 
                    logger.info(f"Respuesta exitosa de IA con modelo '{modelo}'.")
                    return respuesta_ia.strip()
                else:
                    logger.warning(f"IA ({modelo}) devolvió respuesta vacía o solo espacios.")
            else:
                logger.warning(f"IA ({modelo}) devolvió structure inesperada o sin 'choices'. Payload: {data}")

        except requests.exceptions.RequestException as e:
            logger.warning(f"Error de red/HTTP al consultar IA ({modelo}): {e}")
        except json.JSONDecodeError:
            logger.error(f"Error al decodificar JSON de IA ({modelo}). Payload: {response.text}")
        except Exception as e:
            logger.error(f"Error inesperado procesando respuesta IA ({modelo}): {e}", exc_info=True)

    logger.error("Todos los intentos de consulta a IA han fallado.")
    return "Lo siento, mi sistema de IA está temporalmente fuera de servicio. Intenta de nuevo en unos minutos. 🙏"

# --- FUNCIONES DE CLASIFICACIÓN Y DETECCIÓN ---

def validar_filtro_numerico(valor, tipo_esperado=int):
    """Valida si un valor es numérico (int/float) o None."""
    if valor is None:
        return None
    try:
        if tipo_esperado == int: return int(valor)
        elif tipo_esperado == float: return float(valor)
    except (ValueError, TypeError): return None
    return None

def detectar_rol_y_intencion(mensaje_usuario: str, historial_conversacion: list) -> dict:
    """
    Usa IA para clasificar rol, intención y extraer filtros de búsqueda.
    Prioriza la detección de 'colega_inmobiliario' y si NO SE DETECTA, por defecto es 'cliente' con 'info_general',
    pero si hay indicios de que el cliente busca propiedad, se marca como tal.
    El prompt exige EXCLUSIVAMENTE JSON de respuesta pura.
    """
    prompt_deteccion = f"""
    Eres un clasificador de usuarios para chatbot inmobiliario. Analiza el mensaje Y el historial de conversación para determinar:
    1. ROL: DEBE SER 'cliente' O 'colega_inmobiliario'. Si no hay pistas claras para 'colega_inmobiliario', ASUME 'cliente'.
    2. INTENCIÓN: DEBE SER 'buscar_propiedad', 'pedir_ficha_completa', 'solicitar_captador', 'info_general', o 'otro'. 
       - Usa 'buscar_propiedad' SI HAY INDICIO CLARO DE FILTROS ESPECÍFICOS (zona, tipo, operación, precio, habitaciones). Si no hay, usa 'info_general'.
       - Usa 'pedir_ficha_completa' si solicita detalles de una propiedad específica (ej. "dame más info de la propiedad X").
       - Usa 'info_general' para saludos, consultas básicas, o si la intención es ambigua y NO HAY FILTROS ESPECÍFICOS.
    3. FILTROS (solo si la INTENCIÓN es 'buscar_propiedad'): dict con 'zona' (str), 'tipo_propiedad' (str, ej. 'casa', 'apartamento', 'local'), 'tipo_operacion' (str, 'venta' o 'alquiler'), 'precio_max' (numérico float, usa null si no viable), 'habitaciones_min' (numérico int, usa null si no viable).
       - Extrae el tipo de operación (venta/alquiler) explícitamente. Si no se menciona, asume 'venta'.
       - Usa valores NUMÉRICOS REALISTAS (ej. 100000.00 para precios; 2, 3 para habitaciones). Si un filtro es inválido (ej. precio 500 para casa), usa null.

    Critérios clave para 'colega_inmobiliario': mención de comisiones, captaciones, MLS, términos técnicos inmobiliarios, o referencia a mí como colega/agente.

    DEBES RESPONDER EXCLUSIVAMENTE CON UN OBJETO JSON VÁLIDO. SIN TEXTO ADICIONAL, NI MARCADORES DE CÓDIGO COMO ```json.

    JSON de ejemplo A (Colega buscando): {{"rol": "colega_inmobiliario", "intencion": "buscar_propiedad", "filtros": {{"zona": "Altamira", "tipo_propiedad": "casa", "tipo_operacion": "venta", "precio_max": 300000.00, "habitaciones_min": 4}} }}
    JSON de ejemplo B (Colega info): {{"rol": "colega_inmobiliario", "intencion": "info_general", "filtros": {{}}}}
    JSON de ejemplo C (Cliente buscando): {{"rol": "cliente", "intencion": "buscar_propiedad", "filtros": {{"zona": "Las Mercedes", "tipo_propiedad": "apartamento", "tipo_operacion": "venta", "precio_max": 150000.00, "habitaciones_min": 3}} }}
    JSON de ejemplo D (Cliente saludo): {{"rol": "cliente", "intencion": "info_general", "filtros": {{}}}}
    JSON de ejemplo E (Cliente buscando pero sin filtros claros): {{"rol": "cliente", "intencion": "info_general", "filtros": {{}}}} # Si no hay filtros, se considera info_general.
    JSON de ejemplo F (Cliente buscando con alquiler): {{"rol": "cliente", "intencion": "buscar_propiedad", "filtros": {{"zona": "El Bosque", "tipo_propiedad": "apartamento", "tipo_operacion": "alquiler", "precio_max": 500.00, "habitaciones_min": 1}} }}
    JSON de ejemplo G (Cliente buscando con precio bajo inválido): {{"rol": "cliente", "intencion": "buscar_propiedad", "filtros": {{"zona": "Downtown", "tipo_propiedad": "apartamento", "tipo_operacion": "venta", "precio_max": null, "habitaciones_min": 1}} }} # Precio 500 es inválido para venta, se vuelve null.
    """

    mensajes_para_ia = [{"role": "system", "content": prompt_deteccion}]
    mensajes_para_ia.extend(historial_conversacion[-4:]) # Contexto reciente
    mensajes_para_ia.append({"role": "user", "content": f"Analiza este mensaje: \"{mensaje_usuario}\""})

    respuesta_raw = consultar_ia(mensajes_para_ia, max_tokens_respuesta=300) # Aumentamos tokens para más info

    try:
        # Intenta extraer JSON de forma robusta
        start_index = respuesta_raw.find('{')
        end_index = respuesta_raw.rfind('}')
        
        if start_index != -1 and end_index != -1 and start_index < end_index:
            respuesta_json_str = respuesta_raw[start_index : end_index + 1]
            datos_extraccion = json.loads(respuesta_json_str)
            
            # --> **Validación y Limpieza de Datos Extraídos** <--
            rol_detectado = datos_extraccion.get("rol")
            intencion_detectada = datos_extraccion.get("intencion")
            filtros_raw = datos_extraccion.get("filtros", {})

            # Limpiar y validar filtros
            # Aseguramos que tipo_propiedad y tipo_operacion no sean vacíos si la intención es buscar_propiedad
            tipo_prop_val = filtros_raw.get("tipo_propiedad", "")
            zona_val = filtros_raw.get("zona", "")
            tipo_op_val = filtros_raw.get("tipo_operacion", "").lower()
            # Si la intención es buscar_propiedad, pero no se especifica operación, asumimos 'venta'
            if intencion_detectada == 'buscar_propiedad' and not tipo_op_val:
                tipo_op_val = 'venta'

            # Si la intención es buscar_propiedad y no tenemos zona, tipo o operación claros,
            # podemos considerarlo como info_general para forzar al bot a indagar.
            if intencion_detectada == 'buscar_propiedad' and not (zona_val or tipo_prop_val or tipo_op_val):
                intencion_final = 'info_general' # Forzar a indagar si no hay filtros claros
                logger.warning("Intención 'buscar_propiedad' detectada pero sin filtros claros. Cambiando a 'info_general'.")
            else:
                intencion_final = intencion_detectada # Usamos la intención detectada si hay algo claro

            filtros_limpios = {
                "zona": zona_val if zona_val else "",
                "tipo_propiedad": tipo_prop_val if tipo_prop_val else "",
                "tipo_operacion": tipo_op_val if tipo_op_val in ['venta', 'alquiler'] else 'venta', # Default a venta si no es claro
                "precio_max": validar_filtro_numerico(filtros_raw.get("precio_max"), float),
                "habitaciones_min": validar_filtro_numerico(filtros_raw.get("habitaciones_min"), int),
            }
            # Anular valores numéricos inválidos o no realistas
            if filtros_limpios["precio_max"] is not None and filtros_limpios["precio_max"] <= 0: filtros_limpios["precio_max"] = None
            if filtros_limpios["habitaciones_min"] is not None and filtros_limpios["habitaciones_min"] < 0: filtros_limpios["habitaciones_min"] = None
                
            # Validar rol: Prioriza 'colega_inmobiliario', si no, por defecto 'cliente'
            rol_final = "cliente" # Default más seguro
            if rol_detectado == "colega_inmobiliario":
                rol_final = "colega_inmobiliario"
            elif rol_detectado != "cliente": # Si no es 'colega' y tampoco es 'cliente' explícito
                logger.warning(f"Rol detectado '{rol_detectado}' no válido. Asumiendo rol por defecto: '{rol_final}'.")

            # Validar intención: Si sigue siendo 'otro', se asume 'info_general'
            if intencion_final == 'otro': intencion_final = "info_general" 
            
            return {"rol": rol_final, "intencion": intencion_final, "filtros": filtros_limpios}
        else:
            logger.error(f"No se pudo extraer un objeto JSON de la respuesta de la IA. Respuesta cruda: {respuesta_raw}")
            # Fallback: Si no se puede extraer JSON, ASUME 'cliente' con intención 'info_general'.
            return {"rol": "cliente", "intencion": "info_general", "filtros": {}}

    except json.JSONDecodeError:
        logger.error(f"Error de decodificación JSON de la IA. Respuesta cruda: {respuesta_raw}")
        # Fallback: Si falla JSON, ASUME 'cliente' con 'info_general'.
        return {"rol": "cliente", "intencion": "info_general", "filtros": {}} 
    except Exception as e:
        logger.error(f"Error inesperado procesando respuesta IA: {e}", exc_info=True)
        # Fallback general en caso de cualquier otro error.
        return {"rol": "cliente", "intencion": "info_general", "filtros": {}}

def formatear_ficha_propiedad(propiedad: dict, es_colega: bool = False) -> str:
    """Formatea la información de una propiedad para cliente o colega. Solo muestra el precio correspondiente a la operación."""
    lineas = [
        f"*{propiedad.get('titulo', 'Propiedad sin título')}*",
        f"📍 Zona: {propiedad.get('zona', 'N/D')} | Ciudad: {propiedad.get('ciudad', 'N/D')}",
    ]
    
    # Determinar qué precio mostrar basándose en la operación (deteccion['filtros']['tipo_operacion'])
    # Asumimos que 'venta' es el default si no se especifica.
    operacion_buscada = propiedad.get('operacion_buscada', 'venta') # Debe venir de la detección
    
    precio_str = "N/D"
    if operacion_buscada == 'venta' and propiedad.get('venta', 'N/D') != 'N/D':
        precio_str = f"💰 Venta: {propiedad.get('venta')}"
    elif operacion_buscada == 'alquiler' and propiedad.get('renta', 'N/D') != 'N/D':
        precio_str = f"💰 Renta: {propiedad.get('renta')}"
    
    if precio_str != "N/D":
        lineas.append(precio_str)
        
    lineas.append(f"📐 Área: {propiedad.get('area', 'N/D')}m² | 🛏️ Hab: {propiedad.get('habitaciones', 'N/D')} | 🛁 Baños: {propiedad.get('banos', 'N/D')}")
    lineas.append(f"🔗 Ver más: {propiedad.get('enlace', '#')}")

    if es_colega: # Solo para colega, añade datos del captador de la Sheet
        captador_nombre_prop = propiedad.get('captador_propiedad', 'N/D') 
        captador_tel_sheet = "N/D"
        if captador_nombre_prop != 'N/D':
            if not sheets_cache["captadores"]: sincronizar_google_sheet()
            captador_tel_sheet = sheets_cache["captadores"].get(captador_nombre_prop, "N/D")
        
        if captador_nombre_prop != 'N/D' or (captador_tel_sheet != 'N/D' and captador_tel_sheet):
            lineas.append(f"👤 Captador: {captador_nombre_prop} | 📲 WhatsApp Captador: {captador_tel_sheet}")
    
    return "\n".join(lineas)

def parsear_precio(precio_str: str) -> float:
    """Convierte string de precio a float. Devuelve 0.0 si es inválido o 'N/D'."""
    if not precio_str or precio_str.lower() == 'n/d': return 0.0
    precio_limpio = re.sub(r'[^\d.]', '', str(precio_str)) 
    try:
        valor_float = float(precio_limpio)
        if valor_float > 0 and valor_float < 1000 and precio_str.strip() != '0.0': # Advertencia para precios bajos sospechosos
            logger.warning(f"Precio parseado muy bajo ({valor_float} de '{precio_str}'). Puede ser inválido.")
        return valor_float
    except ValueError:
        logger.warning(f"Error al parsear precio '{precio_str}'. Retornando 0.0.")
        return 0.0

def elegir_top_n_propiedades(propiedades_disponibles: list, intencion_detectada: dict, n: int = 3) -> list:
    """
    Filtra y selecciona las 'n' propiedades más relevantes según los criterios detectados en la intención de búsqueda.
    Ahora SI FILTRA por tipo de propiedad, tipo de operación, zona, precio y habitaciones.
    """
    filtros = intencion_detectada.get("filtros", {})
    zona_buscada = filtros.get("zona", "").lower()
    tipo_prop_buscado = filtros.get("tipo_propiedad", "").lower() # Ej: 'apartamento', 'casa'
    tipo_op_buscado = filtros.get("tipo_operacion", "venta").lower() # Default a 'venta'
    precio_max_buscado = filtros.get("precio_max")
    habitaciones_min_buscado = filtros.get("habitaciones_min")

    propiedades_filtradas = propiedades_disponibles

    # 1. Filtrar por Tipo de Operación (Venta/Alquiler)
    propiedades_filtradas = [
        p for p in propiedades_filtradas
        if (tipo_op_buscado == 'venta' and parsear_precio(p.get('venta')) > 0) or \
           (tipo_op_buscado == 'alquiler' and parsear_precio(p.get('renta')) > 0)
    ]
    
    # 2. Filtrar por Tipo de Propiedad (si se especificó)
    if tipo_prop_buscado:
        propiedades_filtradas = [
            p for p in propiedades_filtradas
            # Busca si el tipo buscado está contenido en el tipo de propiedad de Wasi
            if tipo_prop_buscado in p.get('tipo_propiedad_wasi', '').lower() 
        ]

    # 3. Filtro por Zona (si está especificado)
    if zona_buscada:
        propiedades_filtradas = [
            p for p in propiedades_filtradas
            if zona_buscada in p.get('zona', '').lower() or zona_buscada in p.get('ciudad', '').lower()
        ]

    # Función de scoring para relevancia general (más datos = mayor score)
    def score_propiedad(p):
        score = 0
        if p.get('zona', '') != 'N/D': score += 2
        if p.get('ciudad', '') != 'N/D': score += 1
        # Evaluar precio según operación para scoring
        precio_evaluado = 0
        if tipo_op_buscado == 'venta': precio_evaluado = p.get('precio_venta_float', 0)
        elif tipo_op_buscado == 'alquiler': precio_evaluado = p.get('precio_renta_float', 0)
        if precio_evaluado > 0: score += 1
        
        if p.get('area', 'N/D') != 'N/D': score += 1
        # Contar habitaciones/baños si son números válidos
        hab_valido = False
        try: 
            if p.get('habitaciones') not in ['N/D', ''] and int(p.get('habitaciones')) > 0: hab_valido = True
        except (ValueError, TypeError): pass
        if hab_valido: score += 2
        
        banos_valido = False
        try: 
            if p.get('banos') not in ['N/D', ''] and int(p.get('banos')) > 0: banos_valido = True
        except (ValueError, TypeError): pass
        if banos_valido: score += 1
        
        return score

    # Ordenar propiedades por relevancia general
    propiedades_ordenadas_relevancia = sorted(propiedades_filtradas, key=score_propiedad, reverse=True)

    # 4. Aplicar filtros de Precio y Habitaciones (después de ordenar por relevancia)
    propiedades_finales_seleccionadas = []
    
    for p in propiedades_ordenadas_relevancia:
        # Usar precios parseados directamente desde Wasi
        precio_venta_prop = p.get('precio_venta_float', 0)
        precio_renta_prop = p.get('precio_renta_float', 0)
        habitaciones_prop_int = 0 # Default
        try:
            if p.get('habitaciones') not in ['N/D', ''] : habitaciones_prop_int = int(p.get('habitaciones'))
        except (ValueError, TypeError): pass
            
        # Evaluar filtro de precio Máximo según la operación buscada
        precio_cumple = True
        if precio_max_buscado is not None:
            if tipo_op_buscado == 'venta':
                precio_cumple = (precio_venta_prop > 0 and precio_venta_prop <= precio_max_buscado)
            elif tipo_op_buscado == 'alquiler':
                precio_cumple = (precio_renta_prop > 0 and precio_renta_prop <= precio_max_buscado)

        # Evaluar filtro de habitaciones Mínimo
        habitaciones_cumplen = True
        if habitaciones_min_buscado is not None:
            habitaciones_cumplen = habitaciones_prop_int >= habitaciones_min_buscado

        # Añadir si cumple todos los filtros aplicados
        if precio_cumple and habitaciones_cumplen:
            # Guardar la operacion buscada para usarla en formatear_ficha_propiedad si es necesario
            p['operacion_buscada'] = tipo_op_buscado
            propiedades_finales_seleccionadas.append(p)
        
        # Detener si ya hemos encontrado suficientes propiedades para el top N
        if len(propiedades_finales_seleccionadas) >= n: 
            break

    # Añadir la operación buscada a cada propiedad seleccionada para formateo posterior
    for prop in propiedades_finales_seleccionadas:
        prop['operacion_buscada'] = tipo_op_buscado

    return propiedades_finales_seleccionadas[:n]

# --- LLAMADA CENTRAL DEL WEBHOOK ---
@app.post("/webhook")
async def handle_request(request: Request):
    try:
        # 1. Validar Clave API
        api_key_header = request.headers.get("x-api-key")
        valid_api_keys = os.getenv("API_KEYS_AGENTES", "").split(",")
        if api_key_header not in valid_api_keys or not api_key_header:
            logger.error(f"Acceso denegado: API Key '{api_key_header}' inválida.")
            raise HTTPException(status_code=403, detail="Acceso denegado.")

        # 2. Parsear payload
        data = await request.json()
        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        
        sender = str(payload.get("sender", "")).strip()
        mensaje_usuario_raw = str(payload.get("message", "")).strip()
        
        if not mensaje_usuario_raw:
            logger.info("Mensaje vacío recibido. Ignorando.")
            return {"replies": []}

        # --- PROCESAMIENTO PRINCIPAL ---
        
        # 3. Cargar/Actualizar datos maestros (inventario y agentes/captadores)
        inventario_disponible = obtener_inventario_cache()
        if not inventario_disponible:
            logger.warning("Inventario de propiedades no disponible. Consultas de búsqueda fallarán.")
            return {"replies": [{"message": "¡Hola! Estamos actualizando nuestro catálogo. Por favor, inténtalo de nuevo más tarde."}]}
        
        sincronizar_google_sheet() # Asegurar que sheets_cache tenga los datos más recientes
        
        # 4. Detectar rol, intención y filtros usando IA
        if sender not in memoria_conversaciones:
            memoria_conversaciones[sender] = []
        
        # LLAMADA A IA PARA DETECCIÓN DE ROL E INTENCIÓN
        deteccion = detectar_rol_y_intencion(mensaje_usuario_raw, memoria_conversaciones[sender])
        rol_usuario = deteccion.get("rol", "cliente") 
        intencion_usuario = deteccion.get("intencion", "info_general") 
        filtros_busqueda = deteccion.get("filtros", {})
        
        logger.info(f"Usuario {sender}: Rol='{rol_usuario}', Intención='{intencion_usuario}', Filtros={filtros_busqueda}")

        respuesta_final_chatbot = "" # Variable para construir la respuesta

        # --- Lógica de Respuesta según Rol y Flujo de Conversación ---
        
        # 1. TRATAMIENTO CUALIFICADO PARA CLIENTES
        if rol_usuario == "cliente":
            # CASO A: Cliente busca propiedad (la IA DEBE haber identificado filtros claros)
            if intencion_usuario == "buscar_propiedad":
                # PASO 1: Presentar propiedades SIEMPRE primero.
                propiedades_seleccionadas = elegir_top_n_propiedades(inventario_disponible, deteccion, n=3)
                
                if not propiedades_seleccionadas:
                    # Si no hay propiedades, respuesta amigable y sugerir otros criterios.
                    respuesta_final_chatbot = (f"¡Hola! 👋 Gracias por tu interés en {filtros_busqueda.get('tipo_propiedad', 'inmuebles')} "
                                               f"en {filtros_busqueda.get('zona', 'tu zona preferida')}.")
                    # (Aquí podemos hacer que Paty indague un poco más)
                    prompt_indagar_cliente = f"""
                    Eres Paty, tu asistente VIP de Mettryc Realty. El cliente buscaba '{filtros_busqueda.get('tipo_propiedad', 'inmuebles')} en {filtros_busqueda.get('zona', 'la zona')} para '{filtros_busqueda.get('tipo_operacion', 'venta')}'.
                    No se encontraron propiedades. Pregúntale de forma cálida y persuasiva si desea ajustar la búsqueda o dar más detalles para encontrar su hogar ideal. Máximo 30 palabras.
                    """
                    historial_para_ia_indagar = [{"role": "system", "content": prompt_indagar_cliente}] + memoria_conversaciones[sender][-4:]
                    respuesta_final_chatbot += "\n" + consultar_ia(historial_para_ia_indagar, max_tokens_respuesta=60)
                else:
                    # Si encontramos propiedades, las presentamos primero, CON UN MENSAJE PERSUASIVO.
                    fichas_formateadas = [formatear_ficha_propiedad(p, es_colega=False) for p in propiedades_seleccionadas]
                    respuesta_final_chatbot = (
                        f"¡Hola! 👋 ¡Qué gusto atenderte! Soy Paty, tu asistente VIP de Mettryc Realty. ✨ Analicé tu búsqueda de {filtros_busqueda.get('tipo_propiedad', 'inmuebles')} en {filtros_busqueda.get('zona', 'la zona')} para {filtros_busqueda.get('tipo_operacion', 'venta')} y encontré estas 3 opciones geniales que creo que te encantarán. ¡Échales un vistazo!\n\n" 
                        + "\n\n".join(fichas_formateadas) +
                        "\n\n¿Qué te parecen? 🤔 Si estas opciones te llaman la atención o si buscas algo específico, ¡dímelo para afinar la búsqueda! Si prefieres, podemos conectarte con un asesor experto."
                    )
                    # La LÓGICA DE CAPTURA DE DATOS se activará DESPUÉS si el cliente responde indicando interés en avanzar.
                    
            # CASO B: Cliente con intención general o saludo (NO busca propiedad explícita, o no se detectaron filtros claros)
            elif intencion_usuario == "info_general":
                # Usar IA para una respuesta amigable y que invite a la conversación.
                prompt_respuesta_cliente = f"""
                Eres Paty, tu asistente VIP de Mettryc Realty. Responde de forma cálida y amigablemente, máximo 30 palabras.
                Sé útil y haz una pregunta abierta para continuar la charla y ENTENDER MEJOR LA NECESIDAD INMOBILIARIA DEL PROSPECTO.
                Evita pedir datos personales directamente. Genera una conexión emocional.
                Contexto: El usuario es un CLIENTE. Su última interacción fue: "{mensaje_usuario_raw}".
                """
                historial_para_ia = [{"role": "system", "content": prompt_respuesta_cliente}] + memoria_conversaciones[sender][-4:] 
                respuesta_final_chatbot = consultar_ia(historial_para_ia, max_tokens_respuesta=60)
            else: # Otro tipo de intención de cliente no manejada explícitamente
                respuesta_final_chatbot = "¡Hola! Gracias por contactarnos. Soy Paty, tu asistente VIP en Mettryc Realty. ¿En qué puedo ayudarte hoy con tus sueños inmobiliarios? ✨"

        # --- 2. LÓGICA PARA 'colega_inmobiliario' ---
        elif rol_usuario == "colega_inmobiliario":
            if intencion_usuario == "buscar_propiedad" or intencion_usuario == "pedir_ficha_completa": 
                propiedades_seleccionadas = elegir_top_n_propiedades(inventario_disponible, deteccion, n=3)
                
                if not propiedades_seleccionadas:
                    respuesta_final_chatbot = "Hola colega. Revisé nuestro inventario pero hoy no encontré propiedades que se ajusten a tu búsqueda. ¿Puedo ayudarte con algo más?"
                else:
                    # Construir la respuesta para colega, incluyendo el tipo de operación correcto
                    operacion_detectada = deteccion.get("filtros", {}).get("tipo_operacion", "venta") # Default a venta
                    fichas_formateadas = [formatear_ficha_propiedad(p, es_colega=True) for p in propiedades_seleccionadas]
                    respuesta_final_chatbot = f"¡Hola colega! 👋 Te comparto estas opciones de nuestro inventario para {operacion_detectada} que podrían interesarte. Por favor, ten en cuenta que los datos de 'WhatsApp Captador' se obtienen de nuestra base de datos y se recomienda verificar directamente:\n\n" + "\n\n".join(fichas_formateadas)
                    
            else: # Colega con intención general
                prompt_respuesta_colega = f"""
                Eres un asistente profesional para colegas de Mettryc Realty. Responde de forma cordial y eficiente, máximo 30 palabras.
                Contexto: El usuario es un COLEGA INMOBILIARIO. Su última interacción fue: "{mensaje_usuario_raw}".
                """
                historial_para_ia = [{"role": "system", "content": prompt_respuesta_colega}] + memoria_conversaciones[sender][-4:]
                respuesta_final_chatbot = consultar_ia(historial_para_ia, max_tokens_respuesta=60)

        # --- LÓGICA DE CAPTURA DE LEAD PARA CLIENTES (SEGUNDO NIVEL DE INTENCIÓN) ----
        # Esta sección se ACTIVA SOLO si el cliente ha mostrado interés explícito DESPUÉS de ver propiedades
        # o si proporciona los datos directamente. TAMBIÉN SE ACTIVA SI LA INTENCIÓN DETECTADA FUE 'pedir_ficha_completa'.
        
        # Condición para iniciar la captura:
        # 1. Rol es 'cliente'.
        # 2. Intención ES 'buscar_propiedad' (pero YA SE PRESENTARON PROPIEDADES O EL CLIENTE RESPONDIÓ AVANZANDO)
        #    O la intención es 'pedir_ficha_completa'.
        # 3. Cliente NO ha sido procesado aún.
        # 4. Y en su ÚLTIMO mensaje hay INDICIO de querer avanzar (feedback/datos).

        # Verificamos si la respuesta anterior fue la de mostrar propiedades O si el cliente directamente da nombre/correo
        # O si la intención detectada es 'pedir_ficha_completa' (alto interés)
        
        # Condición para la captura de datos del cliente
        necesita_captura = False
        if rol_usuario == "cliente" and sender not in clientes_procesados:
             # Si la IA detectó 'buscar_propiedad' y el cliente responde de forma que indica avance, o da datos.
             if intencion_usuario == "buscar_propiedad":
                 # Si el mensaje actual contiene nombre y correo, es señal de avance.
                 if "nombre" in globals().get("datos_lead_extraccion", {}) and "correo" in globals().get("datos_lead_extraccion", {}):
                     necesita_captura = True
                 # Si el cliente responde afirmativamente a la pregunta "¿Qué te parecen?"
                 elif re.search(r"si|me gustan|interesa|avanzar|seguir|quiero|adelante", mensaje_usuario_raw, re.IGNORECASE):
                     necesita_captura = True
             # O si la intención fue explícitamente 'pedir_ficha_completa'
             elif intencion_usuario == "pedir_ficha_completa":
                 necesita_captura = True

        if necesita_captura:
            datos_lead_extraccion = {}
            # Extracción de datos clave
            nombre_match = re.search(r"(?:mi? nombre es|soy|me llamo|me llamo soy)\s+([A-Za-zÀ-ÖØ-ÿ'-]+(?:\s+[A-Za-zÀ-ÖØ-ÿ'-]+){1,})", mensaje_usuario_raw, re.IGNORECASE) # Busca Nombre Apellido
            correo_match = re.search(r"([\w\.-]+@[\w\.-]+\.[\w]+)", mensaje_usuario_raw, re.IGNORECASE) # Patrón básico de email
            telefono_match = re.search(r"(?:mi? teléfono es|mi? celular es|mi? wsp es|tel:|cel:)\s*([\+?\d\s()-]{7,})", mensaje_usuario_raw, re.IGNORECASE) # Patrón básico de teléfono
            
            if nombre_match: datos_lead_extraccion["nombre"] = nombre_match.group(1).strip()
            if correo_match: datos_lead_extraccion["correo"] = correo_match.group(1).strip()
            if telefono_match: datos_lead_extraccion["telefono"] = telefono_match.group(1).strip()
            elif sender.isdigit() and len(sender) > 5: # Usa el número del sender como fallback si es válido
                 datos_lead_extraccion["telefono"] = sender 
            
            # CONDICIÓN MÍNIMA PARA PROCEDER CON CAPTURA: Nombre COMPLETO Y Correo.
            if "nombre" in datos_lead_extraccion and "correo" in datos_lead_extraccion:
                
                # VALIDACIÓN ADICIONAL: Asegurar que el nombre tenga al menos dos partes (Nombre y Apellido).
                if len(datos_lead_extraccion["nombre"].split()) < 2: 
                    logger.warning(f"Nombre incompleto detectado de {sender}: '{datos_lead_extraccion['nombre']}'. Pidiendo apellidos.")
                    respuesta_final_chatbot = f"¡Excelente! Para poder registrar tu ficha VIP y asignarte el asesor especialista, por favor, confírmame tu Apellido. 😊"
                else:
                    # Si tenemos Nombre completo y Correo: ¡Procedemos a asignar agente y notificar!
                    agente_asignado = asignar_agente_round_robin()
                    if agente_asignado:
                        telefono_a_notificar = datos_lead_extraccion.get("telefono", sender) 
                        
                        datos_notificacion = {
                            "Nombre": datos_lead_extraccion["nombre"],
                            "Correo": datos_lead_extraccion["correo"],
                            "Telefono": telefono_a_notificar,
                            "Origen": "Chatbot Inmobiliario" 
                        }
                        enviar_notificaciones_telegram(agente_asignado, telefono_a_notificar, datos_notificacion)
                        
                        clientes_procesados.add(sender) # Marcar como cliente procesado
                        
                        respuesta_final_chatbot = (
                            f"¡Perfecto, {datos_lead_extraccion['nombre'].split()[0]}! ✨"
                            f" He registrado tus datos VIP. Nuestro asesor, *{agente_asignado['nombre']}*, te contactará de inmediato para atención personalizada. "
                            f"¡Gracias por confiar en Mettryc Realty! 🤝"
                        )
                    else:
                        # Si no se pudo asignar agente (ej. lista vacía)
                        respuesta_final_chatbot = "¡Genial! Hemos registrado tu interés y estamos asignando un asesor de inmediato. Por favor, espera un momento mientras te conectamos. Gracias."
            
            # Si faltan datos cruciales (Nombre o Correo) Y NO hemos generado la respuesta de propiedades.
            # Esto previene pedir datos si el cliente solo saludó o hizo consulta general.
            elif not respuesta_final_chatbot.startswith("¡Hola! 👋 ¡Qué gusto atenderte! Soy Paty, tu asistente VIP de Mettryc Realty."):
                 if not "nombre" in datos_lead_extraccion: 
                     respuesta_final_chatbot = "¡Me alegra tu interés! Para registrar tu ficha VIP y asignar un asesor, confírmame tu Nombre Completo (Nombre y Apellido). 😊"
                 elif "correo" not in datos_lead_extraccion: 
                      respuesta_final_chatbot = f"¡Excelente, {datos_lead_extraccion['nombre'].split()[0]}! Ya tengo tu nombre. Por favor, compárteme tu Correo Electrónico actual para completar tu ficha VIP. 📲"

        # --- Actualizar historial de conversación ---
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_usuario_raw})
        if not memoria_conversaciones[sender] or memoria_conversaciones[sender][-1]["content"] != respuesta_final_chatbot:
            memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_final_chatbot})
        
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        # --- Formatear y retornar ---
        respuesta_limpia = respuesta_final_chatbot.replace("**", "*") 

        return {"replies": [{"message": respuesta_limpia}]}

    except HTTPException as e:
        raise e 
    except Exception as e:
        logger.error(f"Error crítico e inesperado en el endpoint /webhook: {e}", exc_info=True)
        return {"replies": [{"message": "Lo siento, hemos encontrado un inconveniente técnico. Por favor, intenta de nuevo más tarde."}]}
