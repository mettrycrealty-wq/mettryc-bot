import os
import requests
import logging
import time
import re
import json
import unicodedata
import threading
import hashlib
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

ZONA_HORARIA_LOCAL = ZoneInfo("America/Caracas")

INTERVALO_ACTUALIZACION_SHEETS = timedelta(hours=1)
HORAS_ACTUALIZACION_INVENTARIO = {0, 12}

VENTANA_DEDUPLICACION_SEGUNDOS = 20

# ============================================================
# MODELOS IA ECONÓMICOS POR TAREA
# ============================================================

MODELOS_IA = {
    "analisis": {
        "principal": "google/gemini-2.5-flash-lite",
        "respaldo": "openai/gpt-4o-mini",
        "max_tokens": 350,
        "temperature": 0.1
    },
    "redaccion": {
        "principal": "google/gemini-2.5-flash-lite",
        "respaldo": "openai/gpt-4o-mini",
        "max_tokens": 120,
        "temperature": 0.65
    },
    "critico": {
        "principal": "openai/gpt-4o-mini",
        "respaldo": "google/gemini-2.0-flash-001",
        "max_tokens": 250,
        "temperature": 0.2
    },
    "resumen": {
        "principal": "google/gemini-2.5-flash-lite",
        "respaldo": "openai/gpt-4o-mini",
        "max_tokens": 180,
        "temperature": 0.2
    }
}

# Compatibilidad con nombres anteriores
MODELO_PRINCIPAL = MODELOS_IA["analisis"]["principal"]
MODELO_RESPALDO = MODELOS_IA["analisis"]["respaldo"]

# ============================================================
# CACHÉ, LOCKS Y MEMORIA
# ============================================================

cache = {
    "inventario": [],
    "ultima_actualizacion": None,
    "slot_actualizacion": None,
    "actualizando": False
}

sheets_cache = {
    "agentes": [],
    "captadores": {},
    "ultimo_indice": -1,
    "ultima_actualizacion": datetime.min.replace(tzinfo=ZONA_HORARIA_LOCAL)
}

memoria_conversaciones = {}
clientes_procesados = set()
estado_usuarios = {}

mensajes_procesados = {}

lock_inventario = threading.Lock()
lock_sheets = threading.Lock()
lock_deduplicacion = threading.Lock()

# ============================================================
# ESTADOS CONVERSACIONALES
# ============================================================

ESTADO_INDAGANDO = "indagando_necesidad"
ESTADO_COMPLETANDO_FILTROS = "completando_filtros"
ESTADO_DEFINIENDO_ROL = "definiendo_rol"
ESTADO_MOSTRANDO_PROPIEDADES = "mostrando_propiedades"
ESTADO_CAPTURANDO_LEAD = "capturando_lead"
ESTADO_LEAD_COMPLETO = "lead_completo"

# ============================================================
# NORMALIZACIÓN Y UTILIDADES
# ============================================================

STOPWORDS_ZONA = {
    "el", "la", "los", "las", "de", "del", "en", "y",
    "urb", "urbanizacion", "urbanización", "sector", "zona"
}

CALIFICADORES_ZONA = {
    "norte", "sur", "este", "oeste", "centro", "central",
    "alto", "alta", "bajo", "baja"
}

CIUDADES_COMUNES = {
    "valencia", "carabobo", "naguanagua", "san", "diego",
    "guacara", "tocuyito", "maracay", "caracas"
}

def ahora_local():
    return datetime.now(ZONA_HORARIA_LOCAL)

def normalizar_texto(texto: str) -> str:
    if not texto:
        return ""

    texto = str(texto).lower().strip()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()

    return texto

def tokens_relevantes(texto: str) -> set:
    texto_norm = normalizar_texto(texto)
    return {
        t for t in texto_norm.split()
        if t not in STOPWORDS_ZONA and len(t) > 1
    }

def zona_coincide(zona_buscada: str, zona_propiedad: str, ciudad_propiedad: str = "") -> bool:
    buscada_norm = normalizar_texto(zona_buscada)
    zona_norm = normalizar_texto(zona_propiedad)
    ciudad_norm = normalizar_texto(ciudad_propiedad)

    if not buscada_norm:
        return True

    tokens_busqueda = tokens_relevantes(buscada_norm)
    tokens_zona_propiedad = tokens_relevantes(zona_norm)
    tokens_ciudad = tokens_relevantes(ciudad_norm)
    tokens_propiedad_total = tokens_zona_propiedad.union(tokens_ciudad)

    if not tokens_busqueda:
        return True

    calificadores_buscados = tokens_busqueda.intersection(CALIFICADORES_ZONA)

    if calificadores_buscados:
        if not calificadores_buscados.issubset(tokens_zona_propiedad):
            return False

    tokens_clave_busqueda = tokens_busqueda - CIUDADES_COMUNES

    if len(tokens_clave_busqueda) >= 2:
        return tokens_clave_busqueda.issubset(tokens_zona_propiedad)

    if len(tokens_clave_busqueda) == 1:
        token = list(tokens_clave_busqueda)[0]
        return token in tokens_zona_propiedad

    return bool(tokens_busqueda.intersection(tokens_propiedad_total))

def convertir_entero_seguro(valor) -> int:
    try:
        if valor in [None, "", "N/D"]:
            return 0
        return int(float(valor))
    except Exception:
        return 0

def limpiar_telefono(valor: str) -> str:
    return re.sub(r"\D", "", str(valor or ""))

def actualizar_memoria(sender: str, mensaje_usuario: str, respuesta_bot: str):
    if sender not in memoria_conversaciones:
        memoria_conversaciones[sender] = []

    memoria_conversaciones[sender].append({
        "role": "user",
        "content": mensaje_usuario
    })

    memoria_conversaciones[sender].append({
        "role": "assistant",
        "content": respuesta_bot
    })

    if len(memoria_conversaciones[sender]) > 20:
        memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]

def obtener_estado_usuario(sender: str) -> dict:
    if sender not in estado_usuarios:
        estado_usuarios[sender] = {
            "estado": ESTADO_INDAGANDO,
            "rol": None,
            "filtros": {
                "tipo_propiedad": "",
                "tipo_operacion": "",
                "zona": "",
                "presupuesto": None,
                "habitaciones_min": None,
                "banos_min": None,
                "caracteristicas": []
            },
            "propiedades_enviadas": [],
            "ultimas_propiedades_mostradas": [],
            "propiedad_interes": None,
            "lead": {
                "nombre": "",
                "correo": "",
                "whatsapp": ""
            }
        }

    return estado_usuarios[sender]

def mensaje_es_saludo_simple(mensaje: str) -> bool:
    texto = normalizar_texto(mensaje)

    saludos = {
        "hola",
        "buenas",
        "buenos dias",
        "buenas tardes",
        "buenas noches",
        "saludos",
        "hello",
        "hi"
    }

    return texto in saludos

def limpiar_respuesta_whatsapp(texto: str) -> str:
    return str(texto or "").replace("**", "*").strip()

# ============================================================
# DEDUPLICACIÓN DE WEBHOOKS
# ============================================================

def obtener_message_id_payload(payload: dict) -> str:
    posibles_campos = [
        "message_id",
        "messageId",
        "id",
        "wamid",
        "whatsapp_message_id",
        "messageID"
    ]

    for campo in posibles_campos:
        valor = payload.get(campo)
        if valor:
            return str(valor).strip()

    return ""

def generar_clave_deduplicacion(payload: dict, sender: str, mensaje: str) -> str:
    message_id = obtener_message_id_payload(payload)

    if message_id:
        return f"id:{sender}:{message_id}"

    base = f"{sender}:{normalizar_texto(mensaje)}"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()

    return f"hash:{digest}"

def es_mensaje_duplicado(payload: dict, sender: str, mensaje: str) -> bool:
    ahora = time.time()
    clave = generar_clave_deduplicacion(payload, sender, mensaje)

    with lock_deduplicacion:
        expiradas = [
            k for k, ts in mensajes_procesados.items()
            if ahora - ts > VENTANA_DEDUPLICACION_SEGUNDOS
        ]

        for k in expiradas:
            mensajes_procesados.pop(k, None)

        if clave in mensajes_procesados:
            logger.warning(f"Mensaje duplicado ignorado: {clave}")
            return True

        mensajes_procesados[clave] = ahora
        return False

# ============================================================
# PRECIOS
# ============================================================

def parsear_precio(precio_str) -> float:
    if precio_str in [None, "", "N/D"]:
        return 0.0

    texto = str(precio_str).strip().lower()

    if texto in ["n/d", "nd", "none", "null", "0", "0.0"]:
        return 0.0

    texto = re.sub(r"[^\d.,]", "", texto)

    if not texto:
        return 0.0

    if "." in texto and "," in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "." in texto:
        partes = texto.split(".")
        if len(partes[-1]) == 3 and len(partes) > 1:
            texto = texto.replace(".", "")
    elif "," in texto:
        partes = texto.split(",")
        if len(partes[-1]) == 3 and len(partes) > 1:
            texto = texto.replace(",", "")
        else:
            texto = texto.replace(",", ".")

    try:
        return float(texto)
    except ValueError:
        logger.warning(f"No se pudo parsear precio: {precio_str}")
        return 0.0

def parsear_precio_wasi(valor_numerico=None, valor_label=None) -> float:
    if valor_numerico not in [None, "", "N/D"]:
        try:
            return float(valor_numerico)
        except Exception:
            pass

    return parsear_precio(valor_label)

def parsear_presupuesto_texto(texto: str):
    if not texto:
        return None

    texto_original = str(texto).lower().strip()

    # Ejemplos: 200.000 / 200,000 / 1.500.000
    match_miles = re.search(r"(\d{1,3}(?:[.,]\d{3})+)", texto_original)

    if match_miles:
        numero = match_miles.group(1)
        numero = numero.replace(".", "").replace(",", "")

        try:
            return float(numero)
        except Exception:
            pass

    texto_norm = normalizar_texto(texto)

    # Ejemplos: 200 mil / 200k
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(mil|k)", texto_norm)

    if match:
        numero = float(match.group(1).replace(",", "."))
        return numero * 1000

    # Ejemplos: 1.5 millones / 2 millones
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(millon|millones|mm)", texto_norm)

    if match:
        numero = float(match.group(1).replace(",", "."))
        return numero * 1000000

    # Ejemplo: 200000
    match = re.search(r"(?:usd|us|\$)?\s*(\d{4,9})", texto_norm)

    if match:
        return float(match.group(1))

    return None

# ============================================================
# INVENTARIO WASI
# ============================================================

def obtener_slot_actual_inventario():
    ahora = ahora_local()

    if ahora.hour < 12:
        hora_slot = 0
    else:
        hora_slot = 12

    slot = ahora.replace(
        hour=hora_slot,
        minute=0,
        second=0,
        microsecond=0
    )

    return slot.strftime("%Y-%m-%d %H:%M")

def obtener_inventario_desde_wasi():
    propiedades = []
    take = 100
    skip = 0

    logger.info("Iniciando descarga completa de propiedades ACTIVAS desde Wasi...")

    while True:
        params = {
            "wasi_token": os.getenv("WASI_TOKEN"),
            "id_company": os.getenv("WASI_COMPANY_ID"),
            "take": take,
            "skip": skip,
            "status": 1
        }

        exito_pagina = False
        intentos = 0

        while intentos < 3 and not exito_pagina:
            try:
                response = requests.get(
                    "https://api.wasi.co/v1/property/search",
                    params=params,
                    timeout=60
                )
                response.raise_for_status()
                data = response.json()

                contador_pagina = 0

                for key, value in data.items():
                    if isinstance(value, dict) and key.isdigit():
                        contador_pagina += 1

                        id_prop = value.get("id_property")
                        enlace_web = f"https://www.mettryc.com/inmueble/{id_prop}"

                        user_data = value.get("user_data", {})
                        captador_nombre = (
                            f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}"
                        ).strip() or "Asesor Mettryc"

                        captador_telefono = user_data.get("phone", "")

                        propiedad = {
                            "id": str(id_prop),
                            "titulo": value.get("title", "Propiedad sin título"),
                            "ciudad": value.get("city_label", "N/D"),
                            "zona": value.get("zone_label", "N/D"),
                            "venta": value.get("sale_price_label", "N/D"),
                            "renta": value.get("rent_price_label", "N/D"),
                            "precio_venta_float": parsear_precio_wasi(
                                value.get("sale_price"),
                                value.get("sale_price_label")
                            ),
                            "precio_renta_float": parsear_precio_wasi(
                                value.get("rent_price"),
                                value.get("rent_price_label")
                            ),
                            "area": value.get("area", "N/D"),
                            "habitaciones": value.get("bedrooms", "N/D"),
                            "banos": value.get("bathrooms", "N/D"),
                            "tipo_propiedad_wasi": value.get("type_label", "Indefinido"),
                            "enlace": enlace_web,
                            "captador_propiedad": captador_nombre,
                            "telefono_captador_wasi": captador_telefono
                        }

                        propiedades.append(propiedad)

                exito_pagina = True
                logger.info(
                    f"Descargadas {contador_pagina} propiedades. "
                    f"Total acumulado: {len(propiedades)}"
                )

                if contador_pagina < take:
                    logger.info(f"Descarga completa desde Wasi. Total: {len(propiedades)}")
                    return propiedades

                skip += take
                time.sleep(2)

            except Exception as e:
                intentos += 1
                logger.warning(f"Intento {intentos}/3 fallido obteniendo inventario: {e}")
                time.sleep(5)

        if not exito_pagina:
            logger.error("No se pudo completar descarga de inventario.")
            break

    return propiedades

def actualizar_inventario_si_corresponde(forzar: bool = False):
    slot_actual = obtener_slot_actual_inventario()

    if (
        not forzar
        and cache.get("slot_actualizacion") == slot_actual
        and cache.get("inventario")
    ):
        logger.info(
            f"Inventario vigente en memoria. Slot: {slot_actual}. "
            f"Propiedades: {len(cache['inventario'])}"
        )
        return cache["inventario"]

    with lock_inventario:
        slot_actual = obtener_slot_actual_inventario()

        if (
            not forzar
            and cache.get("slot_actualizacion") == slot_actual
            and cache.get("inventario")
        ):
            logger.info("Otro request ya actualizó el inventario para este slot.")
            return cache["inventario"]

        try:
            cache["actualizando"] = True
            logger.info(
                f"Actualizando inventario desde Wasi. "
                f"Forzar={forzar}. Slot={slot_actual}"
            )

            inventario_nuevo = obtener_inventario_desde_wasi()

            if inventario_nuevo:
                cache["inventario"] = inventario_nuevo
                cache["ultima_actualizacion"] = ahora_local()
                cache["slot_actualizacion"] = slot_actual

                logger.info(
                    f"Inventario actualizado correctamente con "
                    f"{len(inventario_nuevo)} propiedades. Slot={slot_actual}"
                )
            else:
                logger.warning(
                    "Wasi devolvió inventario vacío. Se mantiene inventario anterior."
                )

        except Exception as e:
            logger.error(
                f"Error actualizando inventario desde Wasi: {e}",
                exc_info=True
            )

        finally:
            cache["actualizando"] = False

    return cache["inventario"]

def obtener_inventario():
    """
    Devuelve inventario desde memoria.
    No descarga por saludo ni por conversación.
    Solo actualiza al iniciar, deploy/reinicio o cuando cambia el slot 12 a.m. / 12 p.m.
    """
    if cache.get("inventario"):
        slot_actual = obtener_slot_actual_inventario()

        if cache.get("slot_actualizacion") == slot_actual:
            return cache["inventario"]

    return actualizar_inventario_si_corresponde(forzar=False)

def scheduler_inventario_background():
    logger.info("Scheduler de inventario iniciado.")

    while True:
        try:
            actualizar_inventario_si_corresponde(forzar=False)
        except Exception as e:
            logger.error(
                f"Error en scheduler de inventario: {e}",
                exc_info=True
            )

        time.sleep(300)

# ============================================================
# GOOGLE SHEETS
# ============================================================

def sincronizar_google_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL")

    if not script_url:
        logger.warning("GOOGLE_SHEET_TURNOS_URL no configurada.")
        return

    ahora = ahora_local()

    necesita_actualizar = (
        ahora - sheets_cache["ultima_actualizacion"] > INTERVALO_ACTUALIZACION_SHEETS
        or not sheets_cache["agentes"]
        or not sheets_cache["captadores"]
    )

    if not necesita_actualizar:
        return

    with lock_sheets:
        ahora = ahora_local()

        necesita_actualizar_dentro_lock = (
            ahora - sheets_cache["ultima_actualizacion"] > INTERVALO_ACTUALIZACION_SHEETS
            or not sheets_cache["agentes"]
            or not sheets_cache["captadores"]
        )

        if not necesita_actualizar_dentro_lock:
            return

        try:
            response = requests.get(script_url, timeout=15)
            response.raise_for_status()
            payload_sheet = response.json()

            if isinstance(payload_sheet, dict):
                sheets_cache["agentes"] = payload_sheet.get("agentes", [])
                sheets_cache["captadores"] = {
                    str(k).strip(): str(v).strip()
                    for k, v in payload_sheet.get("captadores", {}).items()
                    if str(k).strip() and str(v).strip()
                }
                sheets_cache["ultima_actualizacion"] = ahora_local()

                logger.info(
                    f"✅ Sincronizados {len(sheets_cache['agentes'])} agentes "
                    f"y {len(sheets_cache['captadores'])} captadores."
                )
            else:
                logger.warning("Formato inesperado desde Google Sheets.")

        except Exception as e:
            logger.error(f"Error sincronizando Google Sheets: {e}")

def asignar_agente_round_robin():
    sincronizar_google_sheet()

    lista_agentes = sheets_cache["agentes"]

    if not lista_agentes:
        logger.warning("No hay agentes disponibles en Google Sheets.")
        return None

    sheets_cache["ultimo_indice"] = (
        sheets_cache["ultimo_indice"] + 1
    ) % len(lista_agentes)

    agente = lista_agentes[sheets_cache["ultimo_indice"]]

    logger.info(f"Agente asignado: {agente.get('nombre', 'Sin nombre')}")

    return agente

def obtener_telefono_captador_de_sheet(nombre_captador: str) -> str:
    if not sheets_cache["captadores"]:
        sincronizar_google_sheet()

    if not nombre_captador:
        return "N/D"

    nombre_norm = normalizar_texto(nombre_captador)

    for nombre_sheet, telefono in sheets_cache["captadores"].items():
        if normalizar_texto(nombre_sheet) == nombre_norm:
            return telefono

    tokens_wasi = tokens_relevantes(nombre_captador)

    mejor_telefono = None
    mejor_score = 0

    for nombre_sheet, telefono in sheets_cache["captadores"].items():
        tokens_sheet = tokens_relevantes(nombre_sheet)
        score = len(tokens_wasi.intersection(tokens_sheet))

        if score > mejor_score:
            mejor_score = score
            mejor_telefono = telefono

    if mejor_score >= 2:
        return mejor_telefono

    logger.warning(f"No se encontró teléfono de captador para: {nombre_captador}")
    return "N/D"

# ============================================================
# OPENROUTER IA
# ============================================================

def consultar_ia(historial, tarea="analisis", max_tokens=None, temperature=None, fallback=""):
    url_ia = "https://openrouter.ai/api/v1/chat/completions"
    api_key = os.getenv("OPENROUTER_API_KEY")

    if not api_key:
        logger.error("OPENROUTER_API_KEY no configurada.")
        return fallback or ""

    config = MODELOS_IA.get(tarea, MODELOS_IA["analisis"])

    modelos = [
        config["principal"],
        config["respaldo"]
    ]

    max_tokens_final = max_tokens or config.get("max_tokens", 250)
    temperature_final = temperature if temperature is not None else config.get("temperature", 0.2)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://www.mettryc.com",
        "X-Title": "Mettryc Realty Chatbot"
    }

    for modelo in modelos:
        try:
            logger.info(
                f"Consultando IA | tarea={tarea} | modelo={modelo} | "
                f"max_tokens={max_tokens_final} | temperature={temperature_final}"
            )

            response = requests.post(
                url_ia,
                headers=headers,
                json={
                    "model": modelo,
                    "messages": historial,
                    "max_tokens": max_tokens_final,
                    "temperature": temperature_final
                },
                timeout=25
            )

            response.raise_for_status()
            data = response.json()

            choices = data.get("choices", [])

            if choices:
                contenido = choices[0].get("message", {}).get("content", "")

                if contenido and contenido.strip():
                    return contenido.strip()

            logger.warning(f"IA devolvió respuesta vacía con modelo {modelo}")

        except Exception as e:
            logger.warning(f"Error consultando IA con {modelo}: {e}")

    return fallback or ""

def analizar_respuesta_usuario(mensaje_usuario: str, estado: dict) -> dict:
    estado_resumido = {
        "estado": estado.get("estado"),
        "rol": estado.get("rol"),
        "filtros": estado.get("filtros"),
        "lead": {
            "tiene_nombre": bool(estado.get("lead", {}).get("nombre")),
            "tiene_correo": bool(estado.get("lead", {}).get("correo")),
            "tiene_whatsapp": bool(estado.get("lead", {}).get("whatsapp"))
        }
    }

    prompt = f"""
Analiza mensaje inmobiliario. Devuelve solo JSON válido. No inventes.

Estado:
{json.dumps(estado_resumido, ensure_ascii=False)}

Mensaje:
"{mensaje_usuario}"

JSON:
{{
 "filtros_detectados": {{
  "tipo_propiedad": "",
  "tipo_operacion": "",
  "zona": "",
  "presupuesto": null,
  "habitaciones_min": null,
  "banos_min": null,
  "caracteristicas": []
 }},
 "rol_detectado": "cliente|colega_inmobiliario|desconocido",
 "pide_mas_opciones": false,
 "muestra_interes": false,
 "quiere_agendar": false,
 "datos_contacto": {{
  "nombre": "",
  "correo": "",
  "whatsapp": ""
 }},
 "objecion_detectada": "",
 "emocion_detectada": "",
 "confianza_extraccion": 0.0
}}
"""

    respuesta_raw = consultar_ia(
        [{"role": "system", "content": prompt}],
        tarea="analisis",
        fallback="{}"
    )

    try:
        start = respuesta_raw.find("{")
        end = respuesta_raw.rfind("}")

        if start != -1 and end != -1:
            return json.loads(respuesta_raw[start:end + 1])

    except Exception as e:
        logger.warning(f"No se pudo parsear análisis IA: {e}")

    return {}

def fusionar_filtros(filtros_actuales: dict, filtros_nuevos: dict) -> dict:
    filtros = filtros_actuales.copy()

    for campo in ["tipo_propiedad", "tipo_operacion", "zona"]:
        valor = filtros_nuevos.get(campo)
        if valor:
            filtros[campo] = str(valor).strip()

    for campo in ["presupuesto", "habitaciones_min", "banos_min"]:
        valor = filtros_nuevos.get(campo)

        if valor not in [None, "", []]:
            try:
                filtros[campo] = float(valor) if campo == "presupuesto" else int(valor)
            except Exception:
                pass

    nuevas_caracteristicas = filtros_nuevos.get("caracteristicas", [])

    if isinstance(nuevas_caracteristicas, list) and nuevas_caracteristicas:
        actuales = filtros.get("caracteristicas", [])
        filtros["caracteristicas"] = list(dict.fromkeys(actuales + nuevas_caracteristicas))

    tipo_op = normalizar_texto(filtros.get("tipo_operacion", ""))

    if tipo_op in ["comprar", "compra", "venta", "vendo"]:
        filtros["tipo_operacion"] = "venta"
    elif tipo_op in ["renta", "alquiler", "alquilar", "arrendamiento"]:
        filtros["tipo_operacion"] = "alquiler"

    return filtros

# ============================================================
# REDACCIÓN HUMANA Y PLANTILLAS
# ============================================================

PLANTILLAS_HUMANAS = {
    "tipo_propiedad": [
        "¡Hola! 👋 Qué gusto saludarte. Soy Paty de Mettryc Realty 😊\n\nCuéntame, ¿qué tipo de propiedad estás buscando?",
        "¡Hola! Bienvenido a Mettryc Realty 😊 Para ayudarte bien, cuéntame: ¿buscas casa, apartamento, local u otro tipo de propiedad?"
    ],
    "tipo_operacion": [
        "¡Qué buena elección! 🏡 ¿La buscas para comprar o alquilar?",
        "Perfecto, ya voy entendiendo lo que necesitas 😊 ¿Sería para venta o alquiler?"
    ],
    "zona": [
        "Excelente. Para enfocarme en opciones que realmente te sirvan, ¿en qué zona te gustaría buscar?",
        "Muy bien 😊 ¿Qué zona, urbanización o ciudad tienes en mente?"
    ],
    "presupuesto": [
        "Perfecto. Para mostrarte opciones realistas y bien filtradas, ¿cuál sería tu presupuesto máximo aproximado?",
        "Genial. Así puedo afinar mejor la búsqueda: ¿qué presupuesto máximo tienes en mente?"
    ],
    "caracteristicas": [
        "Buenísimo. ¿Qué característica es indispensable para ti? Por ejemplo: habitaciones, patio, estacionamiento o vigilancia.",
        "Perfecto 😊 ¿Hay algo que no pueda faltar en la propiedad?"
    ],
    "rol": [
        "Gracias, ya tengo la información base 😊 Una pregunta rápida: ¿la propiedad la buscas para ti o para un cliente?"
    ]
}

def obtener_plantilla_humana(campo: str) -> str:
    opciones = PLANTILLAS_HUMANAS.get(campo, [])
    if not opciones:
        return "Cuéntame un poco más para ayudarte mejor 😊"
    return random.choice(opciones)

def construir_pregunta_campo(campo: str, es_inicio=False) -> str:
    if es_inicio and campo == "tipo_propiedad":
        return obtener_plantilla_humana("tipo_propiedad")

    return obtener_plantilla_humana(campo)

def construir_pregunta_rol() -> str:
    return obtener_plantilla_humana("rol")

def redactar_respuesta_humana(objetivo: str, pregunta_base: str, estado: dict, mensaje_usuario: str) -> str:
    contexto = {
        "rol": estado.get("rol"),
        "estado": estado.get("estado"),
        "filtros": estado.get("filtros")
    }

    prompt = f"""
Eres Paty, asesora VIP de Mettryc Realty.

Redacta una respuesta de WhatsApp:
- Humana, cálida y comercial.
- Máximo 45 palabras.
- Una sola pregunta.
- No digas que eres IA.
- No uses doble asterisco.
- Incluye la pregunta obligatoria.

Objetivo:
{objetivo}

Contexto:
{json.dumps(contexto, ensure_ascii=False)}

Último mensaje:
"{mensaje_usuario}"

Pregunta obligatoria:
{pregunta_base}

Respuesta:
"""

    respuesta = consultar_ia(
        [{"role": "system", "content": prompt}],
        tarea="redaccion",
        fallback=pregunta_base
    )

    return limpiar_respuesta_whatsapp(respuesta or pregunta_base)

def debe_usar_ia_redaccion(analisis: dict, estado: dict) -> bool:
    if analisis.get("objecion_detectada"):
        return True

    if analisis.get("emocion_detectada") in ["molestia", "duda", "desconfianza", "ansiedad"]:
        return True

    if estado.get("estado") in [ESTADO_CAPTURANDO_LEAD, ESTADO_LEAD_COMPLETO]:
        return True

    return False

# ============================================================
# EXTRACCIÓN DE FILTROS
# ============================================================

def extraer_filtros_regex(mensaje_usuario: str, filtros: dict) -> dict:
    filtros = filtros.copy()
    texto = normalizar_texto(mensaje_usuario)

    tipos = [
        "casa", "apartamento", "local", "terreno", "oficina",
        "galpon", "galpón", "townhouse", "penthouse", "quinta",
        "anexo", "consultorio"
    ]

    for tipo in tipos:
        if tipo in texto and not filtros.get("tipo_propiedad"):
            filtros["tipo_propiedad"] = "galpón" if tipo == "galpon" else tipo
            break

    if not filtros.get("tipo_operacion"):
        if any(p in texto for p in ["venta", "comprar", "compra", "adquirir"]):
            filtros["tipo_operacion"] = "venta"
        elif any(p in texto for p in ["alquiler", "alquilar", "renta", "arrendamiento"]):
            filtros["tipo_operacion"] = "alquiler"

    presupuesto_detectado = parsear_presupuesto_texto(mensaje_usuario)

    if presupuesto_detectado:
        presupuesto_actual = filtros.get("presupuesto")

        if presupuesto_actual is None:
            filtros["presupuesto"] = presupuesto_detectado
        elif presupuesto_detectado > presupuesto_actual * 10:
            filtros["presupuesto"] = presupuesto_detectado

    actuales = filtros.get("caracteristicas", [])

    if filtros.get("habitaciones_min") is None:
        match_hab = re.search(r"(\d+)\s*(hab|habitacion|habitaciones|cuarto|cuartos)", texto)
        if match_hab:
            habitaciones = int(match_hab.group(1))
            filtros["habitaciones_min"] = habitaciones
            car = f"{habitaciones} habitaciones"
            if car not in actuales:
                actuales.append(car)

    if filtros.get("banos_min") is None:
        match_banos = re.search(r"(\d+)\s*(bano|banos|baño|baños)", texto)
        if match_banos:
            banos = int(match_banos.group(1))
            filtros["banos_min"] = banos
            car = f"{banos} baños"
            if car not in actuales:
                actuales.append(car)

    if not filtros.get("zona"):
        zona_match = re.search(
            r"(?:en|zona|urbanizacion|urbanización)\s+(.+?)(?:\s+(?:venta|alquiler|hasta|con|presupuesto|de\s+\d)|$)",
            mensaje_usuario,
            re.IGNORECASE
        )

        if zona_match:
            zona = zona_match.group(1).strip()
            zona = re.sub(r"\s+", " ", zona)
            if len(zona.split()) <= 8:
                filtros["zona"] = zona

    caracteristicas_clave = [
        "patio", "terraza", "jardin", "jardín", "piscina", "vigilancia",
        "seguridad", "estacionamiento", "garage", "garaje", "maletero",
        "planta baja", "remodelado", "amoblado", "ascensor", "pozo",
        "tanque", "calle cerrada", "conjunto cerrado", "parrillera",
        "family room", "estudio", "deposito", "depósito"
    ]

    for car in caracteristicas_clave:
        if normalizar_texto(car) in texto and car not in actuales:
            actuales.append(car)

    if any(p in texto for p in ["sin caracteristicas", "sin características", "ninguna", "no tengo", "nada especial"]):
        if not actuales:
            actuales.append("sin características adicionales")

    filtros["caracteristicas"] = actuales

    return filtros

def extraer_filtros_busqueda(mensaje_usuario: str, filtros_actuales: dict, estado: dict) -> dict:
    filtros_antes = filtros_actuales.copy()
    filtros_regex = extraer_filtros_regex(mensaje_usuario, filtros_actuales)

    # Si regex logró extraer algo útil, evitamos IA para ahorrar tokens.
    if filtros_regex != filtros_antes:
        return filtros_regex

    # Si no hubo cambio y el mensaje no es simple, usamos IA económica.
    analisis = analizar_respuesta_usuario(mensaje_usuario, estado)
    filtros_ia = analisis.get("filtros_detectados", {})

    filtros = fusionar_filtros(filtros_regex, filtros_ia)

    # Regex final para corregir posibles errores de IA, especialmente presupuesto.
    filtros = extraer_filtros_regex(mensaje_usuario, filtros)

    return filtros

def obtener_siguiente_campo_faltante(filtros: dict):
    if not filtros.get("tipo_propiedad"):
        return "tipo_propiedad"

    if not filtros.get("tipo_operacion"):
        return "tipo_operacion"

    if not filtros.get("zona"):
        return "zona"

    if filtros.get("presupuesto") is None:
        return "presupuesto"

    if not filtros.get("caracteristicas"):
        return "caracteristicas"

    return None

# ============================================================
# DETECCIÓN DE ROL
# ============================================================

def detectar_rol_por_respuesta_directa(mensaje: str) -> str:
    texto = normalizar_texto(mensaje)

    patrones_colega = [
        "para un cliente",
        "para mi cliente",
        "soy asesor",
        "soy agente",
        "soy corredor",
        "soy broker",
        "soy realtor",
        "colega",
        "comision",
        "mls",
        "captador",
        "inmobiliario",
        "soy inmobiliario",
        "soy asesora",
        "soy agente inmobiliario"
    ]

    patrones_cliente = [
        "para mi",
        "para mí",
        "es para mi",
        "es para mí",
        "para mi familia",
        "para nosotros",
        "quiero comprar",
        "quiero alquilar",
        "es para vivir",
        "para vivir",
        "para invertir",
        "para mi empresa"
    ]

    if any(p in texto for p in patrones_colega):
        return "colega_inmobiliario"

    if any(p in texto for p in patrones_cliente):
        return "cliente"

    return "desconocido"

def detectar_rol_usuario(mensaje_usuario: str, estado: dict) -> str:
    rol_directo = detectar_rol_por_respuesta_directa(mensaje_usuario)

    if rol_directo != "desconocido":
        return rol_directo

    analisis = analizar_respuesta_usuario(mensaje_usuario, estado)
    rol = analisis.get("rol_detectado", "desconocido")

    if rol in ["cliente", "colega_inmobiliario", "desconocido"]:
        return rol

    return "desconocido"

# ============================================================
# BÚSQUEDA DE PROPIEDADES
# ============================================================

def propiedad_contiene_caracteristicas(propiedad: dict, caracteristicas: list) -> int:
    texto = normalizar_texto(
        f"{propiedad.get('titulo', '')} "
        f"{propiedad.get('zona', '')} "
        f"{propiedad.get('ciudad', '')} "
        f"{propiedad.get('tipo_propiedad_wasi', '')}"
    )

    score = 0

    for caracteristica in caracteristicas:
        car_norm = normalizar_texto(caracteristica)

        if not car_norm:
            continue

        if car_norm == "sin caracteristicas adicionales":
            continue

        if car_norm in texto:
            score += 2

    return score

def elegir_top_n_propiedades(
    inventario: list,
    filtros: dict,
    n: int = 3,
    excluir_ids: list = None
) -> list:
    excluir_ids = set(str(x) for x in (excluir_ids or []))

    tipo_prop = normalizar_texto(filtros.get("tipo_propiedad", ""))
    tipo_op = normalizar_texto(filtros.get("tipo_operacion", ""))
    zona = filtros.get("zona", "")
    presupuesto = filtros.get("presupuesto")
    habitaciones_min = filtros.get("habitaciones_min")
    banos_min = filtros.get("banos_min")
    caracteristicas = filtros.get("caracteristicas", [])

    propiedades = [
        p for p in inventario
        if str(p.get("id")) not in excluir_ids
    ]

    if tipo_op == "venta":
        propiedades = [
            p for p in propiedades
            if p.get("precio_venta_float", 0) > 0
        ]
    elif tipo_op == "alquiler":
        propiedades = [
            p for p in propiedades
            if p.get("precio_renta_float", 0) > 0
        ]

    if tipo_prop:
        propiedades = [
            p for p in propiedades
            if tipo_prop in normalizar_texto(p.get("tipo_propiedad_wasi", ""))
            or tipo_prop in normalizar_texto(p.get("titulo", ""))
        ]

    if zona:
        propiedades = [
            p for p in propiedades
            if zona_coincide(zona, p.get("zona", ""), p.get("ciudad", ""))
        ]

    if presupuesto is not None:
        if tipo_op == "venta":
            propiedades = [
                p for p in propiedades
                if 0 < p.get("precio_venta_float", 0) <= presupuesto
            ]
        elif tipo_op == "alquiler":
            propiedades = [
                p for p in propiedades
                if 0 < p.get("precio_renta_float", 0) <= presupuesto
            ]

    if habitaciones_min is not None:
        propiedades = [
            p for p in propiedades
            if convertir_entero_seguro(p.get("habitaciones")) >= int(habitaciones_min)
        ]

    if banos_min is not None:
        propiedades = [
            p for p in propiedades
            if convertir_entero_seguro(p.get("banos")) >= int(banos_min)
        ]

    def score(p):
        puntos = 0

        if zona and zona_coincide(zona, p.get("zona", ""), p.get("ciudad", "")):
            puntos += 5

        if tipo_prop and (
            tipo_prop in normalizar_texto(p.get("tipo_propiedad_wasi", ""))
            or tipo_prop in normalizar_texto(p.get("titulo", ""))
        ):
            puntos += 4

        precio = (
            p.get("precio_venta_float", 0)
            if tipo_op == "venta"
            else p.get("precio_renta_float", 0)
        )

        if precio > 0:
            puntos += 2

        if habitaciones_min and convertir_entero_seguro(p.get("habitaciones")) >= int(habitaciones_min):
            puntos += 2

        if banos_min and convertir_entero_seguro(p.get("banos")) >= int(banos_min):
            puntos += 1

        puntos += propiedad_contiene_caracteristicas(p, caracteristicas)

        return puntos

    propiedades_ordenadas = sorted(propiedades, key=score, reverse=True)
    seleccionadas = propiedades_ordenadas[:n]

    for p in seleccionadas:
        p["operacion_buscada"] = tipo_op

    return seleccionadas

# ============================================================
# FORMATEO DE FICHAS
# ============================================================

def formatear_ficha_propiedad(propiedad: dict, es_colega: bool = False) -> str:
    operacion = propiedad.get("operacion_buscada", "venta")

    if operacion == "alquiler":
        precio = propiedad.get("renta", "N/D")
        etiqueta_precio = "Renta"
    else:
        precio = propiedad.get("venta", "N/D")
        etiqueta_precio = "Venta"

    lineas = [
        f"*{propiedad.get('titulo', 'Propiedad sin título')}*",
        f"📍 Zona: {propiedad.get('zona', 'N/D')} | Ciudad: {propiedad.get('ciudad', 'N/D')}",
        f"💰 {etiqueta_precio}: {precio}",
        f"📐 Área: {propiedad.get('area', 'N/D')}m² | 🛏️ Habs: {propiedad.get('habitaciones', 'N/D')} | 🛁 Baños: {propiedad.get('banos', 'N/D')}",
        f"🔗 Ver más: {propiedad.get('enlace', '#')}"
    ]

    if es_colega:
        captador = propiedad.get("captador_propiedad", "N/D")
        telefono = obtener_telefono_captador_de_sheet(captador)

        if telefono == "N/D":
            telefono = propiedad.get("telefono_captador_wasi", "N/D")

        lineas.append(f"👤 Captador: {captador}")
        lineas.append(f"📲 WhatsApp Captador: {telefono}")

    return "\n".join(lineas)

# ============================================================
# INTENCIONES DE CONTINUIDAD
# ============================================================

def usuario_pide_mas_opciones(mensaje: str) -> bool:
    texto = normalizar_texto(mensaje)

    patrones = [
        "ver mas",
        "muestrame mas",
        "otras opciones",
        "otra opcion",
        "ninguna me gusta",
        "no me gusto",
        "no me gustaron",
        "tienes mas",
        "mas opciones",
        "quiero ver mas",
        "muestrame otra",
        "muestre otra",
        "otra alternativa"
    ]

    return any(p in texto for p in patrones)

def usuario_muestra_interes(mensaje: str) -> bool:
    texto = normalizar_texto(mensaje)

    patrones = [
        "me interesa",
        "me gusta",
        "quiero verla",
        "quiero visitar",
        "agendar",
        "cita",
        "la primera",
        "la segunda",
        "la tercera",
        "1era",
        "1ra",
        "2da",
        "3era",
        "3ra",
        "opcion 1",
        "opcion 2",
        "opcion 3",
        "hablar con asesor",
        "contactar asesor",
        "quiero avanzar",
        "quiero coordinar"
    ]

    return any(p in texto for p in patrones)

def usuario_envia_datos_contacto(mensaje: str) -> bool:
    tiene_correo = bool(re.search(r"[\w\.-]+@[\w\.-]+\.\w+", mensaje, re.IGNORECASE))
    tiene_telefono = bool(re.search(r"\+?\d[\d\s\-\(\)]{7,}\d", mensaje))
    return tiene_correo or tiene_telefono

def detectar_indice_propiedad_interes(mensaje: str):
    texto = normalizar_texto(mensaje)

    if any(p in texto for p in ["primera", "1era", "1ra", "opcion 1", "opcion uno"]):
        return 0

    if any(p in texto for p in ["segunda", "2da", "2nda", "opcion 2", "opcion dos"]):
        return 1

    if any(p in texto for p in ["tercera", "3era", "3ra", "opcion 3", "opcion tres"]):
        return 2

    return None

def construir_cierre_cliente() -> str:
    return (
        "\n\n¿Alguna te llamó la atención para coordinar una visita o recibir atención personalizada? "
        "Puedo asignarte un asesor de Mettryc Realty para acompañarte de cerca. 😊"
    )

# ============================================================
# LEADS
# ============================================================

PALABRAS_PROHIBIDAS_NOMBRE = {
    "me", "interesa", "quiero", "verla", "visitar", "cita",
    "agendar", "asesor", "hola", "buenas", "gracias",
    "opcion", "opción", "primera", "segunda", "tercera",
    "la", "el", "era", "3era", "3ra", "tercer", "tercera",
    "propiedad", "casa", "apartamento", "correo", "telefono",
    "whatsapp", "número", "numero"
}

def limpiar_nombre_lead(nombre: str) -> str:
    nombre = re.sub(r"[^A-Za-zÀ-ÖØ-ÿ'´\s-]", " ", str(nombre))
    nombre = re.sub(r"\s+", " ", nombre).strip()
    return nombre

def nombre_es_valido(nombre: str) -> bool:
    if not nombre:
        return False

    partes = nombre.split()

    if len(partes) < 2:
        return False

    if len(partes) > 5:
        return False

    for parte in partes:
        if normalizar_texto(parte) in PALABRAS_PROHIBIDAS_NOMBRE:
            return False

    return True

def obtener_primer_nombre(nombre: str) -> str:
    if not nombre_es_valido(nombre):
        return ""

    return nombre.split()[0]

def extraer_datos_lead(mensaje: str, lead_actual: dict, sender: str = "") -> dict:
    lead = lead_actual.copy()

    correo_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", mensaje, re.IGNORECASE)

    if correo_match:
        lead["correo"] = correo_match.group(0).strip()

    telefono_match = re.search(r"(\+?\d[\d\s\-\(\)]{7,}\d)", mensaje)

    if telefono_match:
        lead["whatsapp"] = limpiar_telefono(telefono_match.group(1))

    nombre_match = re.search(
        r"(?:soy|me llamo|mi nombre es|nombre completo es|mi nombre completo es)\s+([A-Za-zÀ-ÖØ-ÿ'´-]+(?:\s+[A-Za-zÀ-ÖØ-ÿ'´-]+){1,4})",
        mensaje,
        re.IGNORECASE
    )

    if nombre_match:
        nombre_detectado = limpiar_nombre_lead(nombre_match.group(1).strip())

        if nombre_es_valido(nombre_detectado):
            lead["nombre"] = nombre_detectado

    elif not lead.get("nombre"):
        # Permite capturar si el usuario responde solo con nombre y apellido,
        # o si envía nombre + correo + teléfono juntos.
        texto_sin_email = re.sub(r"[\w\.-]+@[\w\.-]+\.\w+", "", mensaje)
        texto_sin_tel = re.sub(r"\+?\d[\d\s\-\(\)]{7,}\d", "", texto_sin_email)
        posible_nombre = limpiar_nombre_lead(texto_sin_tel)

        if nombre_es_valido(posible_nombre):
            lead["nombre"] = posible_nombre

    return lead

def lead_completo(lead: dict) -> bool:
    nombre = lead.get("nombre", "")
    correo = lead.get("correo", "")
    whatsapp = lead.get("whatsapp", "")

    return bool(
        nombre_es_valido(nombre)
        and correo
        and "@" in correo
        and "." in correo
        and whatsapp
    )

def construir_solicitud_datos_cliente(lead: dict) -> str:
    faltantes = []

    if not nombre_es_valido(lead.get("nombre", "")):
        faltantes.append("Nombre completo, nombre y apellido")

    if not lead.get("correo"):
        faltantes.append("correo electrónico")

    if not lead.get("whatsapp"):
        faltantes.append("número de WhatsApp")

    return (
        "¡Excelente! Me encanta que quieras avanzar 😊 "
        "Para asignarte rápidamente un asesor por nuestro sistema inmobiliario, "
        f"compárteme por favor: {', '.join(faltantes)}."
    )

def construir_resumen_necesidad(filtros: dict, propiedad_interes: dict = None) -> str:
    partes = []

    if filtros.get("tipo_propiedad"):
        partes.append(f"Tipo de propiedad: {filtros['tipo_propiedad']}")

    if filtros.get("tipo_operacion"):
        partes.append(f"Operación: {filtros['tipo_operacion']}")

    if filtros.get("zona"):
        partes.append(f"Zona: {filtros['zona']}")

    if filtros.get("presupuesto"):
        partes.append(f"Presupuesto máximo: {filtros['presupuesto']}")

    if filtros.get("habitaciones_min"):
        partes.append(f"Habitaciones mínimas: {filtros['habitaciones_min']}")

    if filtros.get("banos_min"):
        partes.append(f"Baños mínimos: {filtros['banos_min']}")

    if filtros.get("caracteristicas"):
        partes.append(f"Características: {', '.join(filtros['caracteristicas'])}")

    if propiedad_interes:
        partes.append("")
        partes.append("Propiedad de interés:")
        partes.append(f"Título: {propiedad_interes.get('titulo', 'N/D')}")
        partes.append(f"ID: {propiedad_interes.get('id', 'N/D')}")
        partes.append(f"Enlace: {propiedad_interes.get('enlace', 'N/D')}")

    return "\n".join([f"- {p}" if p else "" for p in partes]) or "No especificado"

# ============================================================
# TELEGRAM
# ============================================================

def enviar_notificaciones_telegram(agente, lead: dict, resumen_necesidad: str):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not telegram_token:
        logger.warning("TELEGRAM_BOT_TOKEN no configurado.")
        return

    admin_ids = [
        x.strip()
        for x in os.getenv("TELEGRAM_ADMIN_IDS", os.getenv("TELEGRAM_ADMIN_ID", "")).split(",")
        if x.strip()
    ]

    nombre_cliente = lead.get("nombre", "Cliente sin nombre")
    correo = lead.get("correo", "N/D")
    whatsapp = limpiar_telefono(lead.get("whatsapp", ""))
    link_wa = f"https://wa.me/{whatsapp}" if whatsapp else "N/D"

    nombre_agente = (
        agente.get("nombre", "Agente sin nombre")
        if agente
        else "Agente no asignado"
    )

    mensaje_agente = (
        f"👤 Nuevo cliente asignado\n\n"
        f"Cliente: {nombre_cliente}\n"
        f"Correo: {correo}\n"
        f"WhatsApp: {whatsapp}\n"
        f"Enlace WhatsApp: {link_wa}\n\n"
        f"Resumen de necesidad:\n{resumen_necesidad}"
    )

    mensaje_admin = (
        f"🏢 Agente asignado: {nombre_agente}\n\n"
        f"👤 Nuevo cliente capturado\n\n"
        f"Cliente: {nombre_cliente}\n"
        f"Correo: {correo}\n"
        f"WhatsApp: {whatsapp}\n"
        f"Enlace WhatsApp: {link_wa}\n\n"
        f"Resumen de necesidad:\n{resumen_necesidad}"
    )

    url_tg = f"https://api.telegram.org/bot{telegram_token}/sendMessage"

    def enviar(chat_id: str, mensaje: str):
        try:
            response = requests.post(
                url_tg,
                json={
                    "chat_id": chat_id,
                    "text": mensaje
                },
                timeout=8
            )
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Error enviando Telegram a {chat_id}: {e}")

    if agente and agente.get("telegram_id"):
        enviar(str(agente["telegram_id"]).strip(), mensaje_agente)
    else:
        logger.warning("Agente sin telegram_id.")

    for admin_id in admin_ids:
        if admin_id.lstrip("-").isdigit():
            enviar(admin_id, mensaje_admin)
        else:
            logger.warning(f"TELEGRAM_ADMIN_ID inválido: {admin_id}")

# ============================================================
# APRENDIZAJE OPERATIVO OPCIONAL
# ============================================================

def registrar_aprendizaje_conversacion(
    sender: str,
    rol: str,
    mensaje_usuario: str,
    estado: dict,
    respuesta_bot: str,
    resultado: str = ""
):
    url = os.getenv("GOOGLE_SHEET_APRENDIZAJE_URL")

    if not url:
        return

    payload = {
        "fecha": ahora_local().isoformat(),
        "sender": sender,
        "rol": rol,
        "mensaje_usuario": mensaje_usuario,
        "estado": estado.get("estado"),
        "filtros": estado.get("filtros"),
        "lead": {
            "tiene_nombre": bool(estado.get("lead", {}).get("nombre")),
            "tiene_correo": bool(estado.get("lead", {}).get("correo")),
            "tiene_whatsapp": bool(estado.get("lead", {}).get("whatsapp"))
        },
        "propiedad_interes": estado.get("propiedad_interes"),
        "respuesta_bot": respuesta_bot,
        "resultado": resultado
    }

    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        logger.warning(f"No se pudo registrar aprendizaje: {e}")

def responder(sender: str, mensaje_usuario: str, respuesta: str, estado: dict, resultado: str = "respuesta_enviada"):
    respuesta_limpia = limpiar_respuesta_whatsapp(respuesta)

    actualizar_memoria(sender, mensaje_usuario, respuesta_limpia)

    registrar_aprendizaje_conversacion(
        sender=sender,
        rol=estado.get("rol"),
        mensaje_usuario=mensaje_usuario,
        estado=estado,
        respuesta_bot=respuesta_limpia,
        resultado=resultado
    )

    return {"replies": [{"message": respuesta_limpia}]}

# ============================================================
# CASOS ESPECIALES
# ============================================================

def es_consulta_reclutamiento(mensaje: str) -> bool:
    texto = normalizar_texto(mensaje)

    patrones = [
        "quiero unirme",
        "trabajar con ustedes",
        "ser agente",
        "ser asesor",
        "mettryc team",
        "reclutamiento",
        "curso",
        "comision",
        "comisiones"
    ]

    return any(p in texto for p in patrones)

def respuesta_reclutamiento() -> str:
    return (
        "¡Qué emoción que quieras unirte al Mettryc Team! 🚀 "
        "Aquí tienes la información: https://mettryc.com/blog/unete-al-mettryc-team-y-gana-desde-el-80-al-100-de-comision/18270?page=1\n\n"
        "El curso inicial cuesta $60 y dura 5 días, de 9am a 12pm."
    )

# ============================================================
# STARTUP
# ============================================================

def precargar_inventario_background():
    try:
        logger.info("Precarga de inventario iniciada en background...")
        actualizar_inventario_si_corresponde(forzar=True)
        logger.info("Precarga de inventario finalizada.")
    except Exception as e:
        logger.error(f"Error precargando inventario en background: {e}", exc_info=True)

def precargar_sheets_background():
    try:
        logger.info("Precarga de Google Sheets iniciada en background...")
        sincronizar_google_sheet()
        logger.info("Precarga de Google Sheets finalizada.")
    except Exception as e:
        logger.error(f"Error precargando Google Sheets: {e}", exc_info=True)

@app.on_event("startup")
def startup_event():
    logger.info("Iniciando tareas de startup...")

    hilo_precarga = threading.Thread(
        target=precargar_inventario_background,
        daemon=True
    )
    hilo_precarga.start()

    hilo_scheduler = threading.Thread(
        target=scheduler_inventario_background,
        daemon=True
    )
    hilo_scheduler.start()

    hilo_sheets = threading.Thread(
        target=precargar_sheets_background,
        daemon=True
    )
    hilo_sheets.start()

# ============================================================
# WEBHOOK PRINCIPAL
# ============================================================

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()

        api_key_header = str(request.headers.get("x-api-key", "")).strip()
        valid_api_keys = [
            key.strip()
            for key in os.getenv("API_KEYS_AGENTES", "").split(",")
            if key.strip()
        ]

        if api_key_header not in valid_api_keys or not api_key_header:
            raise HTTPException(status_code=403, detail="Acceso denegado")

        payload = data.get("query") if isinstance(data.get("query"), dict) else data

        sender = str(payload.get("sender", "")).strip()
        mensaje_cliente = str(payload.get("message", "")).strip()

        if not mensaje_cliente:
            return {"replies": []}

        logger.info(
            f"Webhook recibido | sender='{sender}' | "
            f"message_id='{obtener_message_id_payload(payload)}' | "
            f"mensaje='{mensaje_cliente[:100]}'"
        )

        if es_mensaje_duplicado(payload, sender, mensaje_cliente):
            return {"replies": []}

        estado = obtener_estado_usuario(sender)

        primera_interaccion = (
            sender not in memoria_conversaciones
            or len(memoria_conversaciones.get(sender, [])) == 0
        )

        # ------------------------------------------------------------
        # Casos especiales sin tocar inventario
        # ------------------------------------------------------------

        if "mercadolibre.com.ve/mlv" in mensaje_cliente.lower():
            respuesta = (
                "¡Hola! 👋 Esta propiedad se encuentra disponible en el precio publicado. "
                "¿Quieres agendar una visita?"
            )
            return responder(sender, mensaje_cliente, respuesta, estado)

        if es_consulta_reclutamiento(mensaje_cliente):
            respuesta = respuesta_reclutamiento()
            return responder(sender, mensaje_cliente, respuesta, estado)

        # ------------------------------------------------------------
        # Saludo simple sin IA y sin inventario
        # ------------------------------------------------------------

        if primera_interaccion and mensaje_es_saludo_simple(mensaje_cliente):
            estado["estado"] = ESTADO_INDAGANDO
            respuesta = construir_pregunta_campo("tipo_propiedad", es_inicio=True)
            return responder(sender, mensaje_cliente, respuesta, estado)

        # ------------------------------------------------------------
        # Más opciones luego de mostrar propiedades
        # ------------------------------------------------------------

        if (
            estado["estado"] == ESTADO_MOSTRANDO_PROPIEDADES
            and usuario_pide_mas_opciones(mensaje_cliente)
        ):
            inventario = obtener_inventario()

            if not inventario:
                respuesta = (
                    "Estoy actualizando nuestro inventario en este momento. "
                    "Por favor, escríbeme nuevamente en unos minutos. 😊"
                )
                return responder(sender, mensaje_cliente, respuesta, estado)

            propiedades = elegir_top_n_propiedades(
                inventario,
                estado["filtros"],
                n=3,
                excluir_ids=estado["propiedades_enviadas"]
            )

            if not propiedades:
                respuesta = (
                    "Por ahora no encontré más opciones con esos mismos criterios. "
                    "¿Quieres que ampliemos la zona o ajustemos el presupuesto?"
                )
            else:
                estado["propiedades_enviadas"].extend([
                    p.get("id")
                    for p in propiedades
                ])

                estado["ultimas_propiedades_mostradas"] = propiedades

                es_colega = estado["rol"] == "colega_inmobiliario"

                fichas = [
                    formatear_ficha_propiedad(p, es_colega=es_colega)
                    for p in propiedades
                ]

                respuesta = (
                    "Claro, te comparto opciones adicionales:\n\n"
                    + "\n\n".join(fichas)
                )

                if estado["rol"] == "cliente":
                    respuesta += construir_cierre_cliente()

            return responder(sender, mensaje_cliente, respuesta, estado)

        # ------------------------------------------------------------
        # Interés de cliente luego de ver propiedades
        # ------------------------------------------------------------

        if (
            estado["estado"] == ESTADO_MOSTRANDO_PROPIEDADES
            and estado["rol"] == "cliente"
            and sender not in clientes_procesados
            and (
                usuario_muestra_interes(mensaje_cliente)
                or usuario_envia_datos_contacto(mensaje_cliente)
            )
        ):
            indice = detectar_indice_propiedad_interes(mensaje_cliente)

            if indice is not None:
                ultimas = estado.get("ultimas_propiedades_mostradas", [])

                if indice < len(ultimas):
                    estado["propiedad_interes"] = ultimas[indice]

            estado["estado"] = ESTADO_CAPTURANDO_LEAD

        # ------------------------------------------------------------
        # Captura de lead
        # ------------------------------------------------------------

        if estado["estado"] == ESTADO_CAPTURANDO_LEAD:
            estado["lead"] = extraer_datos_lead(
                mensaje_cliente,
                estado["lead"],
                sender
            )

            if not lead_completo(estado["lead"]):
                respuesta = construir_solicitud_datos_cliente(estado["lead"])
                return responder(sender, mensaje_cliente, respuesta, estado)

            agente = asignar_agente_round_robin()

            resumen = construir_resumen_necesidad(
                estado["filtros"],
                estado.get("propiedad_interes")
            )

            if agente:
                enviar_notificaciones_telegram(
                    agente,
                    estado["lead"],
                    resumen
                )

                clientes_procesados.add(sender)
                estado["estado"] = ESTADO_LEAD_COMPLETO

                primer_nombre = obtener_primer_nombre(estado["lead"]["nombre"])
                saludo_nombre = f", {primer_nombre}" if primer_nombre else ""

                respuesta = (
                    f"¡Perfecto{saludo_nombre}! ✨ "
                    f"He registrado tus datos y asigné tu solicitud a *{agente.get('nombre', 'uno de nuestros asesores')}*. "
                    "Te contactará muy pronto para coordinar tu atención personalizada. 🤝"
                )
            else:
                respuesta = (
                    "¡Perfecto! Ya tengo tus datos. Estamos asignando un asesor disponible "
                    "para que te contacte lo antes posible. 😊"
                )

            return responder(
                sender,
                mensaje_cliente,
                respuesta,
                estado,
                resultado="lead_convertido"
            )

        # ------------------------------------------------------------
        # Extraer filtros sin descargar inventario
        # ------------------------------------------------------------

        estado["filtros"] = extraer_filtros_busqueda(
            mensaje_cliente,
            estado["filtros"],
            estado
        )

        # ------------------------------------------------------------
        # Preguntar filtros faltantes
        # ------------------------------------------------------------

        campo_faltante = obtener_siguiente_campo_faltante(estado["filtros"])

        if campo_faltante:
            estado["estado"] = ESTADO_COMPLETANDO_FILTROS

            pregunta_base = construir_pregunta_campo(
                campo_faltante,
                es_inicio=primera_interaccion
            )

            # Para ahorrar tokens, usamos plantilla. IA solo si queremos salvar objeciones o dudas.
            respuesta = pregunta_base

            return responder(sender, mensaje_cliente, respuesta, estado)

        # ------------------------------------------------------------
        # Detectar rol
        # ------------------------------------------------------------

        rol_detectado = detectar_rol_por_respuesta_directa(mensaje_cliente)

        if rol_detectado != "desconocido":
            estado["rol"] = rol_detectado

        if not estado.get("rol"):
            rol_ia = detectar_rol_usuario(
                mensaje_cliente,
                estado
            )

            if rol_ia == "desconocido":
                estado["estado"] = ESTADO_DEFINIENDO_ROL
                respuesta = construir_pregunta_rol()
                return responder(sender, mensaje_cliente, respuesta, estado)

            estado["rol"] = rol_ia

        # ------------------------------------------------------------
        # Buscar propiedades solo aquí
        # ------------------------------------------------------------

        inventario = obtener_inventario()

        if not inventario:
            respuesta = (
                "Estoy actualizando nuestro inventario en este momento. "
                "Por favor, escríbeme nuevamente en unos minutos. 😊"
            )
            return responder(sender, mensaje_cliente, respuesta, estado)

        propiedades = elegir_top_n_propiedades(
            inventario,
            estado["filtros"],
            n=3,
            excluir_ids=estado["propiedades_enviadas"]
        )

        estado["estado"] = ESTADO_MOSTRANDO_PROPIEDADES

        if not propiedades:
            respuesta = (
                "Revisé nuestro inventario activo y no encontré una coincidencia exacta con esos criterios. "
                "¿Quieres que ampliemos la zona o ajustemos un poco el presupuesto?"
            )
        else:
            estado["propiedades_enviadas"].extend([
                p.get("id")
                for p in propiedades
            ])

            estado["ultimas_propiedades_mostradas"] = propiedades

            es_colega = estado["rol"] == "colega_inmobiliario"

            fichas = [
                formatear_ficha_propiedad(p, es_colega=es_colega)
                for p in propiedades
            ]

            if es_colega:
                respuesta = (
                    "¡Perfecto, colega! Revisé nuestro inventario y estas son las opciones que más se acercan. "
                    "Te incluyo los datos del captador para que puedas validar disponibilidad:\n\n"
                    + "\n\n".join(fichas)
                )
            else:
                respuesta = (
                    "¡Perfecto! Revisé nuestro inventario y estas son las opciones que más se acercan a lo que buscas:\n\n"
                    + "\n\n".join(fichas)
                    + construir_cierre_cliente()
                )

        return responder(sender, mensaje_cliente, respuesta, estado)

    except HTTPException as e:
        raise e

    except Exception as e:
        logger.error(f"Error crítico general: {e}", exc_info=True)
        return {
            "replies": [
                {
                    "message": "Lo siento, estamos procesando tu solicitud. Por favor, escribe de nuevo."
                }
            ]
        }
