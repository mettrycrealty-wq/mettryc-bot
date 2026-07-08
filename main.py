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
MODELO_RESPALDO = "anthropic/claude-3.5-haiku" # Este modelo puede tener problemas de acceso temporal

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
        # Construir la URL con parámetros limpios
        url = url_base + "&".join([f"{k}={v}" for k, v in params.items() if v is not None])

        exito_pagina = False
        intentos = 0

        while intentos < 3 and not exito_pagina:
            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status() # Lanza excepción para respuestas de error HTTP (4xx, 5xx)
                data = response.json()
                contador_pagina = 0

                for key, value in data.items():
                    if isinstance(value, dict) and key.isdigit():
                        contador_pagina += 1
                        id_prop = value.get('id_property')
                        enlace_web = f"https://www.mettryc.com/inmueble/{id_prop}"

                        user_data = value.get('user_data', {})
                        asesor_encargado = f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip() or "Asesor Mettryc"
                        telefono_asesor = user_data.get('phone', '') # Teléfono del asesor

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
                            "captador": asesor_encargado,
                            "telefono_captador": telefono_asesor
                        })

                exito_pagina = True # Página obtenida correctamente
                total_obtenidas += contador_pagina
                logger.info(f"Descargadas {contador_pagina} propiedades en esta página. Total acumulado: {total_obtenidas}")

                if contador_pagina < take:
                    logger.info(f"Descarga completa: Última página tiene menos de {take} registros. Total de propiedades obtenidas: {total_obtenidas}")
                    return propiedades # Terminamos bucle si la última página no está completa

                skip += take
                time.sleep(2) # Pausa para no saturar la API de Wasi

            except requests.exceptions.RequestException as e:
                intentos += 1
                logger.warning(f"Intento {intentos}/3 de obtener inventario desde Wasi. URL: {url}. Error: {e}. Reintentando en 5 segundos...")
                time.sleep(5)
            except Exception as e:
                logger.error(f"Error inesperado al procesar la respuesta del inventario de Wasi: {e}", exc_info=True)
                break # Salir del bucle si hay un error no recuperable en el parsing o procesamiento

        if not exito_pagina:
            logger.error(f"No se pudo obtener el inventario de Wasi después de {intentos} intentos para la página con skip={skip}.")
            break # Salir si fallaron todos los intentos de una página

    return propiedades

def obtener_inventario_cache():
    """Retorna el inventario desde caché. Si está obsoleto o vacío, lo actualiza desde Wasi."""
    ahora = datetime.now()
    inventario_actual = cache["inventario_completo"]
    ultima_act = cache["ultima_actualizacion_inventario"]
    
    # Condición de actualización: si no hay inventario o ha pasado el intervalo
    necesita_actualizacion = not inventario_actual or (ahora - ultima_act > INTERVALO_ACTUALIZACION_INVENTARIO)

    if necesita_actualizacion:
        logger.info("Cache de inventario obsoleta o vacía. Intentando actualizar desde Wasi...")
        inventario_nuevo = obtener_inventario_desde_wasi()
        
        # Solo actualizar caché si la obtención fue exitosa y devolvió propiedades
        if inventario_nuevo:
            cache["inventario_completo"] = inventario_nuevo
            cache["ultima_actualizacion_inventario"] = ahora
            logger.info(f"Inventario actualizado y almacenado en caché con {len(inventario_nuevo)} propiedades.")
        else:
            logger.warning("La obtención del inventario desde Wasi falló o devolvió lista vacía. Se mantiene el inventario anterior en caché (si existe).")
            # Opcional: si el caché estaba vacío y falla, podrías forzar un error aquí si el bot no puede operar sin inventario.
            if not inventario_actual:
                logger.error("No se pudo obtener inventario inicial y la caché está vacía. Las funciones de propiedades podrían fallar.")
    else:
        logger.debug("Usando inventario de propiedades desde caché.")
    return cache["inventario_completo"]

def sincronizar_google_sheet():
    """Sincroniza la lista de agentes y captadores desde la URL de Google Sheets."""
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL")
    if not script_url:
        logger.warning("La URL de Google Sheets (GOOGLE_SHEET_TURNOS_URL) no está configurada. Los datos de agentes/captadores no se sincronizarán.")
        return

    ahora = datetime.now()
    # Actualizar si ha pasado el intervalo o si los datos importantes (agentes/captadores) están vacíos
    si_necesita_actualizacion = (ahora - sheets_cache["ultima_actualizacion_sheets"] > INTERVALO_ACTUALIZACION_SHEETS) or \
                                (not sheets_cache["agentes"] or not sheets_cache["captadores"])
    
    if si_necesita_actualizacion:
        logger.info("Sincronizando Google Sheets para obtener datos de agentes y captadores...")
        try:
            response = requests.get(script_url, timeout=15)
            response.raise_for_status() # Lanza excepción para errores HTTP
            payload_sheet = response.json()

            # Validar que el payload sea un diccionario y contenga las claves esperadas
            if isinstance(payload_sheet, dict) and \
               "agentes" in payload_sheet and isinstance(payload_sheet["agentes"], list) and \
               "captadores" in payload_sheet and isinstance(payload_sheet["captadores"], dict):
                
                sheets_cache["agentes"] = payload_sheet["agentes"]
                # Limpiar captadores: asegurarse que sea un diccionario y los teléfonos sean strings
                sheets_cache["captadores"] = {
                    k: str(v) for k, v in payload_sheet["captadores"].items() if isinstance(v, (str, int, float))
                }
                sheets_cache["ultima_actualizacion_sheets"] = ahora
                logger.info(f"✅ Sincronizados {len(sheets_cache['agentes'])} agentes y {len(sheets_cache['captadores'])} captadores desde Google Sheets.")
            else:
                logger.warning(f"Ocurrió un problema con los datos recibidos de Google Sheets. Tarea: Expected a dictionary with 'agentes' (list) and 'captadores' (dict). Received: {payload_sheet}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Error de red al intentar sincronizar Google Sheets: {e}")
        except json.JSONDecodeError:
            logger.error(f"Error al decodificar la respuesta JSON de Google Sheets.")
        except Exception as e:
            logger.error(f"Error inesperado durante la sincronización de Google Sheets: {e}", exc_info=True)
    else:
        logger.debug("Usando datos de agentes/captadores desde caché.")

def asignar_agente_round_robin():
    """Asigna un agente de forma cíclica (Round Robin)."""
    sincronizar_google_sheet() # Asegura que los datos de agentes estén actualizados
    lista_agentes = sheets_cache["agentes"]
    
    if not lista_agentes:
        logger.warning("No hay agentes disponibles en caché para asignar.")
        return None

    # Calcular el próximo índice de agente
    sheets_cache["ultimo_indice_agente"] = (sheets_cache["ultimo_indice_agente"] + 1) % len(lista_agentes)
    agente_asignado = lista_agentes[sheets_cache["ultimo_indice_agente"]]
    
    logger.info(f"Agente asignado (Round Robin): {agente_asignado.get('nombre', 'Sin Nombre')} (Índice: {sheets_cache['ultimo_indice_agente']})")
    return agente_asignado

def enviar_notificaciones_telegram(agente, telefono_destino, datos_lead_dict):
    """Envía notificaciones a Telegram para el agente asignado y el administrador."""
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    
    if not telegram_token:
        logger.warning("TELEGRAM_BOT_TOKEN no se encuentra configurado. Las notificaciones a Telegram no serán enviadas.")
        return

    # Limpiar el número de teléfono para crear un enlace de WhatsApp válido
    telefono_limpio = re.sub(r'\D', '', str(telefono_destino)) # Asegura que sea string y saca dígitos
    link_wa = f"https://wa.me/{telefono_limpio}" if telefono_limpio and len(telefono_limpio) >= 7 else "#" # Mínimo 7 dígitos para ser razonable

    # Construir un string legible de los datos del lead para los mensajes
    datos_lead_str_list = []
    for k, v in datos_lead_dict.items():
        if v: # Solo incluir si el valor no está vacío
            datos_lead_str_list.append(f"- **{k.capitalize()}**: {v}")
    datos_lead_str = "\n".join(datos_lead_str_list) or "No hay detalles adicionales."

    # Mensajes de Telegram
    mensaje_agente = (
        f"👤 *¡Nuevo Cliente VIP Asignado!*\n\n"
        f"Tu cliente potencial es: **{agente.get('nombre', 'Sin nombre')}**\n\n"
        f"*Detalles del Lead:*\n{datos_lead_str}\n\n"
        f"📲 *Contacta de inmediato (WhatsApp):* {link_wa}"
    )
    
    mensaje_admin = (
        f"👁️ *REPORTE ADMIN: Nuevo Lead Capturado*\n\n"
        f"👤 *Agente a cargo:* **{agente.get('nombre', 'Sin nombre')}**\n"
        f"*Detalles del Lead:*\n{datos_lead_str}\n"
        f"📲 *Link WhatsApp:* {link_wa}"
    )

    # Enviar notificación al agente
    if agente and agente.get("telegram_id"):
        agente_id = str(agente["telegram_id"]).strip()
        if agente_id and agente_id != "None" and agente_id.lstrip('-').isdigit(): # Validación básica de ID válido
            try:
                requests.post(
                    f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                    json={"chat_id": agente_id, "text": mensaje_agente, "parse_mode": "Markdown"},
                    timeout=5 # Timeout corto para no bloquear la respuesta
                )
                logger.info(f"Notificación de nuevo lead enviada a Telegram para el agente ID: {agente_id}.")
            except Exception as e:
                logger.error(f"Error al enviar notificación a Telegram para el agente ID {agente_id}: {e}")
        else:
             logger.warning(f"El agente '{agente.get('nombre')}' no tiene un telegram_id válido ({agente.get('telegram_id')}). No se enviará notificación.")
    else:
         logger.warning(f"El agente '{agente.get('nombre')}' no tiene un telegram_id configurado. No se enviará notificación.")

    # Enviar notificación al administrador
    if admin_id and admin_id.lstrip('-').isdigit(): # Validación básica de ID válido
        try:
            requests.post(
                f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                json={"chat_id": admin_id, "text": mensaje_admin, "parse_mode": "Markdown"},
                timeout=5
            )
            logger.info(f"Notificación de nuevo lead enviada a Telegram al administrador ID: {admin_id}.")
        except Exception as e:
            logger.error(f"Error al enviar notificación a Telegram al administrador ID {admin_id}: {e}")
    else:
        logger.warning("TELEGRAM_ADMIN_ID no está configurado o es inválido. No se enviarán notificaciones administrativas.")

# --- FUNCIÓN IA CENTRALIZADA Y BLINDADA ---
def consultar_ia(historial: list, max_tokens_respuesta: int = 100) -> str:
    """
    Consulta a la IA, intentando modelos principal y de respaldo.
    Maneja errores y limita la longitud de la respuesta en tokens.
    """
    url_ia = "https://openrouter.ai/api/v1/chat/completions"
    api_key = os.getenv("OPENROUTER_API_KEY")
    
    if not api_key:
        logger.error("El acceso a la IA está deshabilitado: OPENROUTER_API_KEY no configurado.")
        return "Lo siento, mi sistema de inteligencia artificial no está disponible en este momento. Por favor, inténtelo más tarde."

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    modelos_a_probar = [MODELO_PRINCIPAL, MODELO_RESPALDO]

    for modelo in modelos_a_probar:
        try:
            logger.info(f"Intentando consulta a IA con modelo: {modelo}")
            response = requests.post(
                url_ia,
                headers=headers,
                json={
                    "model": modelo,
                    "messages": historial,
                    "max_tokens": max_tokens_respuesta # Limitar la salida generada
                },
                timeout=30 # Timeout general para la petición a la IA
            )
            response.raise_for_status() # Lanza excepción para errores HTTP (4xx, 5xx)
            data = response.json()

            if 'choices' in data and data['choices']:
                respuesta_ia = data['choices'][0]['message']['content']
                if respuesta_ia and respuesta_ia.strip(): # Asegurar que no sea vacío tras .strip()
                    logger.info(f"Respuesta exitosa de la IA con modelo '{modelo}'.")
                    return respuesta_ia.strip()
                else:
                    logger.warning(f"La IA ({modelo}) devolvió una respuesta vacía o solo espacios en blanco.")
            else:
                logger.warning(f"La IA ({modelo}) devolvió una estructura inesperada o sin 'choices'. Payload: {data}")

        except requests.exceptions.RequestException as e:
            logger.warning(f"Error de red/HTTP al consultar IA ({modelo}): {e}")
        except json.JSONDecodeError:
            logger.error(f"Error al decodificar la respuesta JSON de la IA ({modelo}). Payload: {response.text}")
        except Exception as e:
            logger.error(f"Error inesperado procesando la respuesta de la IA ({modelo}): {e}", exc_info=True)

    # Si todos los modelos fallan
    logger.error("Todos los intentos de consulta a la IA han fallado.")
    return "Lo siento, parece que mi sistema de inteligencia artificial está temporalmente fuera de servicio. Por favor, inténtalo de nuevo en unos minutos. 🙏"

# --- FUNCIONES DE CLASIFICACIÓN Y DETECCIÓN ---

def validar_filtro_numerico(valor, tipo_esperado=int):
    """Valida si un valor es del tipo numérico esperado (int/float) o None."""
    if valor is None:
        return None
    try:
        if tipo_esperado == int:
            return int(valor)
        elif tipo_esperado == float:
            return float(valor)
    except (ValueError, TypeError):
        return None
    return None # Por si acaso

def detectar_rol_y_intencion(mensaje_usuario: str, historial_conversacion: list) -> dict:
    """
    Usa IA para clasificar el rol del usuario (cliente/colega) y su intención principal.
    También extrae filtros de búsqueda si aplica.
    """
    # Prompt optimizado para la IA, asegurando las llaves escapadas
    prompt_deteccion = f"""
    Eres un asistente de clasificación de usuarios para un chatbot inmobiliario.
    Analiza el siguiente mensaje y el historial de la conversación para determinar:
    1. EL ROL DEL USUARIO: DEBE SER 'cliente' o 'colega_inmobiliario'.
    2. LA INTENCIÓN PRINCIPAL: DEBE SER 'buscar_propiedad', 'pedir_ficha_completa', 'solicitar_captador', 'info_general', o 'otro'.
    3. FILTROS DE BÚSQUEDA (SI APLICA): Un diccionario con 'zona' (string), 'tipo' (string), 'precio_max' (número), 'habitaciones_min' (número). Usa valores numéricos REALISTAS para precios (ej. 100000, 250000) y habitaciones (ej. 2, 3). Usa null si no se especifica.

    Criterios clave:
    - Si hay menciones de comisiones, captaciones, otros agentes, MLS, o te refieres a mí como 'colega' o 'agente', clasifícate como 'colega_inmobiliario'.
    - Si buscas propiedades, identifica la zona, tipo, presupuesto y habitaciones.

    Formato de salida JSON (obligatorio, SOLO el JSON):
    {{"rol": "...", "intencion": "...", "filtros": {{"zona": "...", "tipo": "...", "precio_max": 100000, "habitaciones_min": 2}} }}

    Ejemplos válidos:
    - Colega buscando: {{"rol": "colega_inmobiliario", "intencion": "buscar_propiedad", "filtros": {{"zona": "Altamira", "tipo": "casa", "precio_max": 300000, "habitaciones_min": 4}} }}
    - Colega pidiendo info: {{"rol": "colega_inmobiliario", "intencion": "info_general", "filtros": {{}}}}
    - Cliente buscando: {{"rol": "cliente", "intencion": "buscar_propiedad", "filtros": {{"zona": "Las Mercedes", "tipo": "apartamento", "precio_max": 150000, "habitaciones_min": 3}} }}
    - Cliente pidiendo info/saludando: {{"rol": "cliente", "intencion": "info_general", "filtros": {{}}}}
    - Cliente con precio bajo: {{"rol": "cliente", "intencion": "buscar_propiedad", "filtros": {{"precio_max": 500, "habitaciones_min": 1}} }} # Nota: Precio bajo para ejemplo
    """

    mensajes_para_ia = [{"role": "system", "content": prompt_deteccion}]
    # Usar contexto reciente de la conversación para mejorar la detección
    mensajes_para_ia.extend(historial_conversacion[-4:]) 
    mensajes_para_ia.append({"role": "user", "content": f"Analiza este mensaje: \"{mensaje_usuario}\""})

    # Llamamos a la IA para obtener la clasificación. Aumentamos un poco los tokens para asegurar el JSON completo.
    respuesta_raw = consultar_ia(mensajes_para_ia, max_tokens_respuesta=250) 

    try:
        # Buscar y extraer el bloque JSON de la respuesta cruda de la IA
        match = re.search(r'\{.*?\}', respuesta_raw, re.DOTALL)
        if match:
            respuesta_json_str = match.group(0)
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
            # Si precio_max es 0 o negativo, anularlo.
            if filtros_limpios["precio_max"] is not None and filtros_limpios["precio_max"] <= 0:
                filtros_limpios["precio_max"] = None
            # Si habitaciones_min es negativo, anularlo.
            if filtros_limpios["habitaciones_min"] is not None and filtros_limpios["habitaciones_min"] < 0:
                filtros_limpios["habitaciones_min"] = None
                
            # Validar rol principal: si no es cliente ni colega, por defecto es cliente
            rol_final = rol_detectado if rol_detectado in ["cliente", "colega_inmobiliario"] else "cliente"
            if rol_final == "cliente" and rol_detectado != "cliente":
                logger.warning(f"Rol detectado '{rol_detectado}' no es válido. Asumiendo rol por defecto: '{rol_final}'.")

            # Si no hay intención detectada, por defecto es info_general
            intencion_final = intencion_detectada if intencion_detectada and intencion_detectada != 'otro' else "info_general"
            if intencion_final == 'otro': intencion_final = "info_general"
            
            return {
                "rol": rol_final,
                "intencion": intencion_final,
                "filtros": filtros_limpios
            }
        else:
            logger.warning(f"No se encontró bloque JSON válido en la respuesta de la IA (detección). Respuesta cruda: {respuesta_raw}")
            # Fallback: Si no se extrae JSON, asumir que es un cliente con intención general.
            return {"rol": "cliente", "intencion": "info_general", "filtros": {}}

    except json.JSONDecodeError:
        logger.error(f"Error de decodificación JSON recibido de la IA (detección). Respuesta cruda: {respuesta_raw}")
        return {"rol": "cliente", "intencion": "info_general", "filtros": {}} # Fallback
    except Exception as e:
        logger.error(f"Error inesperado procesando la respuesta de la IA para detección: {e}", exc_info=True)
        return {"rol": "cliente", "intencion": "info_general", "filtros": {}} # Fallback

def formatear_ficha_propiedad(propiedad: dict, es_colega: bool = False) -> str:
    """Formatea la información de una propiedad para cliente (sin captador) o colega (con captador)."""
    lineas = [
        f"*{propiedad.get('titulo', 'Propiedad sin título')}*",
        f"📍 Zona: {propiedad.get('zona', 'N/D')} | Ciudad: {propiedad.get('ciudad', 'N/D')}",
        f"💰 Venta: {propiedad.get('venta', 'N/D')} | Renta: {propiedad.get('renta', 'N/D')}",
        f"📐 Área: {propiedad.get('area', 'N/D')}m² | 🛏️ Hab: {propiedad.get('habitaciones', 'N/D')} | 🛁 Baños: {propiedad.get('banos', 'N/D')}",
        f"🔗 Ver más: {propiedad.get('enlace', '#')}"
    ]
    if es_colega: # Añadir datos del captador solo si es para un colega
        telefono_captador = propiedad.get('telefono_captador', 'N/D')
        lineas.append(f"👤 Captador: {propiedad.get('captador', 'N/D')} | 📲 WhatsApp Captador: {telefono_captador if telefono_captador else 'N/D'}")
    
    return "\n".join(lineas)

def parsear_precio(precio_str: str) -> float:
    """Convierte un string de precio (ej: '$150,000.00') a float. Devuelve 0.0 si es inválido o 'N/D'."""
    if not precio_str or precio_str.lower() == 'n/d': return 0.0
    # Elimina caracteres no numéricos excepto puntos decimales
    precio_limpio = re.sub(r'[^\d.]', '', str(precio_str)) 
    try:
        # Intenta convertir a float, maneja posibles errores de formato
        valor_float = float(precio_limpio)
        # Considerar precios muy bajos como inválidos (ej. 500 en lugar de 500000)
        # Podría ser necesario un umbral más dinámico o un chequeo de contexto
        if valor_float < 1.0 and precio_str.strip() != '0.0': # Evitar que 0.0 se reporte como inválido
            logger.warning(f"Precio parseado como muy bajo ({valor_float} de '{precio_str}'). Podría ser inválido.")
            # return 0.0 # Opcionalmente, devolver 0 si es sospechosamente bajo
        return valor_float
    except ValueError:
        logger.warning(f"Error al parsear el precio '{precio_str}'. Retornando 0.0.")
        return 0.0

def elegir_top_n_propiedades(propiedades_disponibles: list, intencion: dict, n: int = 3) -> list:
    """
    Filtra y selecciona las 'n' propiedades más relevantes basadas en la intención del usuario
    (zona, tipo, precio_max, habitaciones_min).
    """
    filtros = intencion.get("filtros", {})
    zona_buscada = filtros.get("zona", "").lower()
    tipo_buscado = filtros.get("tipo", "").lower() 
    precio_max_buscado = filtros.get("precio_max")
    habitaciones_min_buscado = filtros.get("habitaciones_min")

    propiedades_filtradas = propiedades_disponibles

    # 1. Aplicar filtro de Zona si está especificado
    if zona_buscada:
        propiedades_filtradas = [
            p for p in propiedades_filtradas
            if zona_buscada in p.get('zona', '').lower() or zona_buscada in p.get('ciudad', '').lower()
        ]

    # Función de scoring para ordenar por relevancia general (más datos = mayor score)
    def score_propiedad(p):
        score = 0
        if p.get('zona', '') != 'N/D': score += 2
        if p.get('ciudad', '') != 'N/D': score += 1
        if p.get('venta', 'N/D') != 'N/D' or p.get('renta', 'N/D') != 'N/D': score += 1
        if p.get('area', 'N/D') != 'N/D': score += 1
        # Considerar habitaciones y baños solo si son números válidos y no 'N/D'
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

    # Ordenar propiedades según el score de relevancia
    propiedades_ordenadas_relevancia = sorted(propiedades_filtradas, key=score_propiedad, reverse=True)

    # 2. Aplicar filtros de Precio y Habitaciones (después de ordenar por relevancia)
    propiedades_finales_seleccionadas = []
    
    for p in propiedades_ordenadas_relevancia:
        # Parsear precios y habitaciones de la propiedad para comparación
        precio_venta_prop = parsear_precio(p.get('venta'))
        precio_renta_prop = parsear_precio(p.get('renta'))
        habitaciones_prop = p.get('habitaciones', 0) # Default a 0 si no hay dato
        
        # Asegurar que habitaciones_prop sea un entero para la comparación
        try:
            habitaciones_prop_int = int(habitaciones_prop) if habitaciones_prop not in ['N/D', ''] else 0
        except (ValueError, TypeError):
            habitaciones_prop_int = 0
            
        # Evaluar filtro de precio: debe cumplir si se especificó precio_max
        precio_cumple = True
        if precio_max_buscado is not None:
            # Debe ser menor o igual al máximo, Y aplicable a venta o renta
            precio_cumple = (precio_venta_prop > 0 and precio_venta_prop <= precio_max_buscado) or \
                            (precio_renta_prop > 0 and precio_renta_prop <= precio_max_buscado)

        # Evaluar filtro de habitaciones: debe cumplir si se especificó habitaciones_min
        habitaciones_cumplen = True
        if habitaciones_min_buscado is not None:
            habitaciones_cumplen = habitaciones_prop_int >= habitaciones_min_buscado

        # Si la propiedad cumple todos los filtros aplicados, añadirla
        if precio_cumple and habitaciones_cumplen:
            propiedades_finales_seleccionadas.append(p)
        
        # Detener si ya hemos encontrado suficientes propiedades
        if len(propiedades_finales_seleccionadas) >= n:
            break

    return propiedades_finales_seleccionadas[:n] # Devolver máximo 'n'

# --- LLAMADA CENTRAL DEL WEBHOOK ---
@app.post("/webhook")
async def handle_request(request: Request):
    try:
        # 1. Validar Clave API del cliente/agente
        api_key_header = request.headers.get("x-api-key")
        valid_api_keys = os.getenv("API_KEYS_AGENTES", "").split(",")
        if api_key_header not in valid_api_keys or not api_key_header:
            logger.error(f"Acceso denegado: API Key '{api_key_header}' inválida o no proporcionada.")
            raise HTTPException(status_code=403, detail="Acceso denegado. API Key inválida.")

        # 2. Parsear el payload de la solicitud
        data = await request.json()
        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        
        sender = str(payload.get("sender", "")).strip()
        mensaje_usuario_raw = str(payload.get("message", "")).strip()
        
        if not mensaje_usuario_raw:
            logger.info("Mensaje vacío recibido. Ignorando solicitud.")
            return {"replies": []} # Responder vacío si el mensaje está vacío

        # --- PROCESAMIENTO PRINCIPAL ---
        
        # 3. Cargar o actualizar datos maestros (Inventario y Agentes/Captadores)
        inventario_disponible = obtener_inventario_cache()
        if not inventario_disponible:
            logger.warning("No hay inventario de propiedades disponible. El bot no podrá responder con resultados de búsqueda.")
            # Responder evasivamente si no hay datos cruciales
            return {"replies": [{"message": "¡Hola! Actualmente estamos actualizando nuestro catálogo de propiedades. Por favor, inténtalo de nuevo en unos minutos."}]}
        
        sincronizar_google_sheet() # Asegurar que los datos de agentes/captadores estén frescos
        
        # 4. Preparar historial y detectar rol/intención/filtros usando IA
        if sender not in memoria_conversaciones:
            memoria_conversaciones[sender] = []
        
        # Usar la IA para entender qué quiere el usuario
        deteccion = detectar_rol_y_intencion(mensaje_usuario_raw, memoria_conversaciones[sender])
        rol_usuario = deteccion.get("rol", "cliente") # Default a cliente
        intencion_usuario = deteccion.get("intencion", "info_general") # Default a info_general
        filtros_busqueda = deteccion.get("filtros", {})
        
        logger.info(f"Usuario {sender}: Rol='{rol_usuario}', Intención='{intencion_usuario}', Filtros={filtros_busqueda}")

        respuesta_final_chatbot = "" # Inicializar la respuesta

        # --- Lógica de Respuesta según Rol ---
        if rol_usuario == "cliente":
            if intencion_usuario == "buscar_propiedad":
                # Encontrar las mejores propiedades según los filtros
                propiedades_seleccionadas = elegir_top_n_propiedades(inventario_disponible, deteccion, n=3)
                
                if not propiedades_seleccionadas:
                    # Respuesta si no se encuentran propiedades
                    respuesta_final_chatbot = "¡Hola! 👋 Gracias por contactarnos. Lamentablemente, no logré encontrar propiedades que se ajusten a tu búsqueda actual. ¿Podríamos intentar con otros criterios o zonas?"
                else:
                    # Formatear las fichas de propiedad para el cliente
                    fichas_formateadas = [formatear_ficha_propiedad(p, es_colega=False) for p in propiedades_seleccionadas]
                    
                    # Mensaje introductorio (máximo 30 palabras)
                    texto_introductorio = "¡Hola! 👋 ¡Qué gusto ayudarte! Encontré estas 3 opciones geniales que pensé que te podrían encantar. ✨"
                    respuesta_final_chatbot = texto_introductorio + "\n\n" + "\n\n".join(fichas_formateadas)
                    # Nota: La captura de datos se intentará ASÍ MISMO en el cliente si este responde con datos.

            else: # Cliente con intención general (saludo, pregunta no específica)
                # Usar IA para generar una respuesta corta y amigable
                prompt_respuesta_cliente = f"""
                Eres Paty, tu asistente VIP de Mettryc Realty. Responde cálida y amigablemente, máximo 30 palabras.
                Sé útil y haz una pregunta abierta para continuar la charla.
                Contexto: El usuario es un CLIENTE. Su última interacción fue: "{mensaje_usuario_raw}".
                """
                historial_para_ia = [{"role": "system", "content": prompt_respuesta_cliente}] + memoria_conversaciones[sender][-4:] # Usar contexto reciente
                respuesta_final_chatbot = consultar_ia(historial_para_ia, max_tokens_respuesta=60) # ~30 palabras

        elif rol_usuario == "colega_inmobiliario":
            # Si el colega solicita propiedades o fichas específicas
            if intencion_usuario == "buscar_propiedad" or intencion_usuario == "pedir_ficha_completa": 
                propiedades_seleccionadas = elegir_top_n_propiedades(inventario_disponible, deteccion, n=3)
                
                if not propiedades_seleccionadas:
                    respuesta_final_chatbot = "Hola colega. Revisé nuestro inventario pero hoy no encontré propiedades que se ajusten a tu búsqueda. ¿Puedo ayudarte con algo más?"
                else:
                    # Formatear fichas incluyendo datos del captador para el colega
                    fichas_formateadas = [formatear_ficha_propiedad(p, es_colega=True) for p in propiedades_seleccionadas]
                    # Respuesta para colega (sin límite de palabras para las fichas)
                    respuesta_final_chatbot = "¡Hola colega! 👋 Te comparto estas opciones de nuestro inventario que podrían interesarte:\n\n" + "\n\n".join(fichas_formateadas)
                    
            else: # Colega con intención general
                # Usar IA para respuesta corta y profesional
                prompt_respuesta_colega = f"""
                Eres un asistente profesional para colegas de Mettryc Realty. Responde de forma cordial y eficiente, máximo 30 palabras.
                Contexto: El usuario es un COLEGA INMOBILIARIO. Su última interacción fue: "{mensaje_usuario_raw}".
                """
                historial_para_ia = [{"role": "system", "content": prompt_respuesta_colega}] + memoria_conversaciones[sender][-4:]
                respuesta_final_chatbot = consultar_ia(historial_para_ia, max_tokens_respuesta=60)

        # --- LÓGICA DE CAPTURA DE LEAD PARA CLIENTES ---
        # Solo aplicable si el usuario es cliente, buscó propiedad, y hay indicios de que proporciona datos.
        # NOTA: Esta lógica es básica. Un flow de captura robusto necesitaría un manejo de diálogo más explícito.
        if rol_usuario == "cliente" and intencion_usuario == "buscar_propiedad" and sender not in clientes_procesados:
            
            # Intenta extraer Nombre, Correo y Teléfono del mensaje actual (rudimentario)
            datos_lead_extraccion = {}
            nombre_match = re.search(r"(?:mi? nombre es|soy|me llamo)\s+([A-Za-zÀ-ÖØ-ÿ'-]+(?:\s+[A-Za-zÀ-ÖØ-ÿ'-]+)+)", mensaje_usuario_raw, re.IGNORECASE)
            correo_match = re.search(r"([\w\.-]+@[\w\.-]+\.[\w]+)", mensaje_usuario_raw, re.IGNORECASE)
            telefono_match = re.search(r"(?:mi? teléfono es|mi? celular es|mi? wsp es|tel:|cel:)\s*([\+?\d\s()-]+)", mensaje_usuario_raw, re.IGNORECASE)
            
            if nombre_match: datos_lead_extraccion["nombre"] = nombre_match.group(1).strip()
            if correo_match: datos_lead_extraccion["correo"] = correo_match.group(1).strip()
            if telefono_match: datos_lead_extraccion["telefono"] = telefono_match.group(1).strip()
            elif sender.isdigit() and len(sender) > 5: # Si el sender es un número de teléfono válido
                 datos_lead_extraccion["telefono"] = sender # Usamos el número del sender como fallback
            
            # CONDICIÓN PARA PROCEDER: Necesitamos Nombre (mínimo 2 palabras) y Correo electrónico.
            if "nombre" in datos_lead_extraccion and "correo" in datos_lead_extraccion:
                
                if len(datos_lead_extraccion["nombre"].split()) < 2:
                    # Si el nombre es incompleto, pedir el apellido
                    logger.warning(f"Nombre incompleto de {sender}: '{datos_lead_extraccion['nombre']}'. Pidiendo apellidos.")
                    respuesta_final_chatbot = f"¡Excelente elección! Para poder registrar tu ficha VIP y asignarte el asesor especialista, por favor, confírmame tu Apellido. 😊"
                else:
                    # Tenemos Nombre (completo) y Correo: Proceder con asignación y notificación
                    agente_asignado = asignar_agente_round_robin()
                    if agente_asignado:
                        telefono_a_notificar = datos_lead_extraccion.get("telefono", sender) # Usar número capturado o sender
                        
                        # Preparar datos para notificación
                        datos_notificacion = {
                            "Nombre": datos_lead_extraccion["nombre"],
                            "Correo": datos_lead_extraccion["correo"],
                            "Telefono": telefono_a_notificar,
                            "Origen": "Chatbot Inmobiliario"
                        }
                        # Enviar notificaciones
                        enviar_notificaciones_telegram(agente_asignado, telefono_a_notificar, datos_notificacion)
                        
                        clientes_procesados.add(sender) # Marcar para evitar re-procesamiento
                        
                        # Respuesta de confirmación al cliente
                        respuesta_final_chatbot = (
                            f"¡Perfecto, {datos_lead_extraccion['nombre'].split()[0]}! ✨"
                            f" He registrado tus datos VIP en nuestro sistema. "
                            f"Nuestro asesor especializado, *{agente_asignado['nombre']}*, se pondrá en contacto contigo de inmediato para brindarte atención personalizada. "
                            f"¡Gracias por confiar en Mettryc Realty! 🤝"
                        )
                    else:
                        # Si no se pudo asignar agente (ej. lista vacía)
                        respuesta_final_chatbot = "¡Genial! Hemos registrado tu interés. Estamos asignando un asesor para tu atención inmediata. Por favor, espera un momento mientras te conectamos. Gracias por tu paciencia."
            
            # Si faltan datos cruciales (Nombre o Correo) y NO hemos procesado al cliente aún
            elif "nombre" not in datos_lead_extraccion or "correo" not in datos_lead_extraccion:
                 if not "nombre" in datos_lead_extraccion:
                     respuesta_final_chatbot = "¡Me alegra mucho tu interés en nuestras propiedades! Para registrar tu ficha VIP y asignar un asesor especialista, por favor, confírmame tu Nombre Completo (Nombre y Apellido). 😊"
                 elif "correo" not in datos_lead_extraccion:
                      respuesta_final_chatbot = f"¡Excelente, {datos_lead_extraccion['nombre'].split()[0]}! Ya tengo tu nombre. Por favor, compárteme tu Correo Electrónico actual para completar tu ficha VIP y que nuestro especialista se comunique contigo a la brevedad. 📲"
        
        # --- Actualizar historial de conversación ---
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_usuario_raw})
        # Añadir respuesta del asistente solo si es diferente para evitar duplicados en reintentos
        if not memoria_conversaciones[sender] or memoria_conversaciones[sender][-1]["content"] != respuesta_final_chatbot:
            memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_final_chatbot})
        
        # Limitar el tamaño del historial para optimizar memoria
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        # --- Formatear y retornar la respuesta ---
        # Limpieza final para asegurar formato compatible con WhatsApp (principalmente dobles asteriscos)
        respuesta_limpia = respuesta_final_chatbot.replace("**", "*")

        return {"replies": [{"message": respuesta_limpia}]}

    except HTTPException as e:
        # Si es un error esperado por HTTP (ej. 403 Forbidden), relanzarlo.
        raise e
    except Exception as e:
        # Capturar cualquier otro error inesperado durante el procesamiento.
        logger.error(f"Error crítico e inesperado en la ruta /webhook: {e}", exc_info=True)
        # Devolver un mensaje de error genérico al usuario final.
        return {"replies": [{"message": "Lo siento, hemos encontrado un inconveniente técnico. Por favor, intenta escribir tu mensaje de nuevo en un momento o contacta a soporte."}]}
