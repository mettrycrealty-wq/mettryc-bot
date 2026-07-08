import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

import requests
from fastapi import FastAPI, HTTPException, Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

HORARIOS_ACTUALIZACION_INVENTARIO = [0, 12]

MODELO_PRINCIPAL = os.getenv("MODELO_PRINCIPAL", "openai/gpt-4o-mini")
MODELO_RESPALDO = os.getenv("MODELO_RESPALDO", "google/gemini-2.5-flash-lite")
MAX_TOKENS_IA = int(os.getenv("MAX_TOKENS_IA", "260"))

INTERVALO_ACTUALIZACION_SHEETS = timedelta(hours=1)
VENTANA_MENSAJE_DUPLICADO_SEGUNDOS = int(os.getenv("VENTANA_MENSAJE_DUPLICADO_SEGUNDOS", "25"))

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

memoria_conversaciones: Dict[str, List[dict]] = {}
clientes_procesados: Set[str] = set()
estado_usuarios: Dict[str, dict] = {}
locks_usuarios: Dict[str, asyncio.Lock] = {}

# ============================================================
# MEMORIA DE APRENDIZAJE
# ============================================================

aprendizaje_global = {
    "zonas": Counter(),
    "tipo_propiedad": Counter(),
    "operaciones": Counter()
}

# ============================================================
# ESTADOS
# ============================================================

ESTADO_INDAGANDO = "indagando_necesidad"
ESTADO_COMPLETANDO_FILTROS = "completando_filtros"
ESTADO_MOSTRANDO_PROPIEDADES = "mostrando_propiedades"
ESTADO_CAPTURANDO_LEAD = "capturando_lead"
ESTADO_LEAD_COMPLETO = "lead_completo"

# ============================================================
# UTILIDADES
# ============================================================

STOPWORDS_ZONA = {
    "el", "la", "los", "las", "de", "del", "en", "y",
    "urb", "urbanizacion", "urbanización", "sector", "zona",
    "ciudad", "estado", "venezuela"
}

TOKENS_DIRECCION_ZONA = {"norte", "sur", "este", "oeste", "centro"}

DOMINIOS_CORREO_TIPICOS = {
    "gmail", "hotmail", "outlook", "yahoo", "icloud",
    "protonmail", "live", "com", "net", "org"
}

SINONIMOS_TIPO = {
    "tohouse": "townhouse",
    "towhouse": "townhouse",
    "twhouse": "townhouse",
    "town house": "townhouse",
    "aparto quinta": "apartoquinta",
    "aptoquinta": "apartoquinta",
    "apartoquita": "apartoquinta",
    "apartoquitaa": "apartoquinta"
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


def normalizar_tipo_propiedad(tipo: str) -> str:
    t = normalizar_texto(tipo)
    if t in SINONIMOS_TIPO:
        return SINONIMOS_TIPO[t]
    if "townhouse" in t:
        return "townhouse"
    if "apartoquinta" in t or "aparto quinta" in t or "apartoquita" in t:
        return "apartoquinta"
    if "penthouse" in t:
        return "penthouse"
    if "apartamento" in t or t == "apto":
        return "apartamento"
    if "casa" in t:
        return "casa"
    return t


def tokens_relevantes(texto: str) -> set:
    texto_norm = normalizar_texto(texto)
    return {t for t in texto_norm.split() if t not in STOPWORDS_ZONA and len(t) > 1}


def zona_coincide(zona_buscada: str, zona_propiedad: str, ciudad_propiedad: str = "") -> bool:
    buscada_norm = normalizar_texto(zona_buscada)
    zona_norm = normalizar_texto(zona_propiedad)
    ciudad_norm = normalizar_texto(ciudad_propiedad)

    if not buscada_norm:
        return True

    texto_propiedad = f"{zona_norm} {ciudad_norm}".strip()
    if not texto_propiedad:
        return False

    tokens_busqueda = tokens_relevantes(buscada_norm)
    tokens_propiedad = tokens_relevantes(texto_propiedad)

    if not tokens_busqueda:
        return True

    direcciones_busqueda = tokens_busqueda.intersection(TOKENS_DIRECCION_ZONA)
    if direcciones_busqueda and not direcciones_busqueda.issubset(tokens_propiedad):
        return False

    if len(tokens_busqueda) >= 2:
        return tokens_busqueda.issubset(tokens_propiedad)

    unico = next(iter(tokens_busqueda))
    return (unico in tokens_propiedad) or (buscada_norm in texto_propiedad)


def convertir_entero_seguro(valor) -> int:
    try:
        if valor in [None, "", "N/D"]:
            return 0
        return int(float(valor))
    except Exception:
        return 0


def limpiar_telefono(valor: str) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def capitalizar_nombre(nombre: str) -> str:
    if not nombre:
        return ""
    palabras = [p for p in re.split(r"\s+", nombre.strip()) if p]
    return " ".join(p[:1].upper() + p[1:].lower() for p in palabras)


def primer_nombre(lead: dict) -> str:
    nombre = capitalizar_nombre(lead.get("nombre", ""))
    if not nombre:
        return "amigo/a"
    return nombre.split()[0]


def actualizar_memoria(sender: str, mensaje_usuario: str, respuesta_bot: str):
    if sender not in memoria_conversaciones:
        memoria_conversaciones[sender] = []
    memoria_conversaciones[sender].append({"role": "user", "content": mensaje_usuario})
    memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_bot})
    if len(memoria_conversaciones[sender]) > 30:
        memoria_conversaciones[sender] = memoria_conversaciones[sender][-30:]


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
            "lead": {
                "nombre": "",
                "correo": "",
                "whatsapp": ""
            },
            "ultimo_pedido_dato": None,
            "mensaje_previo": "",
            "ultima_respuesta": "",
            "ultimo_mensaje_ts": 0.0,
            "ultimo_hash_filtros_mostrados": ""
        }
    return estado_usuarios[sender]


def obtener_lock_usuario(sender: str) -> asyncio.Lock:
    if sender not in locks_usuarios:
        locks_usuarios[sender] = asyncio.Lock()
    return locks_usuarios[sender]


def enviar_respuesta(sender: str, mensaje_cliente: str, respuesta: str, estado: dict = None):
    if estado is not None:
        estado["ultima_respuesta"] = respuesta
    actualizar_memoria(sender, mensaje_cliente, respuesta)
    mensaje_final = respuesta.replace("**", "*")
    return {"replies": [{"message": mensaje_final}]}


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


def hash_filtros(filtros: dict) -> str:
    try:
        return json.dumps(filtros, sort_keys=True, ensure_ascii=False)
    except Exception:
        return str(filtros)


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

    t = normalizar_texto(texto)

    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(mil|k)\b", t)
    if match:
        return float(match.group(1).replace(",", ".")) * 1000

    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(millon|millones|mm)\b", t)
    if match:
        return float(match.group(1).replace(",", ".")) * 1_000_000

    candidatos = re.findall(r"(?:usd|us|dolares|dolar|\$)?\s*([0-9][0-9\.,]{3,})", t)
    for c in candidatos:
        limpio = c.replace(" ", "")
        if "." in limpio and "," in limpio:
            if limpio.rfind(",") > limpio.rfind("."):
                limpio = limpio.replace(".", "").replace(",", ".")
            else:
                limpio = limpio.replace(",", "")
        elif "." in limpio:
            partes = limpio.split(".")
            if len(partes[-1]) == 3:
                limpio = limpio.replace(".", "")
        elif "," in limpio:
            partes = limpio.split(",")
            if len(partes[-1]) == 3:
                limpio = limpio.replace(",", "")
            else:
                limpio = limpio.replace(",", ".")

        try:
            val = float(limpio)
            if val >= 1000:
                return val
        except Exception:
            continue

    return None


# ============================================================
# ACTUALIZACIÓN INVENTARIO
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
    return ahora >= proxima


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
                response = requests.get("https://api.wasi.co/v1/property/search", params=params, timeout=25)
                response.raise_for_status()
                data = response.json()

                contador_pagina = 0
                for key, value in data.items():
                    if isinstance(value, dict) and key.isdigit():
                        contador_pagina += 1
                        id_prop = value.get("id_property")
                        user_data = value.get("user_data", {})
                        captador_nombre = (
                            f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}"
                        ).strip() or "Asesor Mettryc"

                        propiedades.append({
                            "id": str(id_prop),
                            "titulo": value.get("title", "Propiedad sin título"),
                            "ciudad": value.get("city_label", "N/D"),
                            "zona": value.get("zone_label", "N/D"),
                            "venta": value.get("sale_price_label", "N/D"),
                            "renta": value.get("rent_price_label", "N/D"),
                            "precio_venta_float": parsear_precio_wasi(value.get("sale_price"), value.get("sale_price_label")),
                            "precio_renta_float": parsear_precio_wasi(value.get("rent_price"), value.get("rent_price_label")),
                            "area": value.get("area", "N/D"),
                            "habitaciones": value.get("bedrooms", "N/D"),
                            "banos": value.get("bathrooms", "N/D"),
                            "tipo_propiedad_wasi": value.get("type_label", "Indefinido"),
                            "enlace": f"https://www.mettryc.com/inmueble/{id_prop}",
                            "captador_propiedad": captador_nombre,
                            "telefono_captador_wasi": user_data.get("phone", "")
                        })

                exito_pagina = True
                logger.info(f"Descargadas {contador_pagina} propiedades. Total acumulado: {len(propiedades)}")

                if contador_pagina < take:
                    logger.info(f"Descarga completa desde Wasi. Total: {len(propiedades)}")
                    return propiedades

                skip += take
                time.sleep(1.2)

            except Exception as e:
                intentos += 1
                logger.warning(f"Intento {intentos}/3 fallido obteniendo inventario: {e}")
                time.sleep(2)

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
        response = requests.get(script_url, timeout=12)
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
                if nombre and telefono:
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
    sheets_cache["ultimo_indice"] = (sheets_cache["ultimo_indice"] + 1) % len(lista_agentes)
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
                    "max_tokens": max_tokens,
                    "temperature": 0.1
                },
                timeout=12
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices", [])
            if choices:
                contenido = choices[0].get("message", {}).get("content", "")
                if contenido and contenido.strip():
                    return contenido.strip()
        except Exception as e:
            logger.warning(f"Error consultando IA con {modelo}: {e}")
    return fallback or ""


def extraer_json_de_texto(texto: str) -> dict:
    if not texto:
        return {}
    try:
        return json.loads(texto)
    except Exception:
        pass
    try:
        ini = texto.find("{")
        fin = texto.rfind("}")
        if ini != -1 and fin != -1 and fin > ini:
            return json.loads(texto[ini:fin + 1])
    except Exception:
        pass
    return {}


def normalizar_filtros_post_ia(mensaje_usuario: str, filtros: dict) -> dict:
    filtros = filtros.copy()
    filtros["tipo_propiedad"] = normalizar_tipo_propiedad(filtros.get("tipo_propiedad", ""))

    tipo_op = normalizar_texto(filtros.get("tipo_operacion", ""))
    if tipo_op in ["comprar", "compra", "venta", "vendo", "para comprar"]:
        filtros["tipo_operacion"] = "venta"
    elif tipo_op in ["renta", "alquiler", "alquilar", "arrendamiento"]:
        filtros["tipo_operacion"] = "alquiler"

    if filtros.get("presupuesto") is None:
        p = parsear_presupuesto_texto(mensaje_usuario)
        if p:
            filtros["presupuesto"] = p

    if not isinstance(filtros.get("caracteristicas"), list):
        filtros["caracteristicas"] = []

    if not filtros["caracteristicas"]:
        filtros["caracteristicas"] = ["sin características adicionales"]

    registrar_aprendizaje(filtros)
    return filtros


def fusionar_filtros_ia(filtros_actuales: dict, filtros_ia: dict, mensaje_usuario: str) -> dict:
    merged = (filtros_actuales or {}).copy()
    data = filtros_ia or {}

    for campo in ["tipo_propiedad", "tipo_operacion", "zona"]:
        v = data.get(campo)
        if isinstance(v, str) and v.strip():
            merged[campo] = v.strip()

    for campo in ["presupuesto", "habitaciones_min", "banos_min"]:
        v = data.get(campo)
        if v not in [None, "", []]:
            try:
                if campo == "presupuesto":
                    merged[campo] = float(v)
                else:
                    merged[campo] = int(v)
            except Exception:
                pass

    car = data.get("caracteristicas")
    if isinstance(car, list) and car:
        actuales = merged.get("caracteristicas", [])
        merged["caracteristicas"] = list(dict.fromkeys(actuales + [str(c).strip() for c in car if str(c).strip()]))

    return normalizar_filtros_post_ia(mensaje_usuario, merged)


def obtener_siguiente_campo_faltante(filtros: dict):
    # campos principales para iniciar búsqueda
    if not filtros.get("tipo_propiedad"):
        return "tipo_propiedad"
    if not filtros.get("tipo_operacion"):
        return "tipo_operacion"
    if not filtros.get("zona"):
        return "zona"
    if filtros.get("presupuesto") is None:
        return "presupuesto"
    return None


def mensaje_campo_faltante(campo: str) -> str:
    mapa = {
        "tipo_propiedad": "Para seguir, dime qué tipo de propiedad buscas (casa, apartamento, townhouse, etc.).",
        "tipo_operacion": "Para seguir, ¿la deseas para venta o alquiler?",
        "zona": "Para seguir, indícame la zona o urbanización exacta donde deseas buscar.",
        "presupuesto": "Para seguir, compárteme tu presupuesto máximo aproximado."
    }
    return mapa.get(campo, "Compárteme un dato más para continuar con la búsqueda.")


def diagnostico_ia_con_memoria(sender: str, mensaje_usuario: str, estado: dict) -> dict:
    historial = memoria_conversaciones.get(sender, [])[-10:]
    filtros_actuales = estado.get("filtros", {})
    rol_actual = estado.get("rol") or "desconocido"

    prompt = f"""
Eres Paty, asistente inmobiliaria VIP de Mettryc Realty.
Tu misión es entender al usuario con contexto conversacional y errores de escritura.
Debes responder SOLO JSON válido:

{{
  "respuesta": "texto breve y natural para el usuario",
  "rol": "cliente|colega_inmobiliario|desconocido",
  "intencion": "continuar_busqueda|pedir_mas_opciones|mostrar_interes|ajustar_filtros|otro",
  "filtros": {{
    "tipo_propiedad": "",
    "tipo_operacion": "",
    "zona": "",
    "presupuesto": null,
    "habitaciones_min": null,
    "banos_min": null,
    "caracteristicas": []
  }}
}}

Reglas:
1) Comprende el contexto completo y corrige errores ortográficos del usuario.
2) Extrae/actualiza solo los datos que el usuario sí dijo.
3) Tipos especiales:
   - townhouse y apartoquinta son estilo de casa.
   - penthouse es estilo de apartamento.
4) Zona debe quedar lo más exacta posible (ej: "Trigal Norte" no es "Trigal Sur").
5) Si faltan datos para buscar (tipo_propiedad, tipo_operacion, zona, presupuesto), en "respuesta" pide SOLO el dato faltante más importante, de forma natural y concreta.
6) Si ya están los datos base, en "respuesta" confirma que procederás a buscar opciones.
7) Si detectas lenguaje de colega/aliado inmobiliario, marca rol "colega_inmobiliario".
8) Si detectas deseo de avanzar con una propiedad, marca intencion "mostrar_interes".
9) Si pide más opciones, marca intencion "pedir_mas_opciones".

Estado actual:
rol_actual: {rol_actual}
filtros_actuales: {json.dumps(filtros_actuales, ensure_ascii=False)}
"""

    mensajes = [{"role": "system", "content": prompt}]
    if historial:
        mensajes.extend(historial)
    mensajes.append({"role": "user", "content": mensaje_usuario})

    raw = consultar_ia(mensajes, max_tokens=320, fallback="")
    parsed = extraer_json_de_texto(raw)

    if not isinstance(parsed, dict):
        parsed = {}

    respuesta = str(parsed.get("respuesta", "")).strip()
    rol = str(parsed.get("rol", "desconocido")).strip().lower()
    intencion = str(parsed.get("intencion", "otro")).strip().lower()
    filtros_ia = parsed.get("filtros", {}) if isinstance(parsed.get("filtros"), dict) else {}

    if rol not in {"cliente", "colega_inmobiliario", "desconocido"}:
        rol = "desconocido"
    if intencion not in {"continuar_busqueda", "pedir_mas_opciones", "mostrar_interes", "ajustar_filtros", "otro"}:
        intencion = "otro"

    filtros = fusionar_filtros_ia(filtros_actuales, filtros_ia, mensaje_usuario)
    faltante = obtener_siguiente_campo_faltante(filtros)

    if not respuesta and faltante:
        respuesta = mensaje_campo_faltante(faltante)
    elif not respuesta:
        respuesta = "Perfecto, ya tengo la base de tu búsqueda. Voy a revisar las mejores opciones."

    return {
        "respuesta": respuesta,
        "rol": rol,
        "intencion": intencion,
        "filtros": filtros,
        "faltante": faltante
    }


# ============================================================
# REGLAS NEGOCIO PROPIEDADES
# ============================================================

def coincide_tipo_propiedad(propiedad: dict, tipo_filtro: str) -> bool:
    tipo = normalizar_tipo_propiedad(tipo_filtro)
    titulo = normalizar_texto(propiedad.get("titulo", ""))
    tipo_wasi = normalizar_texto(propiedad.get("tipo_propiedad_wasi", ""))

    if not tipo:
        return True

    if tipo == "townhouse":
        return "townhouse" in titulo

    if tipo == "apartoquinta":
        return ("apartoquinta" in titulo) or ("aparto quinta" in titulo) or ("apartoquita" in titulo)

    if tipo == "casa":
        return (
            "casa" in tipo_wasi
            or "casa" in titulo
            or "townhouse" in titulo
            or "apartoquinta" in titulo
            or "aparto quinta" in titulo
            or "apartoquita" in titulo
        )

    if tipo == "penthouse":
        return "penthouse" in titulo

    if tipo == "apartamento":
        return ("apartamento" in tipo_wasi) or ("apartamento" in titulo) or ("penthouse" in titulo)

    return (tipo in tipo_wasi) or (tipo in titulo)


def propiedad_contiene_caracteristicas(propiedad: dict, caracteristicas: list) -> int:
    texto = normalizar_texto(
        f"{propiedad.get('titulo', '')} {propiedad.get('zona', '')} "
        f"{propiedad.get('ciudad', '')} {propiedad.get('tipo_propiedad_wasi', '')}"
    )
    score = 0
    for c in caracteristicas:
        cn = normalizar_texto(c)
        if not cn or cn == "sin caracteristicas adicionales":
            continue
        if cn in texto:
            score += 2
    return score


def elegir_top_n_propiedades(inventario, filtros, n=3, excluir_ids=None):
    excluir_ids = set(str(x) for x in (excluir_ids or []))
    tipo_prop = normalizar_tipo_propiedad(filtros.get("tipo_propiedad", ""))
    tipo_op = normalizar_texto(filtros.get("tipo_operacion", ""))
    zona = filtros.get("zona", "")
    presupuesto = filtros.get("presupuesto")
    habitaciones_min = filtros.get("habitaciones_min")
    banos_min = filtros.get("banos_min")
    caracteristicas = filtros.get("caracteristicas", [])

    propiedades = [p for p in inventario if str(p.get("id")) not in excluir_ids]

    if tipo_op == "venta":
        propiedades = [p for p in propiedades if p.get("precio_venta_float", 0) > 0]
    elif tipo_op == "alquiler":
        propiedades = [p for p in propiedades if p.get("precio_renta_float", 0) > 0]

    if tipo_prop:
        propiedades = [p for p in propiedades if coincide_tipo_propiedad(p, tipo_prop)]

    if zona:
        propiedades = [p for p in propiedades if zona_coincide(zona, p.get("zona", ""), p.get("ciudad", ""))]

    if presupuesto is not None:
        if tipo_op == "venta":
            propiedades = [p for p in propiedades if 0 < p.get("precio_venta_float", 0) <= presupuesto]
        elif tipo_op == "alquiler":
            propiedades = [p for p in propiedades if 0 < p.get("precio_renta_float", 0) <= presupuesto]

    if habitaciones_min is not None:
        propiedades = [p for p in propiedades if convertir_entero_seguro(p.get("habitaciones")) >= int(habitaciones_min)]
    if banos_min is not None:
        propiedades = [p for p in propiedades if convertir_entero_seguro(p.get("banos")) >= int(banos_min)]

    def score(p):
        puntos = 0
        if zona and zona_coincide(zona, p.get("zona", ""), p.get("ciudad", "")):
            puntos += 8
        if tipo_prop and coincide_tipo_propiedad(p, tipo_prop):
            puntos += 6

        precio = p.get("precio_venta_float", 0) if tipo_op == "venta" else p.get("precio_renta_float", 0)
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
        etiqueta = "Renta"
    else:
        precio = propiedad.get("venta", "N/D")
        etiqueta = "Venta"

    area = propiedad.get("area", "N/D")
    area_txt = f"{area}m²" if str(area).strip() not in {"", "N/D", "None"} else "N/D"

    lineas = [
        f"*{propiedad.get('titulo', 'Propiedad sin título')}*",
        f"📍 Zona: {propiedad.get('zona', 'N/D')} | Ciudad: {propiedad.get('ciudad', 'N/D')}",
        f"💰 {etiqueta}: {precio}",
        f"📐 Área: {area_txt} | 🛏️ Habs: {propiedad.get('habitaciones', 'N/D')} | 🛁 Baños: {propiedad.get('banos', 'N/D')}",
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


def construir_cierre_cliente() -> str:
    return (
        "\n\n¿Alguna te interesa para coordinar una visita o recibir atención personalizada? "
        "Puedo asignarte rápidamente un asesor de Mettryc Realty. 😊"
    )


# ============================================================
# INTENCIONES AUXILIARES (fallback)
# ============================================================

def usuario_pide_mas_opciones(mensaje: str) -> bool:
    texto = normalizar_texto(mensaje)
    patrones = [
        "ver mas", "muestrame mas", "muestrame más", "otras opciones", "otra opcion",
        "otra opción", "tienes mas", "tienes más", "mas opciones", "más opciones",
        "quiero ver mas", "quiero ver más", "dame mas", "dame más"
    ]
    return any(p in texto for p in patrones)


def usuario_muestra_interes(mensaje: str) -> bool:
    texto = normalizar_texto(mensaje)
    patrones = [
        "me interesa", "me gusta", "quiero verla", "quiero visitar", "agendar", "cita",
        "opcion 1", "opcion 2", "opcion 3", "quiero avanzar", "quiero coordinar",
        "quiero ver la", "quiero ver el"
    ]
    return any(p in texto for p in patrones)


def usuario_envia_datos_contacto(mensaje: str) -> bool:
    tiene_correo = bool(re.search(r"[\w\.-]+@[\w\.-]+\.\w+", mensaje, re.IGNORECASE))
    tiene_telefono = bool(re.search(r"\+?\d[\d\s\-\(\)]{7,}\d", mensaje))
    return tiene_correo or tiene_telefono


# ============================================================
# LEADS
# ============================================================

PALABRAS_INVALIDAS_NOMBRE = {
    "me", "interesa", "quiero", "verla", "visitar", "cita", "agendar",
    "asesor", "hola", "buenas", "gracias", "opcion", "opción", "primera",
    "segunda", "tercera", "lista", "ultima", "última", "casa", "casas",
    "venta", "alquiler", "propiedad", "mas", "más", "correo", "email",
    "gmail", "hotmail", "outlook", "yahoo", "com", "net", "org"
}


def es_nombre_directo(mensaje: str, palabras_validas: list) -> bool:
    texto = (mensaje or "").strip()
    if not texto or len(texto) > 60:
        return False
    if re.search(r"@|\d", texto):
        return False
    if len(palabras_validas) < 2 or len(palabras_validas) > 4:
        return False

    texto_lower = normalizar_texto(texto)
    prohibidas = {
        "opcion", "opción", "lista", "ultima", "última", "primera", "segunda", "tercera",
        "ver", "verla", "verlo", "quiero", "mostrar", "opciones", "mas", "más",
        "casa", "venta", "alquiler", "propiedad", "gmail", "hotmail", "outlook", "correo"
    }
    return not any(p in texto_lower for p in prohibidas)


def sanitizar_nombre_candidato(nombre: str) -> str:
    if not nombre:
        return ""
    palabras = re.findall(r"[A-Za-zÀ-ÖØ-ÿ'´-]+", nombre)
    limpias = []
    for p in palabras:
        pn = normalizar_texto(p)
        if pn in PALABRAS_INVALIDAS_NOMBRE or pn in DOMINIOS_CORREO_TIPICOS or len(pn) < 2:
            continue
        limpias.append(p)
    if len(limpias) < 2:
        return ""
    return capitalizar_nombre(" ".join(limpias[:4]))


def extraer_datos_lead(mensaje: str, lead_actual: dict, sender: str = "") -> dict:
    lead = lead_actual.copy()
    texto = mensaje or ""

    correo_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", texto, re.IGNORECASE)
    if correo_match:
        lead["correo"] = correo_match.group(0).strip().lower()

    telefono_match = re.search(r"(\+?\d[\d\s\-\(\)]{7,}\d)", texto)
    if telefono_match:
        lead["whatsapp"] = limpiar_telefono(telefono_match.group(1))

    if not lead.get("whatsapp"):
        sender_limpio = limpiar_telefono(sender)
        if len(sender_limpio) >= 7:
            lead["whatsapp"] = sender_limpio

    nombre_match = re.search(
        r"(?:soy|me llamo|mi nombre es)\s+([A-Za-zÀ-ÖØ-ÿ'´-]+(?:\s+[A-Za-zÀ-ÖØ-ÿ'´-]+){0,4})",
        texto,
        re.IGNORECASE
    )

    nombre_candidato = ""
    if nombre_match:
        nombre_candidato = sanitizar_nombre_candidato(nombre_match.group(1))
    else:
        if not correo_match and not telefono_match:
            palabras = re.findall(r"[A-Za-zÀ-ÖØ-ÿ'´-]+", texto)
            palabras_limpias = [
                p for p in palabras
                if normalizar_texto(p) not in PALABRAS_INVALIDAS_NOMBRE
                and normalizar_texto(p) not in DOMINIOS_CORREO_TIPICOS
            ]
            if es_nombre_directo(texto, palabras_limpias):
                nombre_candidato = sanitizar_nombre_candidato(" ".join(palabras_limpias[:4]))

    if nombre_candidato:
        lead["nombre"] = nombre_candidato

    return lead


def proximo_dato_lead(lead: dict) -> str:
    nombre = lead.get("nombre", "")
    if not nombre or len(nombre.split()) < 2:
        return "nombre"
    if not lead.get("correo"):
        return "correo"
    if not lead.get("whatsapp"):
        return "whatsapp"
    return None


def construir_pedido_dato(dato: str, lead: dict) -> str:
    saludado = primer_nombre(lead)
    mensajes = {
        "nombre": "¡Excelente! Para asignarte un asesor, compárteme tu *nombre completo* (nombre y apellido).",
        "correo": f"Gracias {saludado} 🙌 Ahora necesito tu *correo electrónico*.",
        "whatsapp": "Perfecto. ¿Me confirmas tu *número de WhatsApp* para que el asesor te contacte?"
    }
    return mensajes.get(dato, "Compárteme ese dato para continuar.")


def construir_recordatorio_dato_faltante(dato: str) -> str:
    mensajes = {
        "nombre": "Me falta tu *nombre completo* (nombre y apellido).",
        "correo": "Me falta tu *correo electrónico*.",
        "whatsapp": "Me falta tu *número de WhatsApp*."
    }
    return mensajes.get(dato, "Me falta ese dato.") + " ¿Me lo compartes ahora?"


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
# TELEGRAM (SE MANTIENE)
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

    nombre_cliente = capitalizar_nombre(lead.get("nombre", "")) or "Cliente sin nombre"
    correo = lead.get("correo", "N/D")
    whatsapp = limpiar_telefono(lead.get("whatsapp", ""))
    link_wa = f"https://wa.me/{whatsapp}" if whatsapp else "N/D"

    nombre_agente = agente.get("nombre", "Agente sin nombre") if agente else "Agente no asignado"

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
            response = requests.post(url_tg, json={"chat_id": chat_id, "text": mensaje}, timeout=8)
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
        "quiero unirme", "trabajar con ustedes", "ser agente", "ser asesor",
        "mettryc team", "reclutamiento", "curso", "comision", "comisiones"
    ]
    return any(p in texto for p in patrones)


def respuesta_reclutamiento() -> str:
    return (
        "¡Qué emoción que quieras unirte al Mettryc Team! 🚀 "
        "Aquí tienes la información: https://mettryc.com/blog/unete-al-mettryc-team-y-gana-desde-el-80-al-100-de-comision/18270?page=1\n\n"
        "El curso inicial cuesta $60 y dura 5 días, de 9am a 12pm."
    )


# ============================================================
# WEBHOOK
# ============================================================

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        api_key_header = request.headers.get("x-api-key")
        valid_api_keys = [k.strip() for k in os.getenv("API_KEYS_AGENTES", "").split(",") if k.strip()]
        if api_key_header not in valid_api_keys or not api_key_header:
            raise HTTPException(status_code=403, detail="Acceso denegado")

        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = str(payload.get("sender", "")).strip()
        mensaje_cliente = str(payload.get("message", "")).strip()

        if not mensaje_cliente:
            return {"replies": []}

        lock = obtener_lock_usuario(sender)
        async with lock:
            estado = obtener_estado_usuario(sender)
            ahora_ts = time.time()

            if (
                mensaje_cliente == estado.get("mensaje_previo", "")
                and (ahora_ts - float(estado.get("ultimo_mensaje_ts", 0.0))) <= VENTANA_MENSAJE_DUPLICADO_SEGUNDOS
            ):
                logger.info(f"Mensaje duplicado ignorado para sender={sender}")
                return {"replies": []}

            estado["mensaje_previo"] = mensaje_cliente
            estado["ultimo_mensaje_ts"] = ahora_ts

            if "mercadolibre.com.ve/mlv" in mensaje_cliente.lower():
                respuesta = "¡Hola! 👋 Esta propiedad se encuentra disponible en el precio publicado. ¿Quieres agendar una visita?"
                return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

            if es_consulta_reclutamiento(mensaje_cliente):
                return enviar_respuesta(sender, mensaje_cliente, respuesta_reclutamiento(), estado)

            await garantizar_inventario_actualizado()
            sincronizar_google_sheet()

            inventario = cache["inventario"]
            if not inventario:
                respuesta = "Estamos actualizando nuestro inventario. Escríbeme en unos minutos y te comparto opciones. 😊"
                return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

            # ------------------------------------------------------------
            # FLUJO LEAD (si ya entró en captura)
            # ------------------------------------------------------------
            if estado["estado"] == ESTADO_CAPTURANDO_LEAD:
                estado["lead"] = extraer_datos_lead(mensaje_cliente, estado["lead"], sender)
                dato_faltante = proximo_dato_lead(estado["lead"])

                if dato_faltante:
                    if estado["ultimo_pedido_dato"] != dato_faltante:
                        respuesta = construir_pedido_dato(dato_faltante, estado["lead"])
                        estado["ultimo_pedido_dato"] = dato_faltante
                    else:
                        respuesta = construir_recordatorio_dato_faltante(dato_faltante)
                    return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

                agente = asignar_agente_round_robin()
                resumen = construir_resumen_necesidad(estado["filtros"])

                if agente:
                    enviar_notificaciones_telegram(agente, estado["lead"], resumen)
                    clientes_procesados.add(sender)
                    estado["estado"] = ESTADO_LEAD_COMPLETO
                    estado["ultimo_pedido_dato"] = None
                    saludo = primer_nombre(estado["lead"])
                    respuesta = (
                        f"¡Perfecto, {saludo}! ✨ He registrado tus datos y asigné tu solicitud a "
                        f"*{agente.get('nombre', 'uno de nuestros asesores')}*. "
                        "Te contactará muy pronto. 🤝"
                    )
                else:
                    respuesta = "¡Perfecto! Ya tengo tus datos. Estamos asignando un asesor para contactarte. 😊"

                return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

            # ------------------------------------------------------------
            # DIAGNÓSTICO INICIAL 100% IA + MEMORIA
            # ------------------------------------------------------------
            diagnostico = diagnostico_ia_con_memoria(sender, mensaje_cliente, estado)
            estado["filtros"] = diagnostico["filtros"]

            if diagnostico["rol"] != "desconocido":
                estado["rol"] = diagnostico["rol"]

            intencion = diagnostico["intencion"]
            faltante = diagnostico["faltante"]

            # Si pide más opciones y ya estaba viendo propiedades
            if estado["estado"] == ESTADO_MOSTRANDO_PROPIEDADES and (
                intencion == "pedir_mas_opciones" or usuario_pide_mas_opciones(mensaje_cliente)
            ):
                props = elegir_top_n_propiedades(
                    inventario, estado["filtros"], n=3, excluir_ids=estado["propiedades_enviadas"]
                )
                if not props:
                    respuesta = "No encontré más opciones exactas con esos criterios. Si quieres, ajustamos zona o presupuesto."
                    return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

                estado["propiedades_enviadas"].extend([p.get("id") for p in props if p.get("id")])
                es_colega = estado.get("rol") == "colega_inmobiliario"
                fichas = [formatear_ficha_propiedad(p, es_colega=es_colega) for p in props]
                respuesta = "Te comparto 3 opciones adicionales:\n\n" + "\n\n".join(fichas)
                if not es_colega:
                    respuesta += construir_cierre_cliente()
                return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

            # Si aún faltan datos de búsqueda, responde IA
            if faltante:
                estado["estado"] = ESTADO_COMPLETANDO_FILTROS
                respuesta = diagnostico["respuesta"] or mensaje_campo_faltante(faltante)
                return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

            # Si rol no definido, por negocio asumimos cliente
            if not estado.get("rol"):
                estado["rol"] = "cliente"

            # Si muestra interés y es cliente -> pasar a captura lead
            if (
                estado.get("rol") != "colega_inmobiliario"
                and sender not in clientes_procesados
                and (intencion == "mostrar_interes" or usuario_muestra_interes(mensaje_cliente) or usuario_envia_datos_contacto(mensaje_cliente))
            ):
                estado["estado"] = ESTADO_CAPTURANDO_LEAD
                estado["lead"] = extraer_datos_lead(mensaje_cliente, estado["lead"], sender)
                dato_faltante = proximo_dato_lead(estado["lead"])
                if dato_faltante:
                    estado["ultimo_pedido_dato"] = dato_faltante
                    return enviar_respuesta(sender, mensaje_cliente, construir_pedido_dato(dato_faltante, estado["lead"]), estado)

            # Si ya se mostraron opciones y no cambió filtro ni pidió más, no re-disparar listado
            hash_actual = hash_filtros(estado["filtros"])
            if (
                estado["estado"] == ESTADO_MOSTRANDO_PROPIEDADES
                and estado.get("ultimo_hash_filtros_mostrados") == hash_actual
                and intencion not in {"pedir_mas_opciones", "ajustar_filtros"}
                and not usuario_pide_mas_opciones(mensaje_cliente)
                and not usuario_muestra_interes(mensaje_cliente)
            ):
                respuesta = diagnostico["respuesta"] or "Si deseas, te muestro más opciones o ajustamos filtros."
                return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

            # ------------------------------------------------------------
            # BÚSQUEDA Y RESPUESTA DE PROPIEDADES
            # ------------------------------------------------------------
            props = elegir_top_n_propiedades(
                inventario, estado["filtros"], n=3, excluir_ids=estado["propiedades_enviadas"]
            )
            estado["estado"] = ESTADO_MOSTRANDO_PROPIEDADES
            estado["ultimo_hash_filtros_mostrados"] = hash_actual

            if not props:
                respuesta = "Revisé inventario y no encontré una coincidencia exacta. ¿Quieres que ampliemos zona o presupuesto?"
                return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

            estado["propiedades_enviadas"].extend([p.get("id") for p in props if p.get("id")])

            es_colega = estado["rol"] == "colega_inmobiliario"
            fichas = [formatear_ficha_propiedad(p, es_colega=es_colega) for p in props]
            resumen = construir_resumen_necesidad(estado["filtros"])
            resumen_prev = f"Entonces estás buscando:\n{resumen}\n\n" if resumen else ""

            if es_colega:
                respuesta = (
                    f"{resumen_prev}"
                    "¡Perfecto, colega! Estas son las 3 opciones más cercanas. Te incluyo datos del captador:\n\n"
                    + "\n\n".join(fichas)
                )
            else:
                respuesta = (
                    f"{resumen_prev}"
                    "¡Perfecto! Estas son las 3 opciones que más se acercan a tu solicitud:\n\n"
                    + "\n\n".join(fichas)
                    + construir_cierre_cliente()
                )

            return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error crítico general: {e}", exc_info=True)
        return {"replies": [{"message": "Lo siento, ocurrió un inconveniente. Por favor escribe de nuevo."}]}
