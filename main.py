import os
import requests
import logging
import time
import re
import json # Asegurada la importación
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
    "inventario_completo": [], 
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


# --- CONFIGURACIÓN ESTRATÉGICA DE MODELOS ---
MODELO_PRINCIPAL = "google/gemini-2.5-flash-lite"
MODELO_RESPALDO = "anthropic/claude-3.5-haiku" # Puede tener problemas temporales de acceso

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
                            "enlace": enlace_web, "captador": asesor_encargado, "telefono_captador": telefono_asesor
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
                logger.error(f"Error inesperado al procesar inventario de Wasi: {e}", exc_info=True)
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
        
        if inventario_nuevo: # Solo actualizar si la obtencion fue exitosa
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
    """Sincroniza agentes y captadores desde la URL de Google Sheets."""
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL")
    if not script_url:
        logger.warning("GOOGLE_SHEET_TURNOS_URL no configurada. No se sincronizarán datos de agentes/captadores.")
        return

    ahora = datetime.now()
    # Actualizar si ha pasado el intervalo O si los datos de caché están vacíos
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
                
                sheets_cache["agentes"] = payload_sheet["agentes"]
                # Limpiar y asegurar que captadores sean strings
                sheets_cache["captadores"] = {
                    k: str(v) for k, v in payload_sheet["captadores"].items() if isinstance(v, (str, int, float))
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
        logger.debug("Usando datos de agentes/captadores desde caché.")

def asignar_agente_round_robin():
    """Asigna un agente de forma cíclica (Round Robin)."""
    sincronizar_google_sheet()
    lista_agentes = sheets_cache["agentes"]
    
    if not lista_agentes:
        logger.warning("No hay agentes disponibles en caché para asignar.")
        return None

    sheets_cache["ultimo_indice_agente"] = (sheets_cache["ultimo_indice_agente"] + 1) % len(lista_agentes)
    agente_asignado = lista_agentes[sheets_cache["ultimo_indice_agente"]]
    logger.info(f"Agente asignado (Round Robin): {agente_asignado.get('nombre', 'Sin Nombre')}")
    return agente_asignado

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
        if agente_id and agente_id != "None" and agente_id.lstrip('-').isdigit(): # Validación básica ID
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
    if admin_id and admin_id.lstrip('-').isdigit(): # Validación básica ID
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
            response.raise_for_status() # Maneja errores HTTP
            data = response.json()

            if 'choices' in data and data['choices']:
                respuesta_ia = data['choices'][0]['message']['content']
                if respuesta_ia and respuesta_ia.strip(): # Asegurar que no esté vacía
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
    El prompt está optimizado para EXIGIR SOLO JSON de respuesta.
    """
    # Prompt mejorado para exigir SOLO JSON sin formato adicional como ```json
    prompt_deteccion = f"""
    Eres un clasificador de usuarios para chatbot inmobiliario. Analiza el mensaje y el historial para determinar:
    1. ROL: DEBE SER 'cliente' O 'colega_inmobiliario'.
    2. INTENCIÓN: DEBE SER 'buscar_propiedad', 'pedir_ficha_completa', 'solicitar_captador', 'info_general', o 'otro'.
    3. FILTROS (si aplica): dict con 'zona' (str), 'tipo' (str), 'precio_max' (numérico, usa null si no se especifica o es inválido), 'habitaciones_min' (numérico, usa null si no se especifica o es inválido). Usa valores NUMÉRICOS REALISTAS (ej. 100000).

    Critérios:
    - 'colega_inmobiliario' si hay mención de comisiones, captaciones, MLS, u otra inmobiliaria.
    - Si busca propiedad, identifica zona, tipo, presupuesto y habitaciones.

    DEBES RESPONDER EXCLUSIVAMENTE CON UN OBJETO JSON VÁLIDO, SIN TEXTO ADICIONAL, NI MARCADORES DE CÓDIGO COMO ```json.

    Ejemplos de JSON válido:
    - Colega buscando: {{"rol": "colega_inmobiliario", "intencion": "buscar_propiedad", "filtros": {{"zona": "Altamira", "tipo": "casa", "precio_max": 300000, "habitaciones_min": 4}} }}
    - Colega info: {{"rol": "colega_inmobiliario", "intencion": "info_general", "filtros": {{}}}}
    - Cliente buscando: {{"rol": "cliente", "intencion": "buscar_propiedad", "filtros": {{"zona": "Las Mercedes", "tipo": "apartamento", "precio_max": 150000, "habitaciones_min": 3}} }}
    - Cliente saludo: {{"rol": "cliente", "intencion": "info_general", "filtros": {{}}}}
    - Cliente precio bajo/inválido: {{"rol": "cliente", "intencion": "buscar_propiedad", "filtros": {{"precio_max": 500, "habitaciones_min": 1}} }}
    """

    mensajes_para_ia = [{"role": "system", "content": prompt_deteccion}]
    mensajes_para_ia.extend(historial_conversacion[-4:]) # Contexto reciente
    mensajes_para_ia.append({"role": "user", "content": f"Analiza este mensaje: \"{mensaje_usuario}\""})

    respuesta_raw = consultar_ia(mensajes_para_ia, max_tokens_respuesta=250) 

    try:
        # Intentar encontrar y parsear JSON de forma más robusta
        # Buscar el primer '{' y el último '}' para extraer un posible objeto JSON
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
            filtros_limpios = {
                "zona": filtros_raw.get("zona", "") if isinstance(filtros_raw.get("zona"), str) else "",
                "tipo": filtros_raw.get("tipo", "") if isinstance(filtros_raw.get("tipo"), str) else "",
                "precio_max": validar_filtro_numerico(filtros_raw.get("precio_max"), float),
                "habitaciones_min": validar_filtro_numerico(filtros_raw.get("habitaciones_min"), int),
            }
            # Anular valores numéricos inválidos o no realistas
            if filtros_limpios["precio_max"] is not None and filtros_limpios["precio_max"] <= 0: filtros_limpios["precio_max"] = None
            if filtros_limpios["habitaciones_min"] is not None and filtros_limpios["habitaciones_min"] < 0: filtros_limpios["habitaciones_min"] = None
                
            # Validar rol: por defecto 'cliente'
            rol_final = rol_detectado if rol_detectado in ["cliente", "colega_inmobiliario"] else "cliente"
            if rol_final == "cliente" and rol_detectado != "cliente":
                logger.warning(f"Rol detectado '{rol_detectado}' no es válido. Asumiendo rol por defecto: '{rol_final}'.")

            # Validar intención: por defecto 'info_general'
            intencion_final = intencion_detectada if intencion_detectada and intencion_detectada != 'otro' else "info_general"
            if intencion_final == 'otro': intencion_final = "info_general"
            
            return {"rol": rol_final, "intencion": intencion_final, "filtros": filtros_limpios}
        else:
            logger.error(f"No se pudo extraer un objeto JSON de la respuesta de la IA. Respuesta cruda: {respuesta_raw}")
            # Fallback si no se puede extraer el JSON
            return {"rol": "cliente", "intencion": "info_general", "filtros": {}}

    except json.JSONDecodeError:
        logger.error(f"Error de decodificación JSON de la IA. Respuesta cruda: {respuesta_raw}")
        return {"rol": "cliente", "intencion": "info_general", "filtros": {}} # Fallback
    except Exception as e:
        logger.error(f"Error inesperado procesando respuesta IA: {e}", exc_info=True)
        return {"rol": "cliente", "intencion": "info_general", "filtros": {}} # Fallback

def formatear_ficha_propiedad(propiedad: dict, es_colega: bool = False) -> str:
    """Formatea la información de una propiedad para cliente o colega."""
    lineas = [
        f"*{propiedad.get('titulo', 'Propiedad sin título')}*",
        f"📍 Zona: {propiedad.get('zona', 'N/D')} | Ciudad: {propiedad.get('ciudad', 'N/D')}",
        f"💰 Venta: {propiedad.get('venta', 'N/D')} | Renta: {propiedad.get('renta', 'N/D')}",
        f"📐 Área: {propiedad.get('area', 'N/D')}m² | 🛏️ Hab: {propiedad.get('habitaciones', 'N/D')} | 🛁 Baños: {propiedad.get('banos', 'N/D')}",
        f"🔗 Ver más: {propiedad.get('enlace', '#')}"
    ]
    if es_colega:
        telefono_captador = propiedad.get('telefono_captador', 'N/D')
        lineas.append(f"👤 Captador: {propiedad.get('captador', 'N/D')} | 📲 WhatsApp Captador: {telefono_captador if telefono_captador else 'N/D'}")
    
    return "\n".join(lineas)

def parsear_precio(precio_str: str) -> float:
    """Convierte string de precio a float. Devuelve 0.0 si es inválido o 'N/D'."""
    if not precio_str or precio_str.lower() == 'n/d': return 0.0
    precio_limpio = re.sub(r'[^\d.]', '', str(precio_str)) 
    try:
        valor_float = float(precio_limpio)
        # Umbral para considerar precio potencialmente inválido (ej. 500 vs 500000)
        if valor_float > 0 and valor_float < 1000 and precio_str.strip() != '0.0': 
            logger.warning(f"Precio parseado muy bajo ({valor_float} de '{precio_str}'). Podría ser inválido.")
        return valor_float
    except ValueError:
        logger.warning(f"Error al parsear precio '{precio_str}'. Retornando 0.0.")
        return 0.0

def elegir_top_n_propiedades(propiedades_disponibles: list, intencion: dict, n: int = 3) -> list:
    """Filtra y selecciona las 'n' propiedades más relevantes según intención y filtros."""
    filtros = intencion.get("filtros", {})
    zona_buscada = filtros.get("zona", "").lower()
    tipo_buscado = filtros.get("tipo", "").lower() 
    precio_max_buscado = filtros.get("precio_max")
    habitaciones_min_buscado = filtros.get("habitaciones_min")

    propiedades_filtradas = propiedades_disponibles

    # 1. Filtro por Zona
    if zona_buscada:
        propiedades_filtradas = [
            p for p in propiedades_filtradas
            if zona_buscada in p.get('zona', '').lower() or zona_buscada in p.get('ciudad', '').lower()
        ]

    # Función de scoring para relevancia general
    def score_propiedad(p):
        score = 0
        if p.get('zona', '') != 'N/D': score += 2
        if p.get('ciudad', '') != 'N/D': score += 1
        if p.get('venta', 'N/D') != 'N/D' or p.get('renta', 'N/D') != 'N/D': score += 1
        if p.get('area', 'N/D') != 'N/D': score += 1
        # Considerar habitaciones/baños si son numéricos válidos
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

    # Ordenar por relevancia
    propiedades_ordenadas_relevancia = sorted(propiedades_filtradas, key=score_propiedad, reverse=True)

    # 2. Aplicar filtros de Precio y Habitaciones
    propiedades_finales_seleccionadas = []
    
    for p in propiedades_ordenadas_relevancia:
        precio_venta_prop = parsear_precio(p.get('venta'))
        precio_renta_prop = parsear_precio(p.get('renta'))
        habitaciones_prop_int = 0 # Default
        try:
            if p.get('habitaciones') not in ['N/D', ''] : habitaciones_prop_int = int(p.get('habitaciones'))
        except (ValueError, TypeError): pass
            
        # Evaluar filtros
        precio_cumple = True
        if precio_max_buscado is not None:
            precio_cumple = (precio_venta_prop > 0 and precio_venta_prop <= precio_max_buscado) or \
                            (precio_renta_prop > 0 and precio_renta_prop <= precio_max_buscado)

        habitaciones_cumplen = True
        if habitaciones_min_buscado is not None:
            habitaciones_cumplen = habitaciones_prop_int >= habitaciones_min_buscado

        # Añadir si cumple filtros
        if precio_cumple and habitaciones_cumplen:
            propiedades_finales_seleccionadas.append(p)
        
        if len(propiedades_finales_seleccionadas) >= n: # Detener si ya tenemos suficientes
            break

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
        
        # 3. Cargar/Actualizar datos maestros
        inventario_disponible = obtener_inventario_cache()
        if not inventario_disponible:
            logger.warning("Inventario de propiedades no disponible. Consultas de búsqueda fallarán.")
            return {"replies": [{"message": "¡Hola! Estamos actualizando nuestro catálogo. Por favor, inténtalo de nuevo más tarde."}]}
        
        sincronizar_google_sheet()
        
        # 4. Detectar rol, intención y filtros usando IA
        if sender not in memoria_conversaciones:
            memoria_conversaciones[sender] = []
        
        deteccion = detectar_rol_y_intencion(mensaje_usuario_raw, memoria_conversaciones[sender])
        rol_usuario = deteccion.get("rol", "cliente")
        intencion_usuario = deteccion.get("intencion", "info_general")
        filtros_busqueda = deteccion.get("filtros", {})
        
        logger.info(f"Usuario {sender}: Rol='{rol_usuario}', Intención='{intencion_usuario}', Filtros={filtros_busqueda}")

        respuesta_final_chatbot = ""

        # --- Lógica de Respuesta según Rol ---
        if rol_usuario == "cliente":
            if intencion_usuario == "buscar_propiedad":
                propiedades_seleccionadas = elegir_top_n_propiedades(inventario_disponible, deteccion, n=3)
                
                if not propiedades_seleccionadas:
                    respuesta_final_chatbot = "¡Hola! 👋 Gracias por tu interés. Lamentablemente, no encontré propiedades que coincidan con tu búsqueda actual. ¿Probamos con otros criterios o zonas?"
                else:
                    fichas_formateadas = [formatear_ficha_propiedad(p, es_colega=False) for p in propiedades_seleccionadas]
                    texto_introductorio = "¡Hola! 👋 ¡Qué gusto atenderte! Encontré estas 3 opciones geniales que pensé que te podrían encantar. ✨"
                    respuesta_final_chatbot = texto_introductorio + "\n\n" + "\n\n".join(fichas_formateadas)

            else: # Cliente con intención general
                prompt_respuesta_cliente = f"""
                Eres Paty, tu asistente VIP de Mettryc Realty. Responde de forma cálida y amigablemente, máximo 30 palabras.
                Sé útil y haz una pregunta abierta para continuar la charla.
                Contexto: El usuario es un CLIENTE. Su última interacción fue: "{mensaje_usuario_raw}".
                """
                historial_para_ia = [{"role": "system", "content": prompt_respuesta_cliente}] + memoria_conversaciones[sender][-4:]
                respuesta_final_chatbot = consultar_ia(historial_para_ia, max_tokens_respuesta=60)

        elif rol_usuario == "colega_inmobiliario":
            if intencion_usuario == "buscar_propiedad" or intencion_usuario == "pedir_ficha_completa": 
                propiedades_seleccionadas = elegir_top_n_propiedades(inventario_disponible, deteccion, n=3)
                
                if not propiedades_seleccionadas:
                    respuesta_final_chatbot = "Hola colega. Revisé nuestro inventario pero hoy no encontré propiedades que se ajusten a tu búsqueda. ¿Puedo ayudarte con algo más?"
                else:
                    fichas_formateadas = [formatear_ficha_propiedad(p, es_colega=True) for p in propiedades_seleccionadas]
                    respuesta_final_chatbot = "¡Hola colega! 👋 Te comparto estas opciones de nuestro inventario que podrían interesarte:\n\n" + "\n\n".join(fichas_formateadas)
                    
            else: # Colega con intención general
                prompt_respuesta_colega = f"""
                Eres un asistente profesional para colegas de Mettryc Realty. Responde de forma cordial y eficiente, máximo 30 palabras.
                Contexto: El usuario es un COLEGA INMOBILIARIO. Su última interacción fue: "{mensaje_usuario_raw}".
                """
                historial_para_ia = [{"role": "system", "content": prompt_respuesta_colega}] + memoria_conversaciones[sender][-4:]
                respuesta_final_chatbot = consultar_ia(historial_para_ia, max_tokens_respuesta=60)

        # --- LÓGICA DE CAPTURA DE LEAD ---
        # Se ejecuta si es cliente, busca propiedad Y parece dar datos en su ÚLTIMO mensaje.
        if rol_usuario == "cliente" and intencion_usuario == "buscar_propiedad" and sender not in clientes_procesados:
            
            datos_lead_extraccion = {}
            # Mejoramos la extracción para ser más robustos
            nombre_match = re.search(r"(?:mi? nombre es|soy|me llamo|me llamo soy)\s+([A-Za-zÀ-ÖØ-ÿ'-]+(?:\s+[A-Za-zÀ-ÖØ-ÿ'-]+){1,})", mensaje_usuario_raw, re.IGNORECASE) # Al menos 2 palabras
            correo_match = re.search(r"([\w\.-]+@[\w\.-]+\.[\w]+)", mensaje_usuario_raw, re.IGNORECASE)
            telefono_match = re.search(r"(?:mi? teléfono es|mi? celular es|mi? wsp es|tel:|cel:)\s*([\+?\d\s()-]{7,})", mensaje_usuario_raw, re.IGNORECASE) # Mínimo 7 dígitos/símbolos
            
            if nombre_match: datos_lead_extraccion["nombre"] = nombre_match.group(1).strip()
            if correo_match: datos_lead_extraccion["correo"] = correo_match.group(1).strip()
            if telefono_match: datos_lead_extraccion["telefono"] = telefono_match.group(1).strip()
            elif sender.isdigit() and len(sender) > 5: 
                 datos_lead_extraccion["telefono"] = sender 
            
            # CONDICIÓN PARA PROCEDER: Nombre (completo) y Correo.
            if "nombre" in datos_lead_extraccion and "correo" in datos_lead_extraccion:
                
                if len(datos_lead_extraccion["nombre"].split()) < 2: # Re-validar por seguridad
                    logger.warning(f"Nombre incompleto de {sender}: '{datos_lead_extraccion['nombre']}'. Pidiendo apellidos.")
                    respuesta_final_chatbot = f"¡Excelente! Para poder registrar tu ficha VIP y asignarte el asesor, confírmame también tu Apellido. 😊"
                else:
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
                        
                        clientes_procesados.add(sender) 
                        
                        respuesta_final_chatbot = (
                            f"¡Perfecto, {datos_lead_extraccion['nombre'].split()[0]}! ✨"
                            f" He registrado tus datos VIP. Nuestro asesor, *{agente_asignado['nombre']}*, te contactará de inmediato para atención personalizada. "
                            f"¡Gracias por confiar en Mettryc Realty! 🤝"
                        )
                    else:
                        respuesta_final_chatbot = "¡Genial! Hemos registrado tu interés. Estamos asignando un asesor para tu atención inmediata. Por favor, espera un momento."
            
            # Si faltan datos cruciales (Nombre o Correo)
            elif "nombre" not in datos_lead_extraccion or "correo" not in datos_lead_extraccion:
                 if not "nombre" in datos_lead_extraccion:
                     respuesta_final_chatbot = "¡Me alegra tu interés! Para registrar tu ficha VIP y asignar un asesor, confírmame tu Nombre Completo (Nombre y Apellido). 😊"
                 elif "correo" not in datos_lead_extraccion:
                      respuesta_final_chatbot = f"¡Excelente, {datos_lead_extraccion['nombre'].split()[0]}! Ya tengo tu nombre. Por favor, compárteme tu Correo Electrónico actual para completar tu ficha VIP. 📲"
        
        # --- Actualizar historial y Limitar ---
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_usuario_raw})
        if not memoria_conversaciones[sender] or memoria_conversaciones[sender][-1]["content"] != respuesta_final_chatbot: # Evitar duplicados
            memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_final_chatbot})
        
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        # --- Formatear y retornar ---
        respuesta_limpia = respuesta_final_chatbot.replace("**", "*") # Limpieza final de formato

        return {"replies": [{"message": respuesta_limpia}]}

    except HTTPException as e:
        raise e # Relanzar errores HTTP conocidos
    except Exception as e:
        logger.error(f"Error crítico e inesperado en el endpoint /webhook: {e}", exc_info=True)
        return {"replies": [{"message": "Lo siento, hemos encontrado un inconveniente técnico. Por favor, intenta de nuevo más tarde."}]}
