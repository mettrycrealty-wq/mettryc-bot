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
# CONFIGURACIÓN
# ============================================================

HORARIOS_ACTUALIZACION_INVENTARIO = [0, 12]

# Baja latencia
MODELO_PRINCIPAL = os.getenv("MODELO_PRINCIPAL", "google/gemini-2.5-flash-lite")
MODELO_RESPALDO = os.getenv("MODELO_RESPALDO", "openai/gpt-4o-mini")
MAX_TOKENS_IA = int(os.getenv("MAX_TOKENS_IA", "260"))

INTERVALO_ACTUALIZACION_SHEETS = timedelta(hours=1)
VENTANA_MENSAJE_DUPLICADO_SEGUNDOS = int(os.getenv("VENTANA_MENSAJE_DUPLICADO_SEGUNDOS", "25"))

# ============================================================
# CACHÉ / MEMORIA
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

aprendizaje_global = {
    "zonas": Counter(),
    "tipo_propiedad": Counter(),
    "operaciones": Counter()
}

# ============================================================
# ESTADOS
# ============================================================

ESTADO_DIAGNOSTICO_IA = "diagnostico_ia"
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

PALABRAS_INVALIDAS_NOMBRE = {
    "me", "interesa", "quiero", "verla", "visitar", "cita", "agendar",
    "asesor", "hola", "buenas", "gracias", "opcion", "opción", "primera",
    "segunda", "tercera", "lista", "ultima", "última", "casa", "casas",
    "venta", "alquiler", "propiedad", "mas", "más", "correo", "email",
    "gmail", "hotmail", "outlook", "yahoo", "com", "net", "org", "cual", "cuál",
    "la", "el", "que", "enviaste", "enviada"
}

FRASES_NO_NOMBRE = {
    "la que enviaste",
    "la ultima", "la última",
    "la primera", "la segunda", "la tercera",
    "la opcion", "la opción",
    "me interesa", "quiero esa", "quiero la ultima", "quiero la última"
}

STOPWORDS_NOMBRE_EXTRA = {
    "la", "el", "que", "enviaste", "ultima", "última", "primera", "segunda", "tercera",
    "opcion", "opción", "interesa", "quiero", "esa", "esta"
}


def normalizar_texto(texto: str) -> str:
    if not texto:
        return ""
    texto = str(texto).lower().strip()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = re.sub(r"[^a-z0-9\s@.+-]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def capitalizar_nombre(nombre: str) -> str:
    if not nombre:
        return ""
    palabras = [p for p in re.split(r"\s+", nombre.strip()) if p]
    return " ".join(p[:1].upper() + p[1:].lower() for p in palabras)


def limpiar_telefono(valor: str) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def convertir_entero_seguro(valor) -> int:
    try:
        if valor in [None, "", "N/D"]:
            return 0
        return int(float(valor))
    except Exception:
        return 0


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


def normalizar_operacion(op: str) -> str:
    t = normalizar_texto(op)
    if t in {"venta", "comprar", "compra", "para comprar", "comprarla", "adquirir"}:
        return "venta"
    if t in {"alquiler", "alquilar", "arrendar", "arrendamiento", "renta"}:
        return "alquiler"
    return ""


def mensaje_parece_contacto(mensaje: str) -> bool:
    if not mensaje:
        return False
    tiene_correo = bool(re.search(r"[\w\.-]+@[\w\.-]+\.\w+", mensaje, re.IGNORECASE))
    tiene_tel = bool(re.search(r"\+?\d[\d\s\-\(\)]{7,}\d", mensaje))
    return tiene_correo or tiene_tel


def es_nombre_persona_valido(nombre: str) -> bool:
    if not nombre:
        return False
    n = normalizar_texto(nombre)

    if n in FRASES_NO_NOMBRE:
        return False
    if any(f in n for f in FRASES_NO_NOMBRE):
        return False
    if re.search(r"@|\d", nombre):
        return False

    palabras = [p for p in re.findall(r"[A-Za-zÀ-ÖØ-ÿ'´-]+", nombre)]
    palabras = [p for p in palabras if normalizar_texto(p) not in STOPWORDS_NOMBRE_EXTRA and len(p) >= 2]

    # Nombre completo obligatorio
    if len(palabras) < 2 or len(palabras) > 4:
        return False

    return True


def primer_nombre_seguro(lead: dict) -> str:
    nombre = (lead or {}).get("nombre", "") or ""
    if es_nombre_persona_valido(nombre):
        return capitalizar_nombre(nombre).split()[0]
    return ""


def parsear_presupuesto_texto(texto: str):
    """
    Evita confundir teléfonos con presupuesto.
    """
    if not texto:
        return None

    t = normalizar_texto(texto)

    # teléfono largo sin contexto de dinero => ignorar
    if re.search(r"\+?\d[\d\s\-\(\)]{9,}\d", texto):
        if not any(k in t for k in ["presupuesto", "maximo", "máximo", "hasta", "usd", "dolar", "dolares", "$", "mil", "millon", "millones", "k", "mm"]):
            return None

    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(mil|k)\b", t)
    if m:
        val = float(m.group(1).replace(",", ".")) * 1000
        return val if 1000 <= val <= 20_000_000 else None

    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(millon|millones|mm)\b", t)
    if m:
        val = float(m.group(1).replace(",", ".")) * 1_000_000
        return val if 1000 <= val <= 20_000_000 else None

    candidatos = re.findall(r"(?:usd|us|dolares|dolar|\$)?\s*([0-9][0-9\.,]{3,})", t)
    for c in candidatos:
        limpio = c.strip()
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
            if 1000 <= val <= 20_000_000:
                return val
        except Exception:
            continue

    return None


def parsear_precio(valor) -> float:
    if valor in [None, "", "N/D"]:
        return 0.0
    t = str(valor).lower().strip()
    t = re.sub(r"[^\d.,]", "", t)
    if not t:
        return 0.0
    if "." in t and "," in t:
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "." in t:
        partes = t.split(".")
        if len(partes[-1]) == 3:
            t = t.replace(".", "")
    elif "," in t:
        partes = t.split(",")
        if len(partes[-1]) == 3:
            t = t.replace(",", "")
        else:
            t = t.replace(",", ".")
    try:
        return float(t)
    except Exception:
        return 0.0


def parsear_precio_wasi(valor_numerico=None, valor_label=None) -> float:
    if valor_numerico not in [None, "", "N/D"]:
        try:
            return float(valor_numerico)
        except Exception:
            pass
    return parsear_precio(valor_label)


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

    direcciones = tokens_busqueda.intersection(TOKENS_DIRECCION_ZONA)
    if direcciones and not direcciones.issubset(tokens_propiedad):
        return False

    if len(tokens_busqueda) >= 2:
        return tokens_busqueda.issubset(tokens_propiedad)

    unico = next(iter(tokens_busqueda))
    return (unico in tokens_propiedad) or (buscada_norm in texto_propiedad)


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
            "estado": ESTADO_DIAGNOSTICO_IA,
            "rol": None,  # cliente / colega_inmobiliario
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
            "lead": {"nombre": "", "correo": "", "whatsapp": ""},
            "ultima_respuesta": "",
            "mensaje_previo": "",
            "ultimo_mensaje_ts": 0.0,
            "ultimo_campo_solicitado": None
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
    return {"replies": [{"message": respuesta.replace("**", "*")}]}


# ============================================================
# INVENTARIO WASI
# ============================================================

def calcular_proxima_actualizacion(ahora: datetime = None) -> datetime:
    ahora = ahora or datetime.now()
    ventanas = sorted(HORARIOS_ACTUALIZACION_INVENTARIO)
    for hora in ventanas:
        candidato = ahora.replace(hour=hora, minute=0, second=0, microsecond=0)
        if candidato > ahora:
            return candidato
    manana = ahora + timedelta(days=1)
    return manana.replace(hour=ventanas[0], minute=0, second=0, microsecond=0)


def necesita_actualizar_inventario(force: bool = False) -> bool:
    if force:
        return True
    if not cache["inventario"]:
        return True
    proxima = cache.get("proxima_actualizacion", datetime.min)
    if proxima == datetime.min:
        cache["proxima_actualizacion"] = calcular_proxima_actualizacion()
        return False
    return datetime.now() >= proxima


def obtener_inventario_desde_wasi():
    propiedades = []
    take = 100
    skip = 0

    while True:
        params = {
            "wasi_token": os.getenv("WASI_TOKEN"),
            "id_company": os.getenv("WASI_COMPANY_ID"),
            "take": take,
            "skip": skip,
            "status": 1
        }

        try:
            response = requests.get("https://api.wasi.co/v1/property/search", params=params, timeout=25)
            response.raise_for_status()
            data = response.json()

            contador = 0
            for key, value in data.items():
                if isinstance(value, dict) and key.isdigit():
                    contador += 1
                    id_prop = value.get("id_property")
                    user_data = value.get("user_data", {})
                    captador_nombre = f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip() or "Asesor Mettryc"

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

            if contador < take:
                break
            skip += take
            time.sleep(1.0)

        except Exception as e:
            logger.error(f"Error obteniendo inventario Wasi: {e}")
            break

    return propiedades


def actualizar_cache_inventario(force: bool = False):
    if not necesita_actualizar_inventario(force):
        return False
    props = obtener_inventario_desde_wasi()
    if props:
        ahora = datetime.now()
        cache["inventario"] = props
        cache["ultima_actualizacion"] = ahora
        cache["proxima_actualizacion"] = calcular_proxima_actualizacion(ahora + timedelta(seconds=1))
        logger.info(f"Inventario actualizado: {len(props)} propiedades.")
        return True
    return False


async def garantizar_inventario_actualizado(force: bool = False):
    await asyncio.to_thread(actualizar_cache_inventario, force)


# ============================================================
# GOOGLE SHEETS (TURNOS + CAPTADORES)
# ============================================================

def sincronizar_google_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL")
    if not script_url:
        return

    necesita = (
        datetime.now() - sheets_cache["ultima_actualizacion"] > INTERVALO_ACTUALIZACION_SHEETS
        or not sheets_cache["agentes"]
        or not sheets_cache["captadores"]
    )
    if not necesita:
        return

    try:
        r = requests.get(script_url, timeout=12)
        r.raise_for_status()
        payload = r.json()

        if not isinstance(payload, dict):
            return

        sheets_cache["agentes"] = payload.get("agentes", [])
        captadores_payload = payload.get("captadores", {})
        captadores = {}

        if isinstance(captadores_payload, dict):
            pairs = captadores_payload.items()
        elif isinstance(captadores_payload, list):
            pairs = []
            for row in captadores_payload:
                if isinstance(row, dict):
                    k = row.get("nombre") or row.get("name")
                    v = row.get("telefono") or row.get("phone")
                    if k and v:
                        pairs.append((k, v))
        else:
            pairs = []

        for nombre, telefono in pairs:
            captadores[str(nombre).strip()] = str(telefono).strip()

        sheets_cache["captadores"] = captadores
        sheets_cache["ultima_actualizacion"] = datetime.now()
        logger.info(f"Sheets OK: {len(sheets_cache['agentes'])} agentes, {len(captadores)} captadores.")
    except Exception as e:
        logger.error(f"Error sincronizando Google Sheet: {e}")


def asignar_agente_round_robin():
    sincronizar_google_sheet()
    agentes = sheets_cache["agentes"]
    if not agentes:
        return None
    sheets_cache["ultimo_indice"] = (sheets_cache["ultimo_indice"] + 1) % len(agentes)
    agente = agentes[sheets_cache["ultimo_indice"]]
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
    best_score = 0
    best_phone = None
    for nombre_sheet, telefono in sheets_cache["captadores"].items():
        score = len(tokens_wasi.intersection(tokens_relevantes(nombre_sheet)))
        if score > best_score:
            best_score = score
            best_phone = telefono
    if best_score >= 2:
        return best_phone

    return "N/D"


# ============================================================
# OPENROUTER
# ============================================================

def consultar_ia(mensajes: list, max_tokens: int = MAX_TOKENS_IA, fallback: str = "") -> str:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY no configurada.")
        return fallback

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://www.mettryc.com",
        "X-Title": "Mettryc Realty Chatbot"
    }

    for modelo in [MODELO_PRINCIPAL, MODELO_RESPALDO]:
        try:
            resp = requests.post(
                url,
                headers=headers,
                json={
                    "model": modelo,
                    "messages": mensajes,
                    "max_tokens": max_tokens,
                    "temperature": 0.1
                },
                timeout=9
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                c = choices[0].get("message", {}).get("content", "")
                if c and c.strip():
                    return c.strip()
        except Exception as e:
            logger.warning(f"IA error con {modelo}: {e}")

    return fallback


def extraer_json_de_texto(texto: str) -> dict:
    if not texto:
        return {}
    try:
        return json.loads(texto)
    except Exception:
        pass

    ini = texto.find("{")
    fin = texto.rfind("}")
    if ini != -1 and fin != -1 and fin > ini:
        frag = texto[ini:fin + 1]
        try:
            return json.loads(frag)
        except Exception:
            return {}
    return {}


# ============================================================
# EXTRACCIÓN IA + FALLBACK
# ============================================================

def fallback_regex_filtros(mensaje: str, filtros: dict) -> dict:
    txt = normalizar_texto(mensaje)
    out = filtros.copy()

    # tipo
    if any(k in txt for k in ["townhouse", "towhouse", "tohouse", "town house", "twhouse"]):
        out["tipo_propiedad"] = "townhouse"
    elif any(k in txt for k in ["apartoquinta", "aparto quinta", "apartoquita", "aptoquinta"]):
        out["tipo_propiedad"] = "apartoquinta"
    elif "penthouse" in txt:
        out["tipo_propiedad"] = "penthouse"
    elif "apartamento" in txt or "apto" in txt:
        out["tipo_propiedad"] = "apartamento"
    elif "casa" in txt:
        out["tipo_propiedad"] = "casa"

    # operación
    if any(k in txt for k in ["comprar", "compra", "para comprar", "venta", "adquirir"]):
        out["tipo_operacion"] = "venta"
    elif any(k in txt for k in ["alquiler", "alquilar", "renta", "arrendar"]):
        out["tipo_operacion"] = "alquiler"

    # zona (si no existe)
    if not out.get("zona"):
        m = re.search(
            r"(?:en|zona|urbanizacion|urbanización|sector|ciudad)\s+(.+?)(?:\s+(?:con|hasta|de\s+\d|presupuesto|para)\b|$)",
            mensaje,
            re.IGNORECASE
        )
        if m:
            cand = re.sub(r"\s+", " ", m.group(1)).strip()
            if 1 <= len(cand.split()) <= 8:
                out["zona"] = cand

    # presupuesto
    p = parsear_presupuesto_texto(mensaje)
    if p:
        out["presupuesto"] = p

    # habitaciones / baños
    mh = re.search(r"(\d+)\s*(hab|habitacion|habitaciones|cuarto|cuartos)", txt)
    if mh:
        out["habitaciones_min"] = int(mh.group(1))

    mb = re.search(r"(\d+)\s*(bano|banos|baño|baños)", txt)
    if mb:
        out["banos_min"] = int(mb.group(1))

    # características
    claves = ["patio", "terraza", "jardin", "piscina", "vigilancia", "estacionamiento", "pozo", "tanque"]
    car = out.get("caracteristicas", []) or []
    for c in claves:
        if c in txt and c not in car:
            car.append(c)
    out["caracteristicas"] = car

    return out


def fallback_regex_lead(mensaje: str, lead: dict, sender: str) -> dict:
    out = lead.copy()
    texto = mensaje or ""

    m_mail = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", texto, re.IGNORECASE)
    if m_mail:
        out["correo"] = m_mail.group(0).strip().lower()

    m_tel = re.search(r"(\+?\d[\d\s\-\(\)]{7,}\d)", texto)
    if m_tel:
        out["whatsapp"] = limpiar_telefono(m_tel.group(1))
    elif not out.get("whatsapp"):
        sender_tel = limpiar_telefono(sender)
        if len(sender_tel) >= 7:
            out["whatsapp"] = sender_tel

    # nombre explícito
    m_nom = re.search(
        r"(?:soy|me llamo|mi nombre es)\s+([A-Za-zÀ-ÖØ-ÿ'´-]+(?:\s+[A-Za-zÀ-ÖØ-ÿ'´-]+){0,4})",
        texto,
        re.IGNORECASE
    )
    if m_nom:
        cand = capitalizar_nombre(m_nom.group(1).strip())
        if es_nombre_persona_valido(cand):
            out["nombre"] = cand
    else:
        # nombre directo: mensaje corto, no contacto, 2-4 palabras válidas
        if not m_mail and not m_tel and len(texto.strip()) <= 60:
            palabras = re.findall(r"[A-Za-zÀ-ÖØ-ÿ'´-]+", texto)
            limpias = [p for p in palabras if normalizar_texto(p) not in PALABRAS_INVALIDAS_NOMBRE and len(p) > 1]
            if 2 <= len(limpias) <= 4:
                cand = capitalizar_nombre(" ".join(limpias[:4]))
                if es_nombre_persona_valido(cand):
                    out["nombre"] = cand

    return out


def analizar_mensaje_ia(mensaje_usuario: str, estado: dict, historial: list) -> dict:
    filtros = estado.get("filtros", {})
    lead = estado.get("lead", {})
    rol_actual = estado.get("rol") or ""

    historial_breve = historial[-10:] if isinstance(historial, list) else []
    contexto = []
    for h in historial_breve:
        contexto.append({"role": h.get("role", "user"), "content": h.get("content", "")[:220]})

    system = (
        "Eres un analista de mensajes para chatbot inmobiliario. "
        "Responde SOLO JSON válido, sin texto adicional. "
        "Objetivo: interpretar intención, corregir typos y extraer datos estructurados."
    )

    user_prompt = f"""
Reglas:
1) typos: towhouse/towhouse=>townhouse, apartoquita=>apartoquinta.
2) comprar/compra/adquirir => venta. alquilar/renta => alquiler.
3) zonas direccionales estrictas: Trigal Norte != Trigal Sur/Centro.
4) rol colega si detectas broker/realtor/colega/asesor/comision/mls.
5) En lead, SOLO colocar nombre si realmente parece nombre de persona (no frases como "la que enviaste").

Estado actual:
{json.dumps({"filtros": filtros, "lead": lead, "rol": rol_actual, "estado": estado.get("estado")}, ensure_ascii=False)}

Mensaje usuario:
"{mensaje_usuario}"

Devuelve exacto este schema:
{{
  "intencion": "saludo|buscar|mas_opciones|ajustar_busqueda|interes_propiedad|enviar_datos|otro",
  "rol": "cliente|colega_inmobiliario|desconocido",
  "filtros": {{
    "tipo_propiedad": "",
    "tipo_operacion": "",
    "zona": "",
    "presupuesto": null,
    "habitaciones_min": null,
    "banos_min": null,
    "caracteristicas": []
  }},
  "lead": {{
    "nombre": "",
    "correo": "",
    "whatsapp": ""
  }},
  "pregunta_siguiente": ""
}}
"""
    raw = consultar_ia(
        [{"role": "system", "content": system}] + contexto + [{"role": "user", "content": user_prompt}],
        max_tokens=MAX_TOKENS_IA,
        fallback="{}"
    )
    data = extraer_json_de_texto(raw)
    if not isinstance(data, dict):
        return {}
    return data


def fusionar_filtros(base: dict, nuevos: dict, mensaje_usuario: str) -> dict:
    out = dict(base or {})
    nuevos = nuevos or {}

    if nuevos.get("tipo_propiedad"):
        out["tipo_propiedad"] = normalizar_tipo_propiedad(nuevos["tipo_propiedad"])

    if nuevos.get("tipo_operacion"):
        op = normalizar_operacion(nuevos["tipo_operacion"])
        if op:
            out["tipo_operacion"] = op

    if nuevos.get("zona"):
        out["zona"] = str(nuevos["zona"]).strip()

    for k in ["presupuesto", "habitaciones_min", "banos_min"]:
        v = nuevos.get(k, None)
        if v not in [None, "", []]:
            try:
                out[k] = float(v) if k == "presupuesto" else int(v)
            except Exception:
                pass

    if isinstance(nuevos.get("caracteristicas"), list) and nuevos.get("caracteristicas"):
        actuales = out.get("caracteristicas", []) or []
        out["caracteristicas"] = list(
            dict.fromkeys(actuales + [str(c).strip() for c in nuevos["caracteristicas"] if str(c).strip()])
        )

    # fallback local
    out = fallback_regex_filtros(mensaje_usuario, out)

    # normalizaciones finales
    out["tipo_propiedad"] = normalizar_tipo_propiedad(out.get("tipo_propiedad", ""))
    out["tipo_operacion"] = normalizar_operacion(out.get("tipo_operacion", ""))

    if out.get("presupuesto") is None:
        p = parsear_presupuesto_texto(mensaje_usuario)
        if p:
            out["presupuesto"] = p

    if out.get("caracteristicas") is None:
        out["caracteristicas"] = []

    # aprendizaje
    if out.get("tipo_propiedad"):
        aprendizaje_global["tipo_propiedad"][normalizar_texto(out["tipo_propiedad"])] += 1
    if out.get("tipo_operacion"):
        aprendizaje_global["operaciones"][normalizar_texto(out["tipo_operacion"])] += 1
    if out.get("zona"):
        aprendizaje_global["zonas"][normalizar_texto(out["zona"])] += 1

    return out


def fusionar_lead(base: dict, nuevos: dict, mensaje: str, sender: str) -> dict:
    out = dict(base or {})
    nuevos = nuevos or {}

    if nuevos.get("nombre"):
        cand = capitalizar_nombre(str(nuevos["nombre"]).strip())
        if es_nombre_persona_valido(cand):
            out["nombre"] = cand

    if nuevos.get("correo"):
        correo = str(nuevos["correo"]).strip().lower()
        if re.search(r"^[\w\.-]+@[\w\.-]+\.\w+$", correo):
            out["correo"] = correo

    if nuevos.get("whatsapp"):
        w = limpiar_telefono(str(nuevos["whatsapp"]))
        if len(w) >= 10:
            out["whatsapp"] = w

    # fallback robusto
    out = fallback_regex_lead(mensaje, out, sender)

    # validación final de nombre
    if out.get("nombre") and not es_nombre_persona_valido(out["nombre"]):
        out["nombre"] = ""

    return out


def campo_faltante_diagnostico(filtros: dict) -> Optional[str]:
    if not filtros.get("tipo_propiedad"):
        return "tipo_propiedad"
    if not filtros.get("zona"):
        return "zona"
    if not filtros.get("tipo_operacion"):
        return "tipo_operacion"
    if filtros.get("presupuesto") is None:
        return "presupuesto"
    return None


def pregunta_clara_por_campo(campo: str, sugerida_ia: str = "") -> str:
    if sugerida_ia and len(sugerida_ia.strip()) >= 10:
        return sugerida_ia.strip()

    preguntas = {
        "tipo_propiedad": "Para continuar, indícame el tipo de propiedad (casa, apartamento, townhouse, apartoquinta, local, oficina, etc.).",
        "zona": "Para continuar, ¿en qué zona exacta deseas buscar? (Ejemplo: Trigal Norte, Valencia).",
        "tipo_operacion": "Para seguir, ¿la deseas para *venta* o *alquiler*?",
        "presupuesto": "Para afinar resultados, compárteme tu presupuesto máximo aproximado."
    }
    return preguntas.get(campo, "Para continuar, compárteme ese dato de tu búsqueda por favor.")


# ============================================================
# BÚSQUEDA Y RANKING
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
            "casa" in tipo_wasi or "casa" in titulo
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
        f"{propiedad.get('titulo', '')} {propiedad.get('zona', '')} {propiedad.get('ciudad', '')} {propiedad.get('tipo_propiedad_wasi', '')}"
    )
    score = 0
    for c in caracteristicas or []:
        cn = normalizar_texto(c)
        if cn and cn in texto:
            score += 2
    return score


def elegir_top_n_propiedades(inventario: list, filtros: dict, n=3, excluir_ids=None):
    excluir_ids = set(str(x) for x in (excluir_ids or []))
    tipo_prop = filtros.get("tipo_propiedad", "")
    tipo_op = filtros.get("tipo_operacion", "")
    zona = filtros.get("zona", "")
    presupuesto = filtros.get("presupuesto", None)
    hab_min = filtros.get("habitaciones_min", None)
    ban_min = filtros.get("banos_min", None)
    car = filtros.get("caracteristicas", []) or []

    props = [p for p in inventario if str(p.get("id")) not in excluir_ids]

    if tipo_op == "venta":
        props = [p for p in props if p.get("precio_venta_float", 0) > 0]
    elif tipo_op == "alquiler":
        props = [p for p in props if p.get("precio_renta_float", 0) > 0]

    if tipo_prop:
        props = [p for p in props if coincide_tipo_propiedad(p, tipo_prop)]

    if zona:
        props = [p for p in props if zona_coincide(zona, p.get("zona", ""), p.get("ciudad", ""))]

    if presupuesto is not None:
        if tipo_op == "venta":
            props = [p for p in props if 0 < p.get("precio_venta_float", 0) <= presupuesto]
        elif tipo_op == "alquiler":
            props = [p for p in props if 0 < p.get("precio_renta_float", 0) <= presupuesto]

    if hab_min is not None:
        props = [p for p in props if convertir_entero_seguro(p.get("habitaciones")) >= int(hab_min)]

    if ban_min is not None:
        props = [p for p in props if convertir_entero_seguro(p.get("banos")) >= int(ban_min)]

    def score(p):
        puntos = 0
        if zona and zona_coincide(zona, p.get("zona", ""), p.get("ciudad", "")):
            puntos += 8
        if tipo_prop and coincide_tipo_propiedad(p, tipo_prop):
            puntos += 6

        precio = p.get("precio_venta_float", 0) if tipo_op == "venta" else p.get("precio_renta_float", 0)
        if precio > 0:
            puntos += 2

        if hab_min and convertir_entero_seguro(p.get("habitaciones")) >= int(hab_min):
            puntos += 2
        if ban_min and convertir_entero_seguro(p.get("banos")) >= int(ban_min):
            puntos += 1

        puntos += propiedad_contiene_caracteristicas(p, car)

        buscada = normalizar_texto(zona)
        zp = normalizar_texto(p.get("zona", ""))
        cp = normalizar_texto(p.get("ciudad", ""))
        if buscada and (buscada == zp or buscada in f"{zp} {cp}"):
            puntos += 3

        return puntos

    ordenadas = sorted(props, key=score, reverse=True)[:n]
    for p in ordenadas:
        p["operacion_buscada"] = tipo_op
    return ordenadas


def formatear_ficha_propiedad(propiedad: dict, es_colega=False) -> str:
    op = propiedad.get("operacion_buscada", "venta")
    if op == "alquiler":
        precio, etiqueta = propiedad.get("renta", "N/D"), "Renta"
    else:
        precio, etiqueta = propiedad.get("venta", "N/D"), "Venta"

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


# ============================================================
# INTENCIONES / LEADS
# ============================================================

def usuario_pide_mas_opciones(m: str) -> bool:
    t = normalizar_texto(m)
    patrones = [
        "ver mas", "ver más", "muestrame mas", "muestrame más", "otras opciones",
        "otra opcion", "otra opción", "tienes mas", "tienes más", "mas opciones", "más opciones",
        "dame mas", "dame más"
    ]
    return any(p in t for p in patrones)


def usuario_solicita_ajuste(m: str) -> bool:
    t = normalizar_texto(m)
    patrones = [
        "no me gustan", "no me gustaron", "no me gusta", "ninguna me gusta",
        "no me sirven", "no me convence", "no me convencen", "otra zona", "sube el presupuesto"
    ]
    return any(p in t for p in patrones)


def usuario_muestra_interes(m: str) -> bool:
    t = normalizar_texto(m)
    patrones = [
        "me interesa", "quiero verla", "quiero ver la", "quiero ver el", "quiero visitar",
        "agendar", "cita", "quiero avanzar", "contactar asesor", "hablar con asesor",
        "opcion 1", "opcion 2", "opcion 3", "la primera", "la segunda", "la tercera", "la ultima", "la última"
    ]
    return any(p in t for p in patrones)


def usuario_envia_datos_contacto(m: str) -> bool:
    return mensaje_parece_contacto(m)


def proximo_dato_lead(lead: dict) -> Optional[str]:
    nombre = (lead or {}).get("nombre", "")
    if not es_nombre_persona_valido(nombre):
        return "nombre"
    if not (lead or {}).get("correo"):
        return "correo"
    if not (lead or {}).get("whatsapp"):
        return "whatsapp"
    return None


def construir_pedido_dato_lead(campo: str, nombre: str = "") -> str:
    pnombre = primer_nombre_seguro({"nombre": nombre})
    mensajes = {
        "nombre": "¡Excelente! Para asignarte un asesor, compárteme tu *nombre completo* (nombre y apellido), por favor.",
        "correo": (f"Perfecto, {pnombre} 🙌 Ahora indícame tu *correo electrónico*."
                   if pnombre else "Perfecto 🙌 Ahora indícame tu *correo electrónico*."),
        "whatsapp": "Genial. Por favor compárteme tu *número de WhatsApp* con código de país."
    }
    return mensajes.get(campo, "Compárteme ese dato para continuar por favor.")


def construir_recordatorio_dato(campo: str) -> str:
    mensajes = {
        "nombre": "Aún me falta tu *nombre completo* (nombre y apellido) para continuar. ¿Me lo compartes ahora?",
        "correo": "Aún me falta tu *correo electrónico* para continuar. ¿Me lo compartes ahora?",
        "whatsapp": "Aún me falta tu *número de WhatsApp* para continuar. ¿Me lo compartes ahora?"
    }
    return mensajes.get(campo, "Aún me falta ese dato para continuar. ¿Me lo compartes ahora?")


def construir_resumen_necesidad(filtros: dict) -> str:
    partes = []
    if filtros.get("tipo_propiedad"):
        partes.append(f"- Tipo de propiedad: {filtros['tipo_propiedad']}")
    if filtros.get("tipo_operacion"):
        partes.append(f"- Operación: {filtros['tipo_operacion']}")
    if filtros.get("zona"):
        partes.append(f"- Zona: {filtros['zona']}")
    if filtros.get("presupuesto"):
        partes.append(f"- Presupuesto máximo: {filtros['presupuesto']}")
    if filtros.get("habitaciones_min"):
        partes.append(f"- Habitaciones mínimas: {filtros['habitaciones_min']}")
    if filtros.get("banos_min"):
        partes.append(f"- Baños mínimos: {filtros['banos_min']}")
    if filtros.get("caracteristicas"):
        partes.append(f"- Características: {', '.join(filtros['caracteristicas'])}")
    return "\n".join(partes) if partes else "- No especificado"


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

    nombre_cliente = capitalizar_nombre(lead.get("nombre", ""))
    if not es_nombre_persona_valido(nombre_cliente):
        nombre_cliente = "Cliente sin nombre"

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
            r = requests.post(url_tg, json={"chat_id": chat_id, "text": mensaje}, timeout=8)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"Error enviando Telegram a {chat_id}: {e}")

    if agente and agente.get("telegram_id"):
        enviar(str(agente["telegram_id"]).strip(), mensaje_agente)

    for admin_id in admin_ids:
        if admin_id.lstrip("-").isdigit():
            enviar(admin_id, mensaje_admin)


# ============================================================
# CASOS ESPECIALES
# ============================================================

def es_consulta_reclutamiento(mensaje: str) -> bool:
    t = normalizar_texto(mensaje)
    patrones = [
        "quiero unirme", "trabajar con ustedes", "ser agente", "ser asesor",
        "mettryc team", "reclutamiento", "curso", "comision", "comisiones"
    ]
    return any(p in t for p in patrones)


def respuesta_reclutamiento() -> str:
    return (
        "¡Qué emoción que quieras unirte al Mettryc Team! 🚀 "
        "Aquí tienes la información: https://mettryc.com/blog/unete-al-mettryc-team-y-gana-desde-el-80-al-100-de-comision/18270?page=1\n\n"
        "El curso inicial cuesta $60 y dura 5 días, de 9am a 12pm."
    )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup():
    await garantizar_inventario_actualizado(force=True)
    await asyncio.to_thread(sincronizar_google_sheet)


# ============================================================
# WEBHOOK
# ============================================================

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()

        api_key_header = request.headers.get("x-api-key")
        valid_api_keys = [k.strip() for k in os.getenv("API_KEYS_AGENTES", "").split(",") if k.strip()]
        if not api_key_header or api_key_header not in valid_api_keys:
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

            # anti duplicado
            if (
                mensaje_cliente == estado.get("mensaje_previo", "")
                and (ahora_ts - float(estado.get("ultimo_mensaje_ts", 0.0))) <= VENTANA_MENSAJE_DUPLICADO_SEGUNDOS
            ):
                logger.info(f"Duplicado ignorado sender={sender}")
                return {"replies": []}

            estado["mensaje_previo"] = mensaje_cliente
            estado["ultimo_mensaje_ts"] = ahora_ts

            # casos directos
            if "mercadolibre.com.ve/mlv" in mensaje_cliente.lower():
                return enviar_respuesta(
                    sender, mensaje_cliente,
                    "¡Hola! 👋 Esta propiedad se encuentra disponible en el precio publicado. ¿Quieres agendar una visita?",
                    estado
                )

            if es_consulta_reclutamiento(mensaje_cliente):
                return enviar_respuesta(sender, mensaje_cliente, respuesta_reclutamiento(), estado)

            # inventario
            if not cache["inventario"]:
                await garantizar_inventario_actualizado(force=True)
            elif necesita_actualizar_inventario():
                asyncio.create_task(garantizar_inventario_actualizado(force=False))

            sincronizar_google_sheet()
            inventario = cache["inventario"]
            if not inventario:
                return enviar_respuesta(
                    sender, mensaje_cliente,
                    "Estamos actualizando inventario. Escríbeme en unos minutos y te paso opciones. 😊",
                    estado
                )

            # análisis IA
            analisis = await asyncio.to_thread(
                analizar_mensaje_ia,
                mensaje_cliente,
                estado,
                memoria_conversaciones.get(sender, [])
            )
            if not isinstance(analisis, dict):
                analisis = {}

            intencion = analisis.get("intencion", "otro")

            # Rol
            rol_ia = analisis.get("rol", "desconocido")
            if rol_ia in {"cliente", "colega_inmobiliario"}:
                estado["rol"] = rol_ia
            if not estado["rol"]:
                texto_norm = normalizar_texto(mensaje_cliente)
                if any(k in texto_norm for k in ["colega", "broker", "realtor", "asesor", "corredor", "comision", "mls"]):
                    estado["rol"] = "colega_inmobiliario"
                else:
                    estado["rol"] = "cliente"

            # Lead SIEMPRE
            estado["lead"] = fusionar_lead(estado["lead"], analisis.get("lead", {}), mensaje_cliente, sender)

            # Filtros SOLO si no estamos en captura lead y mensaje no parece contacto
            if estado["estado"] != ESTADO_CAPTURANDO_LEAD and not mensaje_parece_contacto(mensaje_cliente):
                estado["filtros"] = fusionar_filtros(estado["filtros"], analisis.get("filtros", {}), mensaje_cliente)

            # ------------------------------------------------------------
            # SI YA ESTÁ MOSTRANDO PROPIEDADES
            # ------------------------------------------------------------
            if estado["estado"] == ESTADO_MOSTRANDO_PROPIEDADES:
                if intencion == "mas_opciones" or usuario_pide_mas_opciones(mensaje_cliente) or usuario_solicita_ajuste(mensaje_cliente):
                    props = elegir_top_n_propiedades(
                        inventario, estado["filtros"], n=3, excluir_ids=estado["propiedades_enviadas"]
                    )
                    if not props:
                        return enviar_respuesta(
                            sender, mensaje_cliente,
                            "Por ahora no encontré más opciones exactas con esos criterios. ¿Quieres ampliar zona o ajustar presupuesto?",
                            estado
                        )

                    estado["propiedades_enviadas"].extend([p.get("id") for p in props if p.get("id")])
                    es_colega = estado["rol"] == "colega_inmobiliario"
                    fichas = [formatear_ficha_propiedad(p, es_colega) for p in props]
                    texto = "Perfecto, te comparto más opciones:\n\n" + "\n\n".join(fichas)
                    if not es_colega:
                        texto += "\n\n¿Alguna te interesa para coordinar visita o asignarte un asesor? 😊"
                    return enviar_respuesta(sender, mensaje_cliente, texto, estado)

            # ------------------------------------------------------------
            # ACTIVAR CAPTURA LEAD (CLIENTE)
            # ------------------------------------------------------------
            if (
                estado["rol"] == "cliente"
                and sender not in clientes_procesados
                and (
                    estado["estado"] == ESTADO_CAPTURANDO_LEAD
                    or intencion in {"interes_propiedad", "enviar_datos"}
                    or usuario_muestra_interes(mensaje_cliente)
                    or usuario_envia_datos_contacto(mensaje_cliente)
                )
            ):
                estado["estado"] = ESTADO_CAPTURANDO_LEAD

                faltante = proximo_dato_lead(estado["lead"])
                if faltante:
                    if estado.get("ultimo_campo_solicitado") == faltante:
                        return enviar_respuesta(sender, mensaje_cliente, construir_recordatorio_dato(faltante), estado)
                    estado["ultimo_campo_solicitado"] = faltante
                    return enviar_respuesta(
                        sender, mensaje_cliente,
                        construir_pedido_dato_lead(faltante, estado["lead"].get("nombre", "")),
                        estado
                    )

                # lead completo => asignación turno
                agente = asignar_agente_round_robin()
                resumen = construir_resumen_necesidad(estado["filtros"])

                if agente:
                    enviar_notificaciones_telegram(agente, estado["lead"], resumen)

                clientes_procesados.add(sender)
                estado["estado"] = ESTADO_LEAD_COMPLETO

                nombre_mostrar = capitalizar_nombre(estado["lead"].get("nombre", ""))
                if not es_nombre_persona_valido(nombre_mostrar):
                    nombre_mostrar = "Cliente"

                if agente:
                    msg = (
                        f"¡Perfecto, {nombre_mostrar}! ✨ Ya registré tus datos y asigné tu solicitud a "
                        f"*{agente.get('nombre', 'uno de nuestros asesores')}*. "
                        "Te contactará muy pronto. 🤝"
                    )
                else:
                    msg = "¡Perfecto! Ya tengo tus datos. En breve te contactará un asesor disponible. 😊"

                return enviar_respuesta(sender, mensaje_cliente, msg, estado)

            # ------------------------------------------------------------
            # FASE DIAGNÓSTICO IA
            # ------------------------------------------------------------
            faltante = campo_faltante_diagnostico(estado["filtros"])
            if faltante:
                estado["estado"] = ESTADO_DIAGNOSTICO_IA
                pregunta = pregunta_clara_por_campo(faltante, analisis.get("pregunta_siguiente", ""))

                if estado.get("ultima_respuesta", "").strip() == pregunta.strip():
                    pregunta = pregunta_clara_por_campo(faltante, "")

                estado["ultimo_campo_solicitado"] = faltante
                return enviar_respuesta(sender, mensaje_cliente, pregunta, estado)

            # ------------------------------------------------------------
            # MOSTRAR PROPIEDADES
            # ------------------------------------------------------------
            props = elegir_top_n_propiedades(
                inventario, estado["filtros"], n=3, excluir_ids=estado["propiedades_enviadas"]
            )
            estado["estado"] = ESTADO_MOSTRANDO_PROPIEDADES

            if not props:
                return enviar_respuesta(
                    sender, mensaje_cliente,
                    "Revisé inventario activo y no encontré coincidencia exacta. ¿Quieres ampliar zona o ajustar presupuesto?",
                    estado
                )

            estado["propiedades_enviadas"].extend([p.get("id") for p in props if p.get("id")])

            es_colega = estado["rol"] == "colega_inmobiliario"
            fichas = [formatear_ficha_propiedad(p, es_colega) for p in props]
            resumen = construir_resumen_necesidad(estado["filtros"])

            if es_colega:
                respuesta = (
                    f"Perfecto, colega. Con base en tu búsqueda:\n{resumen}\n\n"
                    "Estas son 3 opciones y te incluyo datos de captador:\n\n"
                    + "\n\n".join(fichas)
                )
            else:
                respuesta = (
                    f"Perfecto. Con base en tu búsqueda:\n{resumen}\n\n"
                    "Estas son 3 opciones que mejor encajan:\n\n"
                    + "\n\n".join(fichas)
                    + "\n\n¿Alguna te interesa para coordinar visita o asignarte un asesor? 😊"
                )

            return enviar_respuesta(sender, mensaje_cliente, respuesta, estado)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error general webhook: {e}", exc_info=True)
        return {
            "replies": [
                {"message": "Lo siento, tuve un inconveniente procesando tu solicitud. ¿Me la repites por favor?"}
            ]
        }
