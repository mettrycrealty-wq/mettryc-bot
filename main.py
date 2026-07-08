import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta

import requests
from fastapi import FastAPI, HTTPException, Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

HORARIOS_ACTUALIZACION_INVENTARIO = [0, 12]

MODELO_PRINCIPAL = "openai/gpt-4o-mini"
MODELO_RESPALDO = "google/gemini-2.5-flash-lite"
MAX_TOKENS_IA = 220

INTERVALO_ACTUALIZACION_SHEETS = timedelta(hours=1)

# ============================================================
# CACHÉ Y MEMORIA
# ============================================================

cache = {
    "inventario": [],
    "ultima_actualizacion": datetime.min,
    "proxima_actualizacion": datetime.min
}

sheets_cache = {
    "agentes": [],
    "captadores": {},
    "ultimo_indice": -1,
    "ultima_actualizacion": datetime.min
}

memoria_conversaciones = {}
clientes_procesados = set()
estado_usuarios = {}

# ============================================================
# MEMORIA DE APRENDIZAJE
# ============================================================

aprendizaje_global = {
    "zonas": Counter(),
    "tipo_propiedad": Counter(),
    "operaciones": Counter()
}

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
# UTILIDADES
# ============================================================

STOPWORDS_ZONA = {
    "el", "la", "los", "las", "de", "del", "en", "y",
    "urb", "urbanizacion", "urbanización", "sector", "zona"
}


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

    texto_propiedad = f"{zona_norm} {ciudad_norm}".strip()

    if buscada_norm in texto_propiedad:
        return True

    tokens_busqueda = tokens_relevantes(buscada_norm)
    tokens_propiedad = tokens_relevantes(texto_propiedad)

    if not tokens_busqueda:
        return True

    coincidencias = tokens_busqueda.intersection(tokens_propiedad)
    return len(coincidencias) >= min(len(tokens_busqueda), 2)


def convertir_entero_seguro(valor) -> int:
    try:
        if valor in [None, "", "N/D"]:
            return 0
        return int(float(valor))
    except Exception:
        return 0


def limpiar_telefono(valor: str) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def es_mensaje_poco_informativo(mensaje: str) -> bool:
    texto = (mensaje or "").strip()
    if not texto:
        return True
    puntuaciones = set("?!¡¿.")
    if len(texto) <= 3 and all(c in puntuaciones for c in texto):
        return True
    if texto.lower() in {"ok", "va", "si", "sí"} and len(texto) <= 2:
        return True
    return False


def mensaje_recordatorio_filtro(campo: str) -> str:
    recordatorios = {
        "tipo_propiedad": (
            "Te recuerdo que necesito saber qué tipo de propiedad estás buscando "
            "(casa, apartamento, local, oficina…). 😊"
        ),
        "tipo_operacion": (
            "Para seguir con la búsqueda me confirmas si la quieres para venta o alquiler."
        ),
        "zona": (
            "¿En qué zona, urbanización o ciudad deseas que busque?"
            " Puedes contármelo con tus palabras, por ejemplo: Trigal Norte, Valencia."
        ),
        "presupuesto": (
            "¿Cuál es tu presupuesto máximo aproximado?"
            " Así solo te muestro las opciones que realmente te sirven."
        ),
        "caracteristicas": (
            "¿Qué característica es indispensable (patio, terraza, vigilancia, estacionamiento, etc.)?"
        )
    }
    return recordatorios.get(
        campo,
        "Estoy pendiente de ese dato para ayudarte de forma precisa."
    )


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
    if len(memoria_conversaciones[sender]) > 24:
        memoria_conversaciones[sender] = memoria_conversaciones[sender][-24:]


def obtener_estado_usuario(sender: str) -> dict:
    if sender not in estado_usuarios:
        estado_usuarios[sender] = {
            "estado": ESTADO_INDAGANDO,
            "rol": None,
            "rol_preguntado": False,
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
            "lead": {
                "nombre": "",
                "correo": "",
                "whatsapp": ""
            },
            "ultimo_pedido_dato": None,
            "mensaje_previo": "",
            "ultima_respuesta": ""
        }
    return estado_usuarios[sender]


def registrar_aprendizaje(filtros: dict):
    tipo = normalizar_texto(filtros.get("tipo_propiedad", ""))
    if tipo:
        aprendizaje_global["tipo_propiedad"][tipo] += 1
    operacion = normalizar_texto(filtros.get("tipo_operacion", ""))
    if operacion:
        aprendizaje_global["operaciones"][operacion] += 1
    zona = normalizar_texto(filtros.get("zona", ""))
    if zona:
        aprendizaje_global["zonas"][zona] += 1


def enviar_respuesta(sender: str, mensaje_cliente: str, respuesta: str, estado: dict = None, formatear_bold: bool = True):
    if estado is not None:
        estado["ultima_respuesta"] = respuesta
    actualizar_memoria(sender, mensaje_cliente, respuesta)
    mensaje_final = respuesta.replace("**", "*") if formatear_bold else respuesta
    return {"replies": [{"message": mensaje_final}]}


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
    texto_norm = normalizar_texto(texto)
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(mil|k)", texto_norm)
    if match:
        numero = float(match.group(1).replace(",", "."))
        return numero * 1000
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(millon|millones|mm)", texto_norm)
    if match:
        numero = float(match.group(1).replace(",", "."))
        return numero * 1000000
    match = re.search(r"(?:usd|us|\$)?\s*(\d{4,9})", texto_norm)
    if match:
        return float(match.group(1))
    return None


# ============================================================
# ACTUALIZACIÓN DE INVENTARIO (cada 12h y al redeploy)
# ============================================================

def calcular_proxima_actualizacion(ahora: datetime = None) -> datetime:
    ahora = ahora or datetime.now()
    ventanas = sorted(HORARIOS_ACTUALIZACION_INVENTARIO)
    for hora in ventanas:
        candidato = ahora.replace(hour=hora, minute=0, second=0, microsecond=0)
        if candidato > ahora:
            return candidato
    siguiente_dia = ahora + timedelta(days=1)
    return siguiente_dia.replace(hour=ventanas[0], minute=0, second=0, microsecond=0)


def necesita_actualizar_inventario(force: bool = False) -> bool:
    ahora = datetime.now()
    if force:
        return True
    if not cache["inventario"]:
        return True
    proxima = cache.get("proxima_actualizacion")
    if proxima == datetime.min:
        cache["proxima_actualizacion"] = calcular_proxima_actualizacion(ahora)
        return False
    if ahora >= proxima:
        return True
    return False


def actualizar_cache_inventario(force: bool = False):
    ahora = datetime.now()
    if not necesita_actualizar_inventario(force):
        return False
    propiedades = obtener_inventario_desde_wasi()
    if propiedades:
        cache["inventario"] = propiedades
        cache["ultima_actualizacion"] = ahora
        cache["proxima_actualizacion"] = calcular_proxima_actualizacion(ahora + timedelta(seconds=1))
        logger.info(f"Inventario actualizado con {len(propiedades)} propiedades.")
        return True
    logger.warning("No se pudo actualizar inventario. Se mantiene caché anterior.")
    return False


async def garantizar_inventario_actualizado(force: bool = False):
    if necesita_actualizar_inventario(force):
        await asyncio.to_thread(actualizar_cache_inventario, force)


@app.on_event("startup")
async def cargar_inventario_al_arrancar():
    await garantizar_inventario_actualizado(force=True)


# ============================================================
# INVENTARIO WASI
# ============================================================

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
                    timeout=30
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


# ============================================================
# GOOGLE SHEETS
# ============================================================

def sincronizar_google_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL")

    if not script_url:
        logger.warning("GOOGLE_SHEET_TURNOS_URL no configurada.")
        return

    necesita_actualizar = (
        datetime.now() - sheets_cache["ultima_actualizacion"] > INTERVALO_ACTUALIZACION_SHEETS
        or not sheets_cache["agentes"]
        or not sheets_cache["captadores"]
    )

    if not necesita_actualizar:
        return

    try:
        response = requests.get(script_url, timeout=15)
        response.raise_for_status()
        payload_sheet = response.json()

        if isinstance(payload_sheet, dict):
            sheets_cache["agentes"] = payload_sheet.get("agentes", [])
            captadores_payload = payload_sheet.get("captadores", {})
            captadores_transformados = {}

            if isinstance(captadores_payload, dict):
                pairs = captadores_payload.items()
            elif isinstance(captadores_payload, list):
                pairs = []
                for row in captadores_payload:
                    if isinstance(row, dict):
                        key = row.get("nombre") or row.get("name")
                        valor = row.get("telefono") or row.get("phone")
                        if key and valor:
                            pairs.append((key, valor))
            else:
                pairs = []

            for nombre, telefono in pairs:
                if not nombre or not telefono:
                    continue
                captadores_transformados[str(nombre).strip()] = str(telefono).strip()

            sheets_cache["captadores"] = captadores_transformados
            sheets_cache["ultima_actualizacion"] = datetime.now()

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

def consultar_ia(historial, max_tokens=MAX_TOKENS_IA, fallback=""):
    url_ia = "https://openrouter.ai/api/v1/chat/completions"
    api_key = os.getenv("OPENROUTER_API_KEY")

    if not api_key:
        logger.error("OPENROUTER_API_KEY no configurada.")
        return fallback or ""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://www.mettryc.com",
        "X-Title": "Mettryc Realty Chatbot"
    }

    for modelo in [MODELO_PRINCIPAL, MODELO_RESPALDO]:
        try:
            logger.info(f"Consultando IA con modelo: {modelo}")
            response = requests.post(
                url_ia,
                headers=headers,
                json={
                    "model": modelo,
                    "messages": historial,
                    "max_tokens": max_tokens
                },
                timeout=30
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


# ============================================================
# EXTRACCIÓN DE FILTROS
# ============================================================

def extraer_filtros_regex(mensaje_usuario: str, filtros: dict) -> dict:
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
        if any(p in texto for p in ["venta", "comprar", "compra"]):
            filtros["tipo_operacion"] = "venta"
        elif any(p in texto for p in ["alquiler", "alquilar", "renta", "arrendamiento"]):
            filtros["tipo_operacion"] = "alquiler"

    if filtros.get("presupuesto") is None:
        presupuesto = parsear_presupuesto_texto(mensaje_usuario)
        if presupuesto:
            filtros["presupuesto"] = presupuesto

    if filtros.get("habitaciones_min") is None:
        match_hab = re.search(r"(\d+)\s*(hab|habitacion|habitaciones|cuarto|cuartos)", texto)
        if match_hab:
            filtros["habitaciones_min"] = int(match_hab.group(1))

    if filtros.get("banos_min") is None:
        match_banos = re.search(r"(\d+)\s*(bano|banos|baño|baños)", texto)
        if match_banos:
            filtros["banos_min"] = int(match_banos.group(1))

    if not filtros.get("zona"):
        zona_match = re.search(
            r"(?:en|zona|urbanizacion|urbanización)\s+(.+?)(?:\s+(?:venta|alquiler|hasta|con|presupuesto|de\s+\d)|$)",
            mensaje_usuario,
            re.IGNORECASE
        )
        if zona_match:
            zona = zona_match.group(1).strip()
            zona = re.sub(r"\s+", " ", zona)
            if len(zona.split()) <= 6:
                filtros["zona"] = zona

    caracteristicas_clave = [
        "patio", "terraza", "jardin", "jardín", "piscina", "vigilancia",
        "seguridad", "estacionamiento", "garage", "garaje", "maletero",
        "planta baja", "remodelado", "amoblado", "ascensor", "pozo",
        "tanque", "calle cerrada", "conjunto cerrado"
    ]

    actuales = filtros.get("caracteristicas", [])
    for car in caracteristicas_clave:
        if normalizar_texto(car) in texto and car not in actuales:
            actuales.append(car)

    if any(p in texto for p in ["sin caracteristicas", "sin características", "ninguna", "no tengo"]):
        if not actuales:
            actuales.append("sin características adicionales")
    filtros["caracteristicas"] = actuales
    return filtros


def extraer_filtros_busqueda(mensaje_usuario: str, filtros_actuales: dict, historial: list) -> dict:
    prompt = f"""
Eres un extractor de datos para búsqueda inmobiliaria. Responde únicamente JSON válido.

Extrae los siguientes campos:
- tipo_propiedad
- tipo_operacion
- zona
- presupuesto (número máximo sin símbolos)
- habitaciones_min
- banos_min
- caracteristicas (lista, usa ["sin características adicionales"] si no hay)

Mantén los datos existentes si el mensaje no los modifica.

Filtros actuales:
{json.dumps(filtros_actuales, ensure_ascii=False)}

Mensaje:
"{mensaje_usuario}"

JSON:
{{
  "tipo_propiedad": "",
  "tipo_operacion": "",
  "zona": "",
  "presupuesto": null,
  "habitaciones_min": null,
  "banos_min": null,
  "caracteristicas": []
}}
"""

    historial_reciente = historial[-4:] if isinstance(historial, list) else []

    respuesta_raw = consultar_ia(
        [{"role": "system", "content": prompt}] + historial_reciente,
        max_tokens=300,
        fallback=""
    )

    filtros = filtros_actuales.copy()
    try:
        start = respuesta_raw.find("{")
        end = respuesta_raw.rfind("}")
        if start != -1 and end != -1:
            data = json.loads(respuesta_raw[start:end + 1])
        else:
            data = {}
    except Exception:
        data = {}

    for campo in ["tipo_propiedad", "tipo_operacion", "zona"]:
        valor = data.get(campo)
        if valor:
            filtros[campo] = str(valor).strip()

    for campo in ["presupuesto", "habitaciones_min", "banos_min"]:
        valor = data.get(campo)
        if valor not in [None, "", []]:
            try:
                filtros[campo] = float(valor) if campo == "presupuesto" else int(valor)
            except Exception:
                pass

    caracteristicas = data.get("caracteristicas", [])
    if isinstance(caracteristicas, list) and caracteristicas:
        actuales = filtros.get("caracteristicas", [])
        filtros["caracteristicas"] = list(dict.fromkeys(actuales + caracteristicas))

    tipo_op = normalizar_texto(filtros.get("tipo_operacion", ""))
    if tipo_op in ["comprar", "compra", "venta", "vendo"]:
        filtros["tipo_operacion"] = "venta"
    elif tipo_op in ["renta", "alquiler", "alquilar", "arrendamiento"]:
        filtros["tipo_operacion"] = "alquiler"

    filtros = extraer_filtros_regex(mensaje_usuario, filtros)
    registrar_aprendizaje(filtros)
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


def construir_pregunta_campo(campo: str, es_inicio=False) -> str:
    prefijo = ""
    if es_inicio:
        prefijo = (
            "¡Hola! 👋 Soy Paty, asistente VIP de Mettryc Realty. "
            "¿Vamos armando tu búsqueda?"
        )
    preguntas = {
        "tipo_propiedad": (
            "Cuéntame, ¿qué tipo de propiedad estás buscando?"
            " Por ejemplo: casa, apartamento, local u oficina."
        ),
        "tipo_operacion": "Perfecto. ¿La buscas para venta o alquiler?",
        "zona": "Excelente. ¿En qué zona, urbanización o ciudad te gustaría buscar?",
        "presupuesto": "Genial. ¿Cuál es tu presupuesto máximo aproximado?",
        "caracteristicas": (
            "Y para asegurarte una buena experiencia, ¿qué característica es indispensable?"
            " Por ejemplo: habitaciones, patio, estacionamiento, vigilancia o terraza."
        )
    }
    pregunta = preguntas.get(campo, "Cuéntame un poco más sobre lo que necesitas.")
    if prefijo:
        return prefijo + "\n" + pregunta
    return pregunta


# ============================================================
# DETECCIÓN ESPECIAL DE MENSAJES
# ============================================================

def usuario_solicita_ajuste(mensaje: str) -> bool:
    texto = normalizar_texto(mensaje)
    patrones = [
        "no me gustaron", "no me gusto", "no me gustan",
        "ninguna me gusta", "ninguna me gustó", "no es lo que busco",
        "no me interesa", "no me sirve", "no me convencen",
        "no encuentro", "no lo que busco", "no me termina"
    ]
    return any(p in texto for p in patrones)


# ============================================================
# ROL
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
        "inmobiliario"
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
        "para vivir"
    ]
    if any(p in texto for p in patrones_colega):
        return "colega_inmobiliario"
    if any(p in texto for p in patrones_cliente):
        return "cliente"
    return "desconocido"


def construir_pregunta_rol() -> str:
    return (
        "Perfecto, ya tengo la información base para revisar opciones. 😊\n\n"
        "Una pregunta rápida: ¿la propiedad la estás buscando para ti o para un cliente?"
    )


# ============================================================
# PROPIEDADES
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
        if not car_norm or car_norm == "sin caracteristicas adicionales":
            continue
        if car_norm in texto:
            score += 2
    return score


def elegir_top_n_propiedades(inventario, filtros, n=3, excluir_ids=None):
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
# INTENCIONES
# ============================================================

def usuario_pide_mas_opciones(mensaje: str) -> bool:
    texto = normalizar_texto(mensaje)
    patrones = [
        "ver mas", "muestrame mas", "muestrame más", "otras opciones", "otra opcion",
        "otra opción", "tienes mas", "tienes más", "mas opciones",
        "más opciones", "quiero ver mas", "quiero ver más"
    ]
    return any(p in texto for p in patrones)


def usuario_muestra_interes(mensaje: str) -> bool:
    texto = normalizar_texto(mensaje)
    patrones = [
        "me interesa", "me gusta", "quiero verla", "quiero visitar",
        "agendar", "cita", "la primera", "la segunda", "la tercera",
        "opcion 1", "opcion 2", "opcion 3", "hablar con asesor",
        "contactar asesor", "quiero avanzar", "quiero coordinar", "quiero ver la",
        "quiero coordinate"
    ]
    return any(p in texto for p in patrones)


def usuario_envia_datos_contacto(mensaje: str) -> bool:
    tiene_correo = bool(re.search(r"[\w\.-]+@[\w\.-]+\.\w+", mensaje, re.IGNORECASE))
    tiene_telefono = bool(re.search(r"\+?\d[\d\s\-\(\)]{7,}\d", mensaje))
    return tiene_correo or tiene_telefono


def construir_cierre_cliente() -> str:
    return (
        "\n\n¿Alguna te interesa para coordinar una visita o recibir atención personalizada? "
        "Puedo asignarte rápidamente un asesor de Mettryc Realty. 😊"
    )


# ============================================================
# LEADS
# ============================================================

PALABRAS_INVALIDAS_NOMBRE = {
    "me", "interesa", "quiero", "verla", "visitar", "cita",
    "agendar", "asesor", "hola", "buenas", "gracias",
    "opcion", "opción", "primera", "segunda", "tercera",
    "lista", "lista", "ultima", "última", "segunda", "tercera",
    "casa", "casas", "venta", "alquiler", "propiedad", "mas", "más", "lista"
}


def es_nombre_directo(mensaje: str, palabras_validas: list[str]) -> bool:
    texto = (mensaje or "").strip()
    if not texto or len(texto) > 40 or len(palabras_validas) < 2 or len(palabras_validas) > 4:
        return False
    texto_lower = texto.lower()
    prohibidas = {
        "opcion", "opción", "lista", "última", "ultima",
        "primera", "segunda", "tercera", "ver", "verla", "verlo",
        "quiero", "mostrar", "opciones", "más", "mas", "casa",
        "venta", "alquiler", "propiedad"
    }
    return not any(palabra in texto_lower for palabra in prohibidas)


def extraer_datos_lead(mensaje: str, lead_actual: dict, sender: str = "") -> dict:
    lead = lead_actual.copy()

    correo_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", mensaje, re.IGNORECASE)
    if correo_match:
        lead["correo"] = correo_match.group(0).strip()

    telefono_match = re.search(r"(\+?\d[\d\s\-\(\)]{7,}\d)", mensaje)
    if telefono_match:
        lead["whatsapp"] = limpiar_telefono(telefono_match.group(1))

    if not lead.get("whatsapp"):
        sender_limpio = limpiar_telefono(sender)
        if len(sender_limpio) >= 7:
            lead["whatsapp"] = sender_limpio

    palabras = re.findall(r"[A-Za-zÀ-ÖØ-ÿ'´-]+", mensaje or "")
    palabras_limpias = [
        p for p in palabras
        if normalizar_texto(p) not in PALABRAS_INVALIDAS_NOMBRE
    ]

    nombre_match = re.search(
        r"(?:soy|me llamo|mi nombre es)\s+([A-Za-zÀ-ÖØ-ÿ'´-]+(?:\s+[A-Za-zÀ-ÖØ-ÿ'´-]+){1,4})",
        mensaje,
        re.IGNORECASE
    )

    nombre_candidato = None

    if nombre_match:
        nombre_candidato = nombre_match.group(1).strip()
    elif es_nombre_directo(mensaje, palabras_limpias):
        nombre_candidato = " ".join(palabras_limpias[:4]).strip()

    if nombre_candidato:
        lead["nombre"] = nombre_candidato

    return lead


def lead_completo(lead: dict) -> bool:
    return bool(
        lead.get("nombre")
        and len(lead.get("nombre", "").split()) >= 2
        and lead.get("correo")
        and "@" in lead.get("correo", "")
        and "." in lead.get("correo", "")
        and lead.get("whatsapp")
    )


def construir_pedido_dato(dato: str, lead: dict) -> str:
    nombre = lead.get("nombre", "")
    mensajes = {
        "nombre": (
            "¡Me encanta que quieras avanzar! 😊 Para asignarte un asesor y personalizar todo,"
            " ¿me compartes tu nombre completo (nombre y apellido)?"
        ),
        "correo": (
            f"Gracias {nombre.split()[0] if nombre else 'amigo/a'} 🙌 Ahora necesito tu correo electrónico"
            " para enviarte opciones adicionales y documentos complementarios."
        ),
        "whatsapp": (
            "Perfecto, casi listo. ¿Me puedes confirmar tu número de WhatsApp?"
            " Así el asesor podrá contactarte rápidamente y enviarte fotos o videos."
        )
    }
    return mensajes.get(dato, "Por favor compárteme la información solicitada.")


def proximo_dato_lead(lead: dict) -> str:
    nombre = lead.get("nombre", "")
    if not nombre or len(nombre.split()) < 2:
        return "nombre"
    if not lead.get("correo"):
        return "correo"
    if not lead.get("whatsapp"):
        return "whatsapp"
    return None


def construir_resumen_necesidad(filtros: dict) -> str:
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
    return "\n".join([f"- {p}" for p in partes]) or "No especificado"


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
# PETICIONES Y RESPUESTAS
# ============================================================

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        api_key_header = request.headers.get("x-api-key")
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

        estado = obtener_estado_usuario(sender)

        mensaje_previo = estado.get("mensaje_previo", "")
        if mensaje_cliente == mensaje_previo and estado.get("ultima_respuesta"):
            return {
                "replies": [
                    {
                        "message": estado["ultima_respuesta"].replace("**", "*")
                    }
                ]
            }
        estado["mensaje_previo"] = mensaje_cliente

        if "mercadolibre.com.ve/mlv" in mensaje_cliente.lower():
            respuesta = (
                "¡Hola! 👋 Esta propiedad se encuentra disponible en el precio publicado. "
                "¿Quieres agendar una visita?"
            )
            return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

        if es_consulta_reclutamiento(mensaje_cliente):
            respuesta = respuesta_reclutamiento()
            return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

        await garantizar_inventario_actualizado()
        sincronizar_google_sheet()

        inventario = cache["inventario"]
        if not inventario:
            respuesta = (
                "Estamos actualizando nuestro inventario para ofrecerte las mejores opciones."
                " Por favor, escríbeme de nuevo en unos minutos y te comparto todo. 😊"
            )
            return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

        primera_interaccion = (
            sender not in memoria_conversaciones
            or len(memoria_conversaciones.get(sender, [])) == 0
        )

        if (
            estado["estado"] == ESTADO_MOSTRANDO_PROPIEDADES
            and usuario_solicita_ajuste(mensaje_cliente)
        ):
            respuesta = (
                "Entiendo que ninguna opción fue perfecta. ¿Quieres que ajustemos la zona, el presupuesto o algunas características antes de mostrarte nuevas opciones?"
            )
            return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

        if (
            estado["estado"] == ESTADO_MOSTRANDO_PROPIEDADES
            and usuario_pide_mas_opciones(mensaje_cliente)
        ):
            propiedades = elegir_top_n_propiedades(
                inventario,
                estado["filtros"],
                n=3,
                excluir_ids=estado["propiedades_enviadas"]
            )
            if not propiedades:
                respuesta = (
                    "Por ahora no encontré más opciones exactamente con esos criterios."
                    " ¿Quieres que ampliemos la zona o ajustemos algo del presupuesto?"
                )
            else:
                estado["propiedades_enviadas"].extend([p.get("id") for p in propiedades])
                es_colega = estado["rol"] == "colega_inmobiliario"
                fichas = [
                    formatear_ficha_propiedad(p, es_colega=es_colega)
                    for p in propiedades
                ]
                respuesta = (
                    "Claro, te comparto 3 opciones adicionales:\n\n"
                    + "\n\n".join(fichas)
                )
                if estado["rol"] == "cliente":
                    respuesta += construir_cierre_cliente()
            return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

        if (
            estado["estado"] == ESTADO_MOSTRANDO_PROPIEDADES
            and estado["rol"] == "cliente"
            and sender not in clientes_procesados
            and (
                usuario_muestra_interes(mensaje_cliente)
                or usuario_envia_datos_contacto(mensaje_cliente)
            )
        ):
            estado["estado"] = ESTADO_CAPTURANDO_LEAD

        if estado["estado"] == ESTADO_CAPTURANDO_LEAD:
            estado["lead"] = extraer_datos_lead(
                mensaje_cliente,
                estado["lead"],
                sender
            )
            dato_faltante = proximo_dato_lead(estado["lead"])
            if dato_faltante:
                if estado["ultimo_pedido_dato"] != dato_faltante:
                    respuesta = construir_pedido_dato(dato_faltante, estado["lead"])
                    estado["ultimo_pedido_dato"] = dato_faltante
                else:
                    respuesta = (
                        "Estoy esperando ese dato para continuar. ¿Me lo puedes enviar ahora?"
                    )
                return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)
            agente = asignar_agente_round_robin()
            resumen = construir_resumen_necesidad(estado["filtros"])
            if agente:
                enviar_notificaciones_telegram(
                    agente,
                    estado["lead"],
                    resumen
                )
                clientes_procesados.add(sender)
                estado["estado"] = ESTADO_LEAD_COMPLETO
                estado["ultimo_pedido_dato"] = None
                respuesta = (
                    f"¡Perfecto, {estado['lead']['nombre'].split()[0]}! ✨ "
                    f"He registrado tus datos y asigné tu solicitud a *{agente.get('nombre', 'uno de nuestros asesores')}*. "
                    "Te contactará muy pronto para coordinar tu atención personalizada. 🤝"
                )
            else:
                respuesta = (
                    "¡Perfecto! Ya tengo tus datos. Estamos asignando un asesor disponible "
                    "para que te contacte lo antes posible. 😊"
                )
            return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

        estado["filtros"] = extraer_filtros_busqueda(
            mensaje_cliente,
            estado["filtros"],
            memoria_conversaciones.get(sender, [])
        )

        campo_faltante = obtener_siguiente_campo_faltante(estado["filtros"])
        if campo_faltante:
            estado["estado"] = ESTADO_COMPLETANDO_FILTROS
            pregunta = construir_pregunta_campo(
                campo_faltante,
                es_inicio=primera_interaccion
            )
            if (
                es_mensaje_poco_informativo(mensaje_cliente)
                or estado.get("ultima_respuesta") == pregunta
            ):
                respuesta = mensaje_recordatorio_filtro(campo_faltante)
            else:
                respuesta = pregunta
            return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

        rol_detectado = detectar_rol_por_respuesta_directa(mensaje_cliente)
        if rol_detectado != "desconocido":
            estado["rol"] = rol_detectado

        if not estado["rol"]:
            if not estado["rol_preguntado"]:
                estado["estado"] = ESTADO_DEFINIENDO_ROL
                estado["rol_preguntado"] = True
                respuesta = construir_pregunta_rol()
                return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)
            else:
                rol_ia = detectar_rol_por_respuesta_directa(mensaje_cliente)
                if rol_ia != "desconocido":
                    estado["rol"] = rol_ia
                else:
                    estado["rol"] = "cliente"

        propiedades = elegir_top_n_propiedades(
            inventario,
            estado["filtros"],
            n=3,
            excluir_ids=estado["propiedades_enviadas"]
        )
        estado["estado"] = ESTADO_MOSTRANDO_PROPIEDADES

        if not propiedades:
            respuesta = (
                "Revisé nuestro inventario activo y no encontré una coincidencia exacta."
                " ¿Quieres ampliar la zona o ajustamos el presupuesto para seguir buscando?"
            )
            return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

        estado["propiedades_enviadas"].extend([p.get("id") for p in propiedades])
        es_colega = estado["rol"] == "colega_inmobiliario"
        fichas = [
            formatear_ficha_propiedad(p, es_colega=es_colega)
            for p in propiedades
        ]
        resumen = construir_resumen_necesidad(estado["filtros"])
        resumen_prev = f"Entonces estás buscando:\n{resumen}\n\n" if resumen else ""
        if es_colega:
            respuesta = (
                f"{resumen_prev}"
                "¡Perfecto, colega! Revisé nuestro inventario y estas son las 3 opciones que más se acercan. "
                "Te incluyo los datos del captador:\n\n"
                + "\n\n".join(fichas)
            )
        else:
            respuesta = (
                f"{resumen_prev}"
                "¡Perfecto! Revisé nuestro inventario y estas son las 3 opciones que más se acercan a lo que buscas:\n\n"
                + "\n\n".join(fichas)
                + construir_cierre_cliente()
            )
        return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

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
