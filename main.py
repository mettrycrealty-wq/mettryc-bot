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
    """Retorna el inventario desde caché o lo actualiza si ha pasado mucho tiempo."""
    if not cache["inventario_completo"] or \
       datetime.now() - cache["ultima_actualizacion_inventario"] > INTERVALO_ACTUALIZACION_INVENTARIO:
        
        logger.info("Actualizando inventario desde Wasi...")
        inventario_nuevo = obtener_inventario_desde_wasi()
        if inventario_nuevo:
            cache["inventario_completo"] = inventario_nuevo
            cache["ultima_actualizacion_inventario"] = datetime.now()
            logger.info(f"Inventario actualizado con {len(inventario_nuevo)} propiedades.")
        else:
            logger.warning("No se pudo obtener inventario nuevo, se mantiene el anterior en caché (si existe).")
    else:
        logger.debug("Usando inventario desde caché.")
    return cache["inventario_completo"]

def sincronizar_google_sheet():
    """Sincroniza la lista de agentes y captadores desde la URL de Google Sheets."""
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL")
    if not script_url:
        logger.warning("La URL de Google Sheets no está configurada. No se sincronizará.")
        return

    if datetime.now() - sheets_cache["ultima_actualizacion_sheets"] > INTERVALO_ACTUALIZACION_SHEETS or \
       not sheets_cache["agentes"]: # Sincronizar si ha pasado tiempo o si la lista está vacía
        try:
            response = requests.get(script_url, timeout=15)
            response.raise_for_status()
            payload_sheet = response.json()

            if isinstance(payload_sheet, dict):
                sheets_cache["agentes"] = payload_sheet.get("agentes", [])
                sheets_cache["captadores"] = payload_sheet.get("captadores", {}) # Esperamos un dict {nombre: telefono}
                sheets_cache["ultima_actualizacion_sheets"] = datetime.now()
                logger.info(f"✅ Sincronizados {len(sheets_cache['agentes'])} agentes y {len(sheets_cache['captadores'])} captadores.")
            else:
                logger.warning(f"Formato inesperado de payload de Google Sheets: {payload_sheet}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Error de red al sincronizar Google Sheets: {e}")
        except Exception as e:
            logger.error(f"Error inesperado al sincronizar Google Sheets: {e}", exc_info=True)

def asignar_agente_round_robin():
    """Asigna un agente usando el método Round Robin."""
    sincronizar_google_sheet()
    lista_agentes = sheets_cache["agentes"]
    if not lista_agentes:
        logger.warning("No hay agentes disponibles para asignar.")
        return None

    sheets_cache["ultimo_indice_agente"] = (sheets_cache["ultimo_indice_agente"] + 1) % len(lista_agentes)
    agente_asignado = lista_agentes[sheets_cache["ultimo_indice_agente"]]
    logger.info(f"Agente asignado: {agente_asignado.get('nombre')}")
    return agente_asignado

def enviar_notificaciones_telegram(agente, telefono_destino, datos_lead_dict):
    """Envía notificaciones a Telegram para el agente asignado y el administrador."""
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    
    if not telegram_token:
        logger.warning("TELEGRAM_BOT_TOKEN no configurado. No se enviarán notificaciones.")
        return

    # Asegurarse que el teléfono tenga formato usable para el enlace de WhatsApp
    telefono_limpio = re.sub(r'\D', '', telefono_destino)
    link_wa = f"https://wa.me/{telefono_limpio}" if telefono_limpio else "#"

    # Construir un string legible de los datos del lead
    datos_lead_str = "\n".join([f"- {k.capitalize()}: {v}" for k, v in datos_lead_dict.items()])

    mensaje_agente = (
        f"👤 *¡Nuevo Cliente VIP Asignado!*\n\n"
        f"Tu cliente potencial es: {agente.get('nombre', 'Sin nombre')}\n\n"
        f"*A continuación, los detalles del lead:*\n{datos_lead_str}\n\n"
        f"📲 *Contacta de inmediato:* {link_wa}"
    )
    
    mensaje_admin = (
        f"👁️ *REPORTE ADMIN: Nuevo Lead Capturado*\n\n"
        f"👤 *Agente a cargo:* {agente.get('nombre', 'Sin nombre')}\n"
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
        else:
             logger.warning(f"Agente {agente.get('nombre')} no tiene telegram_id. No se notificará.")

    except Exception as e:
        logger.error(f"Error al notificar al agente por Telegram: {e}")

    try:
        if admin_id:
            requests.post(
                f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                json={"chat_id": admin_id, "text": mensaje_admin, "parse_mode": "Markdown"},
                timeout=5
            )
    except Exception as e:
        logger.error(f"Error al notificar al admin por Telegram: {e}")

# --- FUNCIÓN IA BLINDADA ---
def consultar_ia(historial, max_tokens_respuesta=150):
    """Consulta a la IA, intentando con modelos principal y de respaldo. Limita la longitud de la respuesta."""
    url_ia = "https://openrouter.ai/api/v1/chat/completions"
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logger.error("OPENROUTER_API_KEY no configurado.")
        return "Lo siento, mi sistema de inteligencia artificial no está disponible en este momento. Por favor, inténtelo más tarde."

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    modelos = [MODELO_PRINCIPAL, MODELO_RESPALDO]
    respuesta_ia = None

    for modelo in modelos:
        try:
            logger.sleep(1) # Pequeña pausa entre llamadas a modelos
            logger.info(f"Consultando IA con modelo: {modelo}")
            response = requests.post(
                url_ia,
                headers=headers,
                json={
                    "model": modelo,
                    "messages": historial,
                    "max_tokens": max_tokens_respuesta # Limita las tokens de respuesta
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            if 'choices' in data and data['choices']:
                respuesta_ia = data['choices'][0]['message']['content']
                if respuesta_ia:
                    logger.close() # Si se obtuvo respuesta, salimos del bucle
                    return respuesta_ia.strip()
            else:
                logger.warning(f"La IA ({modelo}) devolvió una respuesta vacía o sin 'choices'.")

        except requests.exceptions.RequestException as e:
            logger.warning(f"Error de red al consultar IA (modelo {modelo}): {e}")
        except Exception as e:
            logger.error(f"Error inesperado al consultar IA (modelo {modelo}): {e}", exc_info=True)

    logger.error("Todos los intentos de consulta a la IA fallaron.")
    return "Lo siento, mi sistema está experimentando una breve pausa. ¿Podrías escribirme de nuevo en un minuto? 🙏"

# --- FUNCIONES DE CLASIFICACIÓN Y BÚSQUEDA ---

def detectar_rol_y_intencion(mensaje_usuario: str, historial_conversacion: list) -> dict:
    """
    Usa IA para detectar si el usuario es un cliente o un colega inmobiliario
    y cuál es su intención principal (buscar propiedad, obtener info, etc.).
    """
    # Construimos un historial para la IA enfocado en la detección de rol e intención
    prompt_deteccion = f"""
    Eres un asistente de clasificación de usuarios para un chatbot inmobiliario.
    Analiza el siguiente mensaje y el historial de la conversación para determinar:
    1. EL ROL DEL USUARIO: 'cliente' o 'colega_inmobiliario'.
    2. LA INTENCIÓN PRINCIPAL: 'buscar_propiedad', 'pedir_ficha_completa', 'solicitar_captador', 'info_general', 'otro'.

    Prioriza la clasificación como 'colega_inmobiliario' si hay menciones de comisiones, captaciones, otros agentes, MLS, etc.
    Si la intención es buscar propiedades, intenta identificar zonas o filtros (tipo, precio, habitaciones).

    Formato de salida JSON (obligatorio):
    {{"rol": "...", "intencion": "...", "filtros": {{"zona": "...", "tipo": "...", "precio_max": "...", "habitaciones_min": "..."}} }}

    Ejemplo de colega: {"rol": "colega_inmobiliario", "intencion": "pedir_ficha_completa", "filtros": {}}
    Ejemplo de cliente buscando: {"rol": "cliente", "intencion": "buscar_propiedad", "filtros": {"zona": "Las Mercedes", "tipo": "apartamento", "precio_max": 150000}}
    Ejemplo de cliente pidiendo info: {"rol": "cliente", "intencion": "info_general", "filtros": {}}
    """

    mensajes_para_ia = [{"role": "system", "content": prompt_deteccion}]
    # Agregamos un resumen del historial reciente para contexto
    mensajes_para_ia.extend(historial_conversacion[-4:]) # Últimos 4 intercambios para contexto
    mensajes_para_ia.append({"role": "user", "content": f"Analiza este mensaje: \"{mensaje_usuario}\""})

    respuesta_raw = consultar_ia(mensajes_para_ia, max_tokens_respuesta=200) # Un poco más de tokens para el JSON

    try:
        # Buscamos el JSON dentro de la respuesta, ya que puede haber texto alrededor
        match = re.search(r'\{.*?\}', respuesta_raw, re.DOTALL)
        if match:
            respuesta_json = match.group(0)
            datos_extraccion = json.loads(respuesta_json)
            
            # Asegurar que los filtros que no se encuentran sean None o vacíos
            filtros = datos_extraccion.get("filtros", {})
            datos_extraccion["filtros"] = {
                "zona": filtros.get("zona", ""),
                "tipo": filtros.get("tipo", ""),
                "precio_max": filtros.get("precio_max", None),
                "habitaciones_min": filtros.get("habitaciones_min", None),
            }
            return datos_extraccion
        else:
            logger.warning(f"No se encontró JSON válido en la respuesta de detección de rol: {respuesta_raw}")
            # Si no detecta bien, asumimos cliente para ser conservadores
            return {"rol": "cliente", "intencion": "info_general", "filtros": {}}

    except json.JSONDecodeError:
        logger.error(f"Error al decodificar JSON de la IA para detección de rol: {respuesta_raw}")
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

def elegir_top_n_propiedades(propiedades_disponibles: list, intencion: dict, n: int = 3) -> list:
    """
    Filtra y ordena las propiedades basándose en la intención del usuario.
    Puede ser extendido para criterios más complejos de ranking.
    """
    filtros = intencion.get("filtros", {})
    zona_buscada = filtros.get("zona", "").lower()
    tipo_buscado = filtros.get("tipo", "").lower() # No se usa directamente en Wasi, pero puede ser útil para IA
    precio_max = filtros.get("precio_max")
    habitaciones_min = filtros.get("habitaciones_min")

    propiedades_filtradas = propiedades_disponibles

    # Aplicar filtros si están disponibles
    if zona_buscada:
        propiedades_filtradas = [
            p for p in propiedades_filtradas
            if zona_buscada in p.get('zona', '').lower() or zona_buscada in p.get('ciudad', '').lower()
        ]

    # El ranking se puede mejorar, por ahora es una prioridad general
    def score_propiedad(p):
        score = 0
        if p.get('zona', '') != 'N/D': score += 2
        if p.get('ciudad', '') != 'N/D': score += 1
        if p.get('venta', 'N/D') != 'N/D' or p.get('renta', 'N/D') != 'N/D': score += 1
        if p.get('area', 'N/D') != 'N/D': score += 1
        if p.get('habitaciones', 'N/D') != 'N/D': score += 2
        if p.get('banos', 'N/D') != 'N/D': score += 1
        return score

    propiedades_ordenadas = sorted(propiedades_filtradas, key=score_propiedad, reverse=True)

    # Aplicar filtros de precio y habitaciones si existen (después de ordenar por relevancia general)
    if precio_max is not None:
        # Necesitamos parsear el precio para comparar numéricamente
        # Esto es una simplificación, idealmente Wasi devuelve precios numéricos
        propiedades_ordenadas = [
            p for p in propiedades_ordenadas
            if parsear_precio(p.get('venta')) <= precio_max or parsear_precio(p.get('renta')) <= precio_max
        ]

    if habitaciones_min is not None:
        propiedades_ordenadas = [
            p for p in propiedades_ordenadas
            if p.get('habitaciones', 0) >= habitaciones_min
        ]

    return propiedades_ordenadas[:n]

def parsear_precio(precio_str: str) -> float:
    """Intenta convertir un string de precio a float (ej. $150,000.00 -> 150000.00)."""
    if not precio_str or precio_str.lower() == 'n/d': return 0.0
    precio_limpio = re.sub(r'[^\d.]', '', precio_str)
    try:
        return float(precio_limpio)
    except ValueError:
        return 0.0 # Si no se puede parsear, retornar 0

# --- LLAMADA CENTRAL DEL WEBHOOK ---
@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        # Validar la API Key
        if request.headers.get("x-api-key") not in os.getenv("API_KEYS_AGENTES", "").split(","):
            logger.error("Acceso denegado: API Key inválida.")
            raise HTTPException(status_code=403, detail="Acceso denegado")

        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = str(payload.get("sender", "")).strip()
        mensaje_cliente = str(payload.get("message", ""))
        
        if not mensaje_cliente.strip():
            logger.info("Mensaje vacío recibido.")
            return {"replies": []} # No hacer nada si el mensaje está vacío

        # --- Lógica principal del flujo ---
        
        # 1. Obtener inventario y datos de agentes/captadores (actualizar caché si es necesario)
        inventario_disponible = obtener_inventario_cache()
        sincronizar_google_sheet() # Asegura que sheets_cache esté actualizado
        
        # 2. Detectar Rol e Intención del usuario usando IA
        if sender not in memoria_conversaciones:
            memoria_conversaciones[sender] = []
        
        # Llamar a la IA para detectar rol e intención
        deteccion = detectar_rol_y_intencion(mensaje_cliente, memoria_conversaciones[sender])
        rol_usuario = deteccion.get("rol", "cliente") # Por defecto, asumimos cliente
        intencion_usuario = deteccion.get("intencion", "info_general")
        filtros_busqueda = deteccion.get("filtros", {})
        
        logger.info(f"Usuario {sender}: Rol='{rol_usuario}', Intención='{intencion_usuario}', Filtros={filtros_busqueda}")

        respuesta_final_chatbot = ""
        
        # --- SECCIÓN PARA CLIENTE ---
        if rol_usuario == "cliente":
            if intencion_usuario == "buscar_propiedad":
                propiedades_seleccionadas = elegir_top_n_propiedades(inventario_disponible, deteccion, n=3)
                
                if not propiedades_seleccionadas:
                    respuesta_final_chatbot = "¡Hola! 👋 Gracias por escribirnos. Por el momento no encuentro propiedades que se ajusten a tu búsqueda. ¿Te gustaría probar con otras zonas o filtros?"
                else:
                    # Formatear fichas para cliente
                    fichas_formateadas = [formatear_ficha_propiedad(p, es_colega=False) for p in propiedades_seleccionadas]
                    
                    # Construir texto de respuesta principal (máx 30 palabras)
                    texto_principal = "¡Hola! 👋 ¡Qué gusto atenderte! Encontré estas 3 opciones pensé que te encantarán. ✨"
                    
                    # Unir texto principal y fichas
                    respuesta_final_chatbot = texto_principal + "\n\n" + "\n\n".join(fichas_formateadas)
                    
                    # Aquí se podría añadir la lógica para preguntar por datos si el usuario muestra interés
                    # Por ahora, solo presentamos las propiedades
                    
            else: # Intención info_general, saludo, etc.
                # Usamos IA para generar la respuesta corta (max 30 palabras)
                prompt_respuesta_cliente = f"""
                Eres Paty, tu asistente de Mettryc Realty. Responde de forma cálida y amigable, máximo 30 palabras.
                Contexto: El usuario es un CLENTE. Su última interacción fue: "{mensaje_cliente}".
                Responde brevemente y pregunta algo para continuar la conversación.
                """
                historial_ia_cliente = [{"role": "system", "content": prompt_respuesta_cliente}] + memoria_conversaciones[sender][-4:] # Contexto
                respuesta_final_chatbot = consultar_ia(historial_ia_ia_cliente, max_tokens_respuesta=60) # 30 palabras ~ 60 tokens

        # --- SECCIÓN PARA COLEGA INMOBILIARIO ---
        elif rol_usuario == "colega_inmobiliario":
            if intencion_usuario == "pedir_ficha_completa" or intencion_usuario == "buscar_propiedad": # Colega pidiendo info específica
                propiedades_seleccionadas = elegir_top_n_propiedades(inventario_disponible, deteccion, n=3)
                
                if not propiedades_seleccionadas:
                    respuesta_final_chatbot = "Hola colega, revisé el inventario pero no encontré propiedades que se ajusten a tu búsqueda en este momento. ¿Podemos ayudarte en algo más?"
                else:
                    # Formatear fichas para colega (incluye captador)
                    fichas_formateadas = [formatear_ficha_propiedad(p, es_colega=True) for p in propiedades_seleccionadas]
                    
                    # Responder con las fichas (no hay límite de palabras aquí)
                    respuesta_final_chatbot = "¡Hola colega! 👋 Te comparto estas opciones de nuestro inventario:\n\n" + "\n\n".join(fichas_formateadas)
                    # NOTA: No se pide datos de lead a colegas ni se asigna agente. La notificación a telegram del admin es opcional.
                    
            else: # Colega con otra intención
                prompt_respuesta_colega = f"""
                Eres un asistente profesional para colegas inmobiliarios de Mettryc Realty. Responde de forma cordial y eficiente, máximo 30 palabras.
                Contexto: El usuario es un COLEGA INMOBILIARIO. Su última interacción fue: "{mensaje_cliente}".
                Responde brevemente.
                """
                historial_ia_colega = [{"role": "system", "content": prompt_respuesta_colega}] + memoria_conversaciones[sender][-4:]
                respuesta_final_chatbot = consultar_ia(historial_ia_colega, max_tokens_respuesta=60)

        # --- Lógica de Captura y Asignación (solo para Clientes que deben completar datos) ---
        # Esta parte es una SIMPLIFICACIÓN. Un sistema robusto requeriría un flujo de conversación más elaborado para pedir datos.
        # Asumimos que si el usuario da nombre y correo (o al menos nombre completo) en un mensaje posterior a recibir propiedades, intentamos capturar.
        if rol_usuario == "cliente" and intencion_usuario == "buscar_propiedad" and sender not in clientes_procesados:
            # Intenta extraer datos si el mensaje parece contendiente de ellos (rudimentario)
            nombre_match = re.search(r"(?:mi nombre es|soy|me llamo)\s+([A-Za-zÀ-ÖØ-öø-ÿ]+\s+[A-Za-zÀ-ÖØ-öø-ÿ]+)", mensaje_cliente, re.IGNORECASE)
            correo_match = re.search(r"([\w\.-]+@[\w\.-]+)", mensaje_cliente, re.IGNORECASE)
            telefono_match = re.search(r"(?:mi telefono es|mi wsp es|tel:|cel:)\s*([\+?\d\s()-]+)", mensaje_cliente, re.IGNORECASE) # Buscamos algo que parezca teléfono

            datos_lead_dict = {}
            if nombre_match:
                datos_lead_dict["nombre"] = nombre_match.group(1).strip()
            if correo_match:
                datos_lead_dict["correo"] = correo_match.group(1).strip()
            if telefono_match:
                datos_lead_dict["telefono"] = telefono_match.group(1).strip()
            elif sender.isdigit() and len(sender) > 5: # Asumir que el remitente es teléfono si es numérico
                 datos_lead_dict["telefono"] = sender

            # Si tenemos Nombre y Correo, intentamos asignar
            if "nombre" in datos_lead_dict and "correo" in datos_lead_dict:
                # Asegurarse de que el nombre tenga al menos dos palabras
                if len(datos_lead_dict["nombre"].split()) < 2:
                    logger.warning("Nombre incompleto, pidiendo más datos.")
                    respuesta_a_enviar = f"¡Excelente elección! Me entusiasma ayudarte. Para registrar tu ficha VIP y asignarte el asesor, por favor confírmame también tu Apellido. 😊"
                else:
                    # Asignar agente y notificar
                    agente_asignado = asignar_agente_round_robin()
                    if agente_asignado:
                        enviar_notificaciones_telegram(agente_asignado, datos_lead_dict.get("telefono", sender), {
                            "Nombre": datos_lead_dict["nombre"],
                            "Correo": datos_lead_dict["correo"],
                            "Telefono": datos_lead_dict.get("telefono", "No proporcionado"),
                            "Origen": "Chatbot Inmobiliario"
                        })
                        clientes_procesados.add(sender) # Marcar como procesado para no re-asignar
                        
                        # Respuesta final al cliente con confirmación
                        respuesta_a_enviar = (
                            f"¡Perfecto, {datos_lead_dict['nombre'].split()[0]}! ✨"
                            f" He registrado tus datos en nuestro sistema premium. "
                            f"Nuestro asesor especializado, *{agente_asignado['nombre']}*, se pondrá en contacto contigo de inmediato para una atención VIP. "
                            f"¡Gracias por elegir Mettryc Realty! 🤝"
                        )
                    else:
                        respuesta_a_enviar = "¡Genial! Hemos registrado tu interés. Estamos asignando un asesor para tu atención inmediata. Por favor, espera un momento."
                
                respuesta_final_chatbot = respuesta_a_enviar # Sobrescribe la respuesta si se capturó el lead

        # --- ACTUALIZAR MEMORIA ---
        # Guardamos solo el último mensaje del usuario y la respuesta del bot
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_final_chatbot})
        
        # Limitar memoria para no sobrecargar
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        # --- RETORNAR RESPUESTA ---
        # Garantiza que la respuesta final no tenga dobles asteriscos (esto es más una salvaguarda)
        respuesta_limpia = respuesta_final_chatbot.replace("**", "*")

        return {"replies": [{"message": respuesta_limpia}]}

    except HTTPException as e:
        raise e # Re-lanzar errores de HTTP
    except Exception as e:
        logger.error(f"Error crítico en /webhook: {e}", exc_info=True)
        # Si ocurre un error general, devolver un mensaje genérico de error
        return {"replies": [{"message": "Lo siento, estamos procesando tu solicitud. Por favor, escribe de nuevo."}]}

```
