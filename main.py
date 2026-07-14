import asyncio
import json
import logging
import os
import re
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional, Set

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError


# ============================================================
# LOGS
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("mettryc-chatbot")

# Evita que tokens incluidos en URLs aparezcan en Render.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ============================================================
# CONFIGURACIÓN
# ============================================================

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
WASI_TOKEN = os.getenv("WASI_TOKEN", "")
WASI_COMPANY_ID = os.getenv("WASI_COMPANY_ID", "")
GOOGLE_SHEET_TURNOS_URL = os.getenv("GOOGLE_SHEET_TURNOS_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

MODELO_AGENTE_PRINCIPAL = os.getenv(
    "MODELO_AGENTE_PRINCIPAL",
    os.getenv(
        "MODELO_ANALISIS_PRINCIPAL",
        "google/gemini-2.5-flash",
    ),
)

MODELO_AGENTE_RESPALDO = os.getenv(
    "MODELO_AGENTE_RESPALDO",
    os.getenv(
        "MODELO_ANALISIS_RESPALDO",
        "openai/gpt-4o-mini",
    ),
)

OPENROUTER_TIMEOUT = float(
    os.getenv("OPENROUTER_TIMEOUT", "30")
)

WASI_TIMEOUT = float(
    os.getenv("WASI_TIMEOUT", "40")
)

SHEETS_TIMEOUT = float(
    os.getenv("SHEETS_TIMEOUT", "20")
)

TELEGRAM_TIMEOUT = float(
    os.getenv("TELEGRAM_TIMEOUT", "15")
)

MAX_PROPIEDADES_POR_LOTE = int(
    os.getenv("MAX_PROPIEDADES_POR_LOTE", "3")
)

MAX_EXCESO_PRESUPUESTO = float(
    os.getenv("MAX_EXCESO_PRESUPUESTO", "0.20")
)

MAX_HISTORIAL = int(
    os.getenv("MAX_HISTORIAL", "16")
)

DUPLICATE_TTL_SECONDS = int(
    os.getenv("DUPLICATE_TTL_SECONDS", "180")
)

SENDER_ES_WHATSAPP = (
    os.getenv("SENDER_ES_WHATSAPP", "true").lower()
    == "true"
)

API_KEYS_AGENTES = {
    clave.strip()
    for clave in os.getenv(
        "API_KEYS_AGENTES",
        "",
    ).split(",")
    if clave.strip()
}

TELEGRAM_ADMIN_IDS = [
    valor.strip()
    for valor in os.getenv(
        "TELEGRAM_ADMIN_IDS",
        os.getenv("TELEGRAM_ADMIN_ID", ""),
    ).split(",")
    if valor.strip()
]

INTERVALO_ACTUALIZACION_SHEETS = timedelta(
    minutes=int(
        os.getenv(
            "INTERVALO_ACTUALIZACION_SHEETS_MINUTOS",
            "60",
        )
    )
)

INTERVALO_ACTUALIZACION_WASI = timedelta(
    hours=int(
        os.getenv(
            "INTERVALO_ACTUALIZACION_WASI_HORAS",
            "12",
        )
    )
)


# ============================================================
# MODELOS ESTRUCTURADOS DEL AGENTE
# ============================================================

class ActualizacionesConversacion(BaseModel):
    tipo_operacion: Optional[
        Literal["venta", "alquiler"]
    ] = None

    tipo_propiedad: Optional[str] = None
    zona: Optional[str] = None
    presupuesto_max: Optional[float] = None
    habitaciones_min: Optional[int] = None
    banos_min: Optional[int] = None
    garajes_min: Optional[int] = None
    caracteristicas: List[str] = Field(
        default_factory=list
    )

    nombre: Optional[str] = None
    correo: Optional[str] = None
    whatsapp: Optional[str] = None
    usar_numero_actual: bool = False


class AccionAgente(BaseModel):
    tipo: Literal[
        "responder",
        "buscar_propiedades",
        "mostrar_mas_propiedades",
        "buscar_por_codigo",
        "seleccionar_propiedad",
        "pedir_codigo_inmueble",
        "reiniciar_busqueda",
        "pedir_aclaracion",
    ] = "responder"

    codigo: Optional[str] = None
    posicion: Optional[int] = None


class DecisionAgente(BaseModel):
    mensaje: str = ""

    rol: Optional[
        Literal[
            "cliente",
            "colega_inmobiliario",
        ]
    ] = None

    confianza_rol: float = 0.0
    intencion: str = "otro"

    actualizaciones: ActualizacionesConversacion = Field(
        default_factory=ActualizacionesConversacion
    )

    campos_sin_preferencia: List[str] = Field(
        default_factory=list
    )

    accion: AccionAgente = Field(
        default_factory=AccionAgente
    )


class TextoResultado(BaseModel):
    introduccion: str
    cierre: str


# ============================================================
# ESTADO EN MEMORIA
# ============================================================

sesiones: Dict[str, dict] = {}
locks_usuarios: Dict[str, asyncio.Lock] = {}
mensajes_duplicados: Dict[str, float] = {}

inventory_cache: Dict[str, Any] = {
    "inventario": [],
    "ultima_actualizacion": None,
}

sheets_cache: Dict[str, Any] = {
    "agentes": [],
    "captadores": {},
    "ultima_actualizacion": None,
}

inventory_refresh_lock = asyncio.Lock()
sheets_refresh_lock = asyncio.Lock()
round_robin_lock = asyncio.Lock()
round_robin_index = -1

http_client: Optional[httpx.AsyncClient] = None


# ============================================================
# UTILIDADES
# ============================================================

def normalizar_texto(valor: Any) -> str:
    if valor is None:
        return ""

    texto = str(valor).strip().lower()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(
        caracter
        for caracter in texto
        if unicodedata.category(caracter) != "Mn"
    )
    texto = re.sub(
        r"[^a-z0-9@.+\-\s]",
        " ",
        texto,
    )

    return re.sub(r"\s+", " ", texto).strip()


def normalizar_nombre(valor: Any) -> str:
    palabras = re.findall(
        r"[A-Za-zÀ-ÖØ-ÿ'’-]+",
        str(valor or ""),
    )

    return " ".join(
        palabra[:1].upper() + palabra[1:].lower()
        for palabra in palabras
    )


def convertir_float(valor: Any) -> float:
    try:
        if valor in [None, "", "N/D"]:
            return 0.0
        return float(valor)
    except (TypeError, ValueError):
        return 0.0


def convertir_entero(valor: Any) -> int:
    try:
        if valor in [None, "", "N/D"]:
            return 0
        return int(float(valor))
    except (TypeError, ValueError):
        return 0


def formato_moneda(valor: Any) -> str:
    numero = convertir_float(valor)

    if numero <= 0:
        return "N/D"

    return f"${numero:,.0f}".replace(",", ".")


def limpiar_telefono(valor: Any) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def normalizar_telefono(valor: Any) -> Optional[str]:
    telefono = limpiar_telefono(valor)

    if telefono.startswith("00"):
        telefono = telefono[2:]

    if telefono.startswith("0") and len(telefono) == 11:
        telefono = "58" + telefono[1:]

    if len(telefono) == 10 and telefono.startswith("4"):
        telefono = "58" + telefono

    if 10 <= len(telefono) <= 15:
        return telefono

    return None


def extraer_telefono(texto: str) -> Optional[str]:
    coincidencia = re.search(
        r"(\+?\d[\d\s\-()]{7,}\d)",
        texto or "",
    )

    if not coincidencia:
        return None

    return normalizar_telefono(
        coincidencia.group(1)
    )


def extraer_correo(texto: str) -> Optional[str]:
    coincidencia = re.search(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        texto or "",
        re.IGNORECASE,
    )

    if not coincidencia:
        return None

    return coincidencia.group(0).lower()


def correo_valido(valor: Any) -> bool:
    return bool(
        re.fullmatch(
            r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
            str(valor or ""),
            re.IGNORECASE,
        )
    )


def nombre_valido(valor: Any) -> bool:
    nombre = normalizar_nombre(valor)

    if not nombre:
        return False

    palabras = nombre.split()

    bloqueadas = {
        "Hola",
        "Buenas",
        "Gracias",
        "Primera",
        "Segunda",
        "Tercera",
        "Apartamento",
        "Casa",
    }

    if any(palabra in bloqueadas for palabra in palabras):
        return False

    return 2 <= len(palabras) <= 6


def parsear_precio_wasi(
    valor: Any,
    etiqueta: Any,
) -> float:
    numero = convertir_float(valor)

    if numero > 0:
        return numero

    texto = re.sub(
        r"[^\d.,]",
        "",
        str(etiqueta or ""),
    )

    if not texto:
        return 0.0

    if "." in texto and "," in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "")
            texto = texto.replace(",", ".")
        else:
            texto = texto.replace(",", "")

    elif "." in texto:
        if len(texto.split(".")[-1]) == 3:
            texto = texto.replace(".", "")

    elif "," in texto:
        if len(texto.split(",")[-1]) == 3:
            texto = texto.replace(",", "")
        else:
            texto = texto.replace(",", ".")

    return convertir_float(texto)


def normalizar_tipo_propiedad(valor: Any) -> str:
    texto = normalizar_texto(valor)

    equivalencias = {
        "apto": "apartamento",
        "apart": "apartamento",
        "town house": "townhouse",
        "towhouse": "townhouse",
        "tohouse": "townhouse",
        "aptoquinta": "apartoquinta",
        "aparto quinta": "apartoquinta",
    }

    if texto in equivalencias:
        return equivalencias[texto]

    for tipo in [
        "townhouse",
        "apartoquinta",
        "penthouse",
        "apartamento",
        "casa",
        "quinta",
        "oficina",
        "local",
        "galpon",
        "terreno",
    ]:
        if tipo in texto:
            return tipo

    return texto


def tokens_nombre(valor: Any) -> Set[str]:
    bloqueadas = {
        "de",
        "del",
        "la",
        "el",
        "los",
        "las",
        "asesor",
        "asesora",
    }

    return {
        token
        for token in normalizar_texto(valor).split()
        if len(token) >= 2 and token not in bloqueadas
    }


def tokens_zona(valor: Any) -> Set[str]:
    bloqueadas = {
        "el",
        "la",
        "los",
        "las",
        "de",
        "del",
        "en",
        "zona",
        "sector",
        "urbanizacion",
        "ciudad",
        "venezuela",
    }

    return {
        token
        for token in normalizar_texto(valor).split()
        if len(token) >= 2 and token not in bloqueadas
    }


def zona_coincide(
    buscada: str,
    zona_prop: str,
    ciudad_prop: str,
) -> bool:
    if not buscada:
        return True

    ciudad_prop_norm = normalizar_texto(ciudad_prop)
    zona_prop_norm = normalizar_texto(zona_prop)
    buscados = tokens_zona(buscada)
    disponibles = tokens_zona(f"{zona_prop} {ciudad_prop}")

    if not buscados:
        return True
    if not disponibles:
        return False

    # 1. 🛡️ FILTRO DE CIUDAD: Si pide una ciudad exacta, debe coincidir.
    ciudades_mettryc = {
        "valencia", "naguanagua", "san diego", "guacara", 
        "barquisimeto", "cabudare", "caracas"
    }
    ciudades_pedidas = buscados.intersection(ciudades_mettryc)
    
    if ciudades_pedidas:
        if not any(ciudad in ciudad_prop_norm for ciudad in ciudades_pedidas):
            return False

    # 2. 🛡️ FILTRO ESTRICTO DE ZONA: Intersección exacta de palabras.
    coincidencias = buscados.intersection(disponibles)
    if len(coincidencias) >= 1:
        return True

    # 3. 🛡️ FILTRO DE SUBCADENAS: Por si la zona en Wasi está escrita diferente.
    for b in buscados:
        if b in zona_prop_norm or b in ciudad_prop_norm:
            return True

    # Si no pasa ninguna de las pruebas de arriba, SE ELIMINA LA PROPIEDAD.
    return False


def extraer_codigo_inmueble(
    mensaje: str,
) -> Optional[str]:
    patrones = [
        r"mettryc\.com/inmueble/(\d+)",
        r"\b(?:codigo|código|cod|inmueble)\s*[:#-]?\s*(\d{4,})\b",
        r"\b(?:ALM|EJL|LR|JM|MFR|TH)-?(\d{4,})\b",
    ]

    for patron in patrones:
        coincidencia = re.search(
            patron,
            mensaje or "",
            re.IGNORECASE,
        )

        if coincidencia:
            return coincidencia.group(1)

    texto = str(mensaje or "").strip()

    if re.fullmatch(r"\d{6,10}", texto):
        return texto

    return None


def detectar_posicion(
    mensaje: str,
) -> Optional[int]:
    texto = normalizar_texto(mensaje)

    referencias = {
        1: [
            "la primera",
            "primera opcion",
            "opcion 1",
            "numero 1",
        ],
        2: [
            "la segunda",
            "segunda opcion",
            "opcion 2",
            "numero 2",
        ],
        3: [
            "la tercera",
            "tercera opcion",
            "opcion 3",
            "numero 3",
            "la ultima",
        ],
    }

    for posicion, frases in referencias.items():
        if any(frase in texto for frase in frases):
            return posicion

    return None


def pide_mas_opciones(mensaje: str) -> bool:
    texto = normalizar_texto(mensaje)

    frases = [
        "mas opciones",
        "otras opciones",
        "otras tres",
        "muestrame otras",
        "enviame otras",
        "quiero ver mas",
        "ninguna me gusto",
        "ninguna me interesa",
        "siguientes opciones",
    ]

    return any(frase in texto for frase in frases)


def menciona_anuncio_sin_codigo(
    mensaje: str,
) -> bool:
    texto = normalizar_texto(mensaje)

    menciona_origen = any(
        palabra in texto
        for palabra in [
            "anuncio",
            "publicacion",
            "instagram",
            "facebook",
            "mercado libre",
            "mercadolibre",
            "portal",
            "pagina web",
            "vi una propiedad",
            "vi una casa",
            "vi un apartamento",
        ]
    )

    return (
        menciona_origen
        and not extraer_codigo_inmueble(mensaje)
    )


def detectar_rol_explicito(
    mensaje: str,
) -> Optional[str]:
    texto = normalizar_texto(mensaje)

    patrones_colega = [
        r"\bsoy\s+(asesor|asesora|agente|corredor|corredora|broker|realtor)\b",
        r"\bsoy\s+colega\b",
        r"\btengo\s+un\s+cliente\b",
        r"\bbusco\s+para\s+un\s+cliente\b",
        r"\btrabajo\s+en\s+una\s+inmobiliaria\b",
        r"\bcomparto\s+comision\b",
    ]

    patrones_cliente = [
        r"\bno\s+soy\s+(asesor|agente|corredor|broker|realtor)\b",
        r"\bsoy\s+cliente\b",
        r"\bes\s+para\s+mi\b",
        r"\bbusco\s+para\s+mi\b",
    ]

    if any(
        re.search(patron, texto)
        for patron in patrones_cliente
    ):
        return "cliente"

    if any(
        re.search(patron, texto)
        for patron in patrones_colega
    ):
        return "colega_inmobiliario"

    return None


def convertir_caracteristicas(valor: Any) -> str:
    if isinstance(valor, str):
        return valor

    if isinstance(valor, list):
        return " ".join(
            str(elemento)
            for elemento in valor
        )

    if isinstance(valor, dict):
        return " ".join(
            f"{clave} {contenido}"
            for clave, contenido in valor.items()
            if contenido not in [
                None,
                "",
                False,
                0,
                "0",
            ]
        )

    return ""

def convertir_caracteristicas(valor: Any) -> str:
    if isinstance(valor, str):
        return valor

    if isinstance(valor, list):
        return " ".join(
            str(elemento)
            for elemento in valor
        )

    if isinstance(valor, dict):
        return " ".join(
            f"{clave} {contenido}"
            for clave, contenido in valor.items()
            if contenido not in [
                None,
                "",
                False,
                0,
                "0",
            ]
        )

    return ""


def verificar_caducidad_y_amnesia(estado: dict) -> dict:
    if not estado.get("actualizado_en"):
        return estado

    ahora = datetime.utcnow()
    ultima_actualizacion = datetime.fromisoformat(estado["actualizado_en"])
    tiempo_inactivo = ahora - ultima_actualizacion
    
    rol = estado.get("rol", "cliente")
    numero_canal = estado.get("numero_canal", "")

    if rol == "colega_inmobiliario" and tiempo_inactivo > timedelta(hours=24):
        logger.info(f"⏳ Sesión de colega ({numero_canal}) caducada por inactividad (24h). Reiniciando estado.")
        return crear_sesion(numero_canal)

    if rol == "cliente" and tiempo_inactivo > timedelta(days=30):
        logger.info(f"⏳ Sesión de cliente ({numero_canal}) superó los 30 días. Reiniciando estado.")
        return crear_sesion(numero_canal)

    if rol == "cliente" and tiempo_inactivo > timedelta(hours=24):
        if estado.get("historial"):
            nombre_log = estado["lead"].get("nombre") or "Desconocido"
            logger.info(f"🧠 Amnesia Selectiva Mettryc: Limpiando mensajes viejos de {nombre_log} (>24h). Filtros conservados.")
            estado["historial"] = []

    return estado


def obtener_pregunta_faltante(estado: dict) -> str:
    filtros = estado.get("filtros", {})
    
    if not filtros.get("tipo_operacion"):
        return "¿Es para la compra o alquiler?"
        
    if not filtros.get("tipo_propiedad"):
        return "¿Qué tipo de inmueble buscas? (Ej: apartamento, casa, townhouse)"
        
    if not filtros.get("zona"):
        return "¿En qué zona o ciudad buscas?"
        
    if not filtros.get("presupuesto_max"):
        return "¿Cuál es tu presupuesto estimado?"
        
    return "¿Hay alguna característica adicional?"


async def humanizar_texto_con_ia(estado: dict, instruccion_cruda: str, mensaje_usuario: str) -> str:
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    
    prompt_sistema = f"""
    Eres Paty, la asistente VIP de Mettryc Realty.
    Tu sistema interno acaba de determinar que necesitas pedirle este dato al usuario:
    "{instruccion_cruda}"
    
    TU TAREA:
    Traduce esa orden rígida a tu personalidad natural, cálida y profesional.
    1. Si el usuario acaba de dar un dato, valídalo brevemente (ej. "¡Excelente zona!").
    2. Luego, haz la pregunta que se te ordenó.
    3. NO hagas más preguntas aparte de la indicada. Sé muy breve (máximo 30 palabras).
    """
    
    mensajes = [{"role": "system", "content": prompt_sistema}]
    
    for msg in estado.get("historial", [])[-4:]:
        mensajes.append({"role": msg["role"], "content": msg["content"]})
        
    mensajes.append({"role": "user", "content": mensaje_usuario})
    
    payload = {
        "model": os.getenv("MODELO_PRINCIPAL", "google/gemini-2.5-flash"),
        "messages": mensajes,
        "max_tokens": 150,
        "temperature": 0.4
    }
    
    try:
        # 🚀 Enlace limpio y conexión resiliente
        resp = await http_client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=15.0
        )
        resp.raise_for_status()
        contenido = resp.json()["choices"][0]["message"]["content"]
        return contenido.strip() if contenido else instruccion_cruda
    except Exception as e:
        logger.error(f"Error humanizando texto: {e}")
        return instruccion_cruda
# ============================================================
# SESIONES
# ============================================================

def crear_sesion(sender: str) -> dict:
    return {
        "rol": None,
        "confianza_rol": 0.0,
        "objetivo": "conversar",
        "filtros": {
            "tipo_operacion": None,
            "tipo_propiedad": None,
            "zona": None,
            "presupuesto_max": None,
            "habitaciones_min": None,
            "banos_min": None,
            "garajes_min": None,
            "caracteristicas": [],
        },
        "sin_preferencia": [],
        "propiedades_enviadas": [],
        "ultimo_lote": [],
        "propiedad_interes": None,
        "esperando_codigo": False,
        "lead": {
            "nombre": None,
            "correo": None,
            "whatsapp": None,
            "whatsapp_confirmado": False,
        },
        "numero_canal": (
            normalizar_telefono(sender)
            if SENDER_ES_WHATSAPP
            else None
        ),
        "lead_id": None,
        "agente_asignado": None,
        "notificacion_enviada": False,
        "historial": [],
        "creado_en": datetime.utcnow().isoformat(),
        "actualizado_en": datetime.utcnow().isoformat(),
    }


def obtener_sesion(sender: str) -> dict:
    if sender not in sesiones:
        sesiones[sender] = crear_sesion(sender)

    return sesiones[sender]


def guardar_sesion(
    sender: str,
    estado: dict,
) -> None:
    estado["actualizado_en"] = (
        datetime.utcnow().isoformat()
    )
    sesiones[sender] = estado


def agregar_historial(
    estado: dict,
    rol: str,
    contenido: str,
) -> None:
    contenido_limpio = str(contenido or "").strip()

    if len(contenido_limpio) > 5000:
        contenido_limpio = contenido_limpio[:5000]

    estado["historial"].append({
        "role": rol,
        "content": contenido_limpio,
    })

    estado["historial"] = estado[
        "historial"
    ][-MAX_HISTORIAL:]


def reiniciar_busqueda(
    estado: dict,
) -> dict:
    rol = estado.get("rol")
    confianza = estado.get("confianza_rol", 0)
    historial = estado.get("historial", [])
    numero_canal = estado.get("numero_canal")

    nuevo = crear_sesion(numero_canal or "")
    nuevo["rol"] = rol
    nuevo["confianza_rol"] = confianza
    nuevo["historial"] = historial
    nuevo["numero_canal"] = numero_canal

    return nuevo


def mensaje_es_duplicado(
    sender: str,
    message_id: str,
) -> bool:
    ahora = time.time()

    expiradas = [
        clave
        for clave, expiracion
        in mensajes_duplicados.items()
        if expiracion <= ahora
    ]

    for clave in expiradas:
        mensajes_duplicados.pop(clave, None)

    clave = f"{sender}:{message_id}"

    if clave in mensajes_duplicados:
        return True

    mensajes_duplicados[clave] = (
        ahora + DUPLICATE_TTL_SECONDS
    )

    return False


# ============================================================
# WASI
# ============================================================

def inventario_necesita_actualizacion() -> bool:
    ultima = inventory_cache.get(
        "ultima_actualizacion"
    )

    if not inventory_cache["inventario"]:
        return True

    if not ultima:
        return True

    return (
        datetime.now() - ultima
        >= INTERVALO_ACTUALIZACION_WASI
    )


async def obtener_inventario_wasi() -> List[dict]:
    if not WASI_TOKEN or not WASI_COMPANY_ID:
        logger.error(
            "Faltan WASI_TOKEN o WASI_COMPANY_ID."
        )
        return []

    propiedades: List[dict] = []
    take = 100
    skip = 0

    for _ in range(100):
        params = {
            "wasi_token": WASI_TOKEN,
            "id_company": WASI_COMPANY_ID,
            "take": take,
            "skip": skip,
            "status": 1,
        }

        data = None

        for intento in range(3):
            try:
                respuesta = await http_client.get(
                    "https://api.wasi.co/v1/property/search",
                    params=params,
                    timeout=WASI_TIMEOUT,
                )
                respuesta.raise_for_status()
                data = respuesta.json()
                break

            except Exception as exc:
                logger.warning(
                    "Error Wasi skip=%s intento=%s tipo=%s",
                    skip,
                    intento + 1,
                    type(exc).__name__,
                )
                await asyncio.sleep(2 ** intento)

        if not isinstance(data, dict):
            break

        cantidad_pagina = 0

        for clave, valor in data.items():
            if not (
                isinstance(valor, dict)
                and str(clave).isdigit()
            ):
                continue

            cantidad_pagina += 1
            property_id = valor.get("id_property")

            if not property_id:
                continue

            usuario = valor.get("user_data") or {}

            descripcion = (
                valor.get("description")
                or valor.get("observations")
                or ""
            )

            caracteristicas = " ".join([
                convertir_caracteristicas(
                    valor.get("features")
                ),
                convertir_caracteristicas(
                    valor.get("internal_features")
                ),
                convertir_caracteristicas(
                    valor.get("external_features")
                ),
            ]).strip()

            captador = (
                f"{usuario.get('first_name', '')} "
                f"{usuario.get('last_name', '')}"
            ).strip()

            propiedades.append({
                "id": str(property_id),
                "titulo": valor.get(
                    "title",
                    "Propiedad Mettryc",
                ),
                "descripcion": descripcion,
                "ciudad": valor.get(
                    "city_label",
                    "N/D",
                ),
                "zona": valor.get(
                    "zone_label",
                    "N/D",
                ),
                "tipo_propiedad_wasi": valor.get(
                    "type_label",
                    "N/D",
                ),
                "precio_venta_float": (
                    parsear_precio_wasi(
                        valor.get("sale_price"),
                        valor.get(
                            "sale_price_label"
                        ),
                    )
                ),
                "precio_renta_float": (
                    parsear_precio_wasi(
                        valor.get("rent_price"),
                        valor.get(
                            "rent_price_label"
                        ),
                    )
                ),
                "area": valor.get("area", "N/D"),
                "habitaciones": valor.get(
                    "bedrooms",
                    "N/D",
                ),
                "banos": valor.get(
                    "bathrooms",
                    "N/D",
                ),
                "garajes": valor.get(
                    "garages",
                    "N/D",
                ),
                "caracteristicas_texto": (
                    caracteristicas
                ),
                "captador_wasi": (
                    captador or "Asesor Mettryc"
                ),
                "telefono_captador_wasi": (
                    usuario.get("phone", "")
                ),
                "enlace": (
                    "https://www.mettryc.com/inmueble/"
                    f"{property_id}"
                ),
            })

        if cantidad_pagina < take:
            break

        skip += take
        await asyncio.sleep(0.25)

    logger.info(
        "Inventario Wasi cargado propiedades=%s",
        len(propiedades),
    )

    return propiedades


async def actualizar_inventario(
    force: bool = False,
) -> bool:
    if (
        not force
        and not inventario_necesita_actualizacion()
    ):
        return False

    async with inventory_refresh_lock:
        if (
            not force
            and not inventario_necesita_actualizacion()
        ):
            return False

        propiedades = await obtener_inventario_wasi()

        if not propiedades:
            logger.error(
                "Wasi no devolvió propiedades. "
                "Se conserva el inventario anterior."
            )
            return False

        inventory_cache["inventario"] = propiedades
        inventory_cache["ultima_actualizacion"] = (
            datetime.now()
        )

        return True


# ============================================================
# GOOGLE SHEETS Y CAPTADORES
# ============================================================

def sheets_necesita_actualizacion() -> bool:
    ultima = sheets_cache.get(
        "ultima_actualizacion"
    )

    if not ultima:
        return True

    return (
        datetime.now() - ultima
        >= INTERVALO_ACTUALIZACION_SHEETS
    )


def agregar_captador_sheet(
    resultado: dict,
    nombre: Any,
    telefono: Any,
) -> None:
    nombre_limpio = str(nombre or "").strip()
    telefono_limpio = normalizar_telefono(telefono)

    if nombre_limpio and telefono_limpio:
        resultado[nombre_limpio] = telefono_limpio


def procesar_captadores_sheet(
    payload: Any,
) -> Dict[str, str]:
    captadores: Dict[str, str] = {}

    if isinstance(payload, dict):
        for nombre, telefono in payload.items():
            if isinstance(telefono, dict):
                agregar_captador_sheet(
                    captadores,
                    telefono.get("nombre") or nombre,
                    telefono.get("telefono")
                    or telefono.get("phone")
                    or telefono.get("whatsapp"),
                )
            else:
                agregar_captador_sheet(
                    captadores,
                    nombre,
                    telefono,
                )

    elif isinstance(payload, list):
        for registro in payload:
            if not isinstance(registro, dict):
                continue

            agregar_captador_sheet(
                captadores,
                registro.get("nombre")
                or registro.get("name")
                or registro.get("asesor")
                or registro.get("captador"),
                registro.get("telefono")
                or registro.get("phone")
                or registro.get("whatsapp"),
            )

    return captadores


async def sincronizar_google_sheet(
    force: bool = False,
) -> bool:
    if not GOOGLE_SHEET_TURNOS_URL:
        logger.warning(
            "GOOGLE_SHEET_TURNOS_URL no configurada."
        )
        return False

    if (
        not force
        and not sheets_necesita_actualizacion()
    ):
        return False

    async with sheets_refresh_lock:
        if (
            not force
            and not sheets_necesita_actualizacion()
        ):
            return False

        try:
            respuesta = await http_client.get(
                GOOGLE_SHEET_TURNOS_URL,
                timeout=SHEETS_TIMEOUT,
                follow_redirects=True,
            )
            respuesta.raise_for_status()
            payload = respuesta.json()

            if not isinstance(payload, dict):
                raise ValueError(
                    "Sheets no devolvió un objeto JSON."
                )

            agentes = payload.get("agentes", [])

            if not isinstance(agentes, list):
                agentes = []

            captadores = procesar_captadores_sheet(
                payload.get("captadores", {})
            )

            # Compatibilidad si el Apps Script devuelve filas.
            if not captadores:
                captadores = procesar_captadores_sheet(
                    payload.get("captadores_data", [])
                    or payload.get("asesores", [])
                )

            sheets_cache["agentes"] = agentes
            sheets_cache["captadores"] = captadores
            sheets_cache["ultima_actualizacion"] = (
                datetime.now()
            )

            logger.info(
                "Sheets sincronizado agentes=%s captadores=%s",
                len(agentes),
                len(captadores),
            )

            return True

        except Exception as exc:
            logger.error(
                "Error Sheets tipo=%s detalle=%s",
                type(exc).__name__,
                str(exc)[:200],
            )
            return False


def cruzar_captador_con_sheet(
    nombre_wasi: str,
) -> dict:
    """
    Cruza el nombre del captador recibido desde Wasi con el
    Google Sheet.

    Primero intenta igualdad exacta normalizada. Si no encuentra,
    calcula coincidencia por tokens del nombre.
    """
    nombre_normalizado = normalizar_texto(
        nombre_wasi
    )

    captadores = sheets_cache.get(
        "captadores",
        {},
    )

    for nombre_sheet, telefono in captadores.items():
        if (
            normalizar_texto(nombre_sheet)
            == nombre_normalizado
        ):
            return {
                "nombre": nombre_sheet,
                "telefono": telefono,
                "tipo_coincidencia": "exacta",
            }

    tokens_wasi = tokens_nombre(nombre_wasi)
    mejor = None
    mejor_score = 0.0

    for nombre_sheet, telefono in captadores.items():
        tokens_sheet = tokens_nombre(nombre_sheet)

        if not tokens_wasi or not tokens_sheet:
            continue

        interseccion = tokens_wasi.intersection(
            tokens_sheet
        )

        union = tokens_wasi.union(tokens_sheet)
        score_jaccard = (
            len(interseccion) / len(union)
            if union
            else 0
        )

        cobertura = (
            len(interseccion) / len(tokens_wasi)
            if tokens_wasi
            else 0
        )

        score = max(
            score_jaccard,
            cobertura,
        )

        if score > mejor_score:
            mejor_score = score
            mejor = {
                "nombre": nombre_sheet,
                "telefono": telefono,
                "tipo_coincidencia": "aproximada",
            }

    if mejor and mejor_score >= 0.65:
        return mejor

    return {
        "nombre": nombre_wasi or "Asesor Mettryc",
        "telefono": None,
        "tipo_coincidencia": "no_encontrada",
    }


async def asignar_agente_round_robin() -> Optional[dict]:
    global round_robin_index

    await sincronizar_google_sheet()

    agentes = [
        deepcopy(agente)
        for agente in sheets_cache["agentes"]
        if isinstance(agente, dict)
        and (
            agente.get("nombre")
            or agente.get("name")
        )
    ]

    if not agentes:
        return None

    async with round_robin_lock:
        round_robin_index = (
            round_robin_index + 1
        ) % len(agentes)

        agente = agentes[round_robin_index]

    if not agente.get("nombre"):
        agente["nombre"] = agente.get("name")

    return agente


# ============================================================
# OPENROUTER
# ============================================================

PROMPT_MAESTRO = """
Eres Paty, la asesora virtual de Mettryc Realty, la Primera
Tecnoinmobiliaria de Venezuela.

Hablas español venezolano de forma cálida, profesional, breve,
natural y humana. Nunca debes parecer un formulario.

TU CONFIGURACION
Temperatura: 0.1
Creatividad: 0.0
RESTRICCIÓN: Tienes Prohibido alucinar y crear informacion que no este contenida en este prompt maestro, tampoco puedes inventar variables distintas a las contenidas aquí. 

TU FUNCIÓN

Debes comprender el mensaje usando toda la conversación y el estado
comercial. Extrae información, identifica la intención y decide qué
herramienta debe ejecutar el sistema.

No inventes propiedades, precios, enlaces, códigos, captadores,
agentes ni disponibilidad. El sistema mostrará las fichas.

REGLAS ESTRICTAS DE EXTRACCIÓN Y FORMATO JSON (¡CRÍTICO!)

1. NO inventes nombres de variables. Usa la clave "actualizaciones" para los filtros. NUNCA uses "filtros" o "filtros_actualizados".
2. Para el campo 'presupuesto_max' usa SIEMPRE Y ÚNICAMENTE números. NUNCA uses letras ni símbolos (ej: si dicen 400 dólares, devuelve 400).
3. Si el cliente da varias zonas separadas por "o" (ej. Prebo o Trigaleña), extrae el texto completo exactamente así en el campo 'zona'.
4. Usa EXACTAMENTE esta estructura de JSON, no agregues ni inventes nada más:
{
  "mensaje": "",
  "actualizaciones": {
    "tipo_operacion": "alquiler",
    "tipo_propiedad": "apartamento",
    "zona": "Prebo o Trigaleña",
    "presupuesto_max": 400
  },
  "accion": {
    "tipo": "buscar_propiedades"
  }
}

COMPORTAMIENTO CONVERSACIONAL

1. Aprovecha cualquier dato dicho anteriormente. Nunca preguntes
   información que ya aparece en el estado.
2. El usuario puede dar requisitos en cualquier orden y con lenguaje
   informal.
3. Si hace una pregunta diferente, respóndela brevemente y retoma de
   manera natural el objetivo pendiente cuando sea oportuno.
4. No interrogues. Haz una o máximo dos preguntas relacionadas.
5. No es obligatorio recopilar todos los filtros.
6. Para buscar normalmente basta con tener:
   - tipo_operacion; y
   - tipo_propiedad; y
   - zona o presupuesto_max.
7. Habitaciones, baños y características son preferencias opcionales.
8. Si el usuario desea ver opciones antes de completar todo, puedes
   ejecutar buscar_propiedades.
9. Si cambia zona, presupuesto, tipo u otra condición, extrae el nuevo
   valor y usa buscar_propiedades.
10. Si dice no importa, cualquiera, ninguna, me da igual o no tengo,
    registra el campo correcto en campos_sin_preferencia.
11. Interpreta números según el contexto de la conversación.
12. No repitas saludos en todos los mensajes.
13. No respondas el mismo mensaje que recibas. ej: si te dicen "Hola esta casa esta disponible?" no respondas "Hola esta casa esta disponible?"

ROL

- "colega_inmobiliario" solo si se identifica explícitamente como
  asesor, agente, broker, corredor, realtor, colega, o dice que busca
  para un cliente.
- No clasifiques como colega a una persona que solamente pide hablar
  con un asesor.
- Si es ambiguo y conocer el rol es necesario, pregúntale naturalmente
  si busca para sí mismo o como colega.
- Para colegas nunca solicites datos personales del cliente.
- Cuando un colega seleccione una propiedad, indícale que puede
  contactar al captador mostrado en la ficha.
- Para clientes, cuando manifiesten interés, activa
  seleccionar_propiedad.

INMUEBLE ESPECÍFICO

- Si proporciona código o enlace de Mettryc, usa buscar_por_codigo.
- Si dice que vio una propiedad en un anuncio, página, Instagram,
  Facebook o portal, pero no proporciona código ni enlace identificable,
  usa pedir_codigo_inmueble y solicítalo naturalmente.
- No asumas que un código de Mercado Libre es el ID de Wasi.
- Si te envian un mensaje como este "Hola, tengo algunas preguntas sobre tu publicación en Mercado Libre: https://inmueble.mercadolibre.com.ve/MLV-1014940508-local-comercial-en-venta-cc-metropolis-san-diego-wc-9687434-_JM" deberás tomar el numero que está antes de -_JM (en este caso 9686434) como el codigo de la propiedad. Pregunta que información adicional quiere saber de ella y busca la respuesta en el inventario.

RESULTADOS

- Si pide otras propiedades, usa mostrar_mas_propiedades.
- Si dice primera, segunda, tercera o última y muestra interés, usa
  seleccionar_propiedad con posicion 1, 2 o 3.
- No describas propiedades que todavía no hayan sido entregadas por
  una herramienta.

CAPTURA DEL CLIENTE

Cuando objetivo sea captura_lead:
- Extrae nombre, correo y WhatsApp de cualquier frase.
- No vuelvas a pedir datos existentes.
- Si quiere usar el mismo número del chat, establece
  usar_numero_actual=true.
- Pide solo los datos faltantes de forma conversacional.
- Puedes solicitar nombre y correo juntos si resulta natural.
- El sistema asignará automáticamente al agente cuando estén completos.

CONSULTAS DE METTRYC

Información permitida:
- Honorarios: 5% en ventas y un mes en alquiler.
- Ubicación: CC Patio Trigal, local 300-6, Valencia, Carabobo.
- Si pregunta si el precio es negociable: puede hacer su mejor oferta
  para presentarla al propietario.
- Nunca compartas el teléfono directo del propietario.
- Reclutamiento: ingreso de $50, incluye curso y credenciales.
- Formulario:
  https://forms.gle/SbLtHrey69fhf3Xt8

ACCIONES

- responder: conversar o hacer una pregunta natural.
- buscar_propiedades: buscar con filtros actuales o nuevos.
- mostrar_mas_propiedades: enviar las siguientes opciones.
- buscar_por_codigo: consultar un código exacto.
- seleccionar_propiedad: el usuario eligió una ficha mostrada.
- pedir_codigo_inmueble: viene de un anuncio y falta el código.
- reiniciar_busqueda: quiere comenzar otra búsqueda.
- pedir_aclaracion: no se entiende un dato relevante.

Devuelve solamente el JSON solicitado.
"""


def limpiar_json_modelo(contenido: str) -> str:
    texto = str(contenido or "").strip()

    if texto.startswith("```"):
        texto = re.sub(
            r"^```(?:json)?\s*",
            "",
            texto,
            flags=re.IGNORECASE,
        )
        texto = re.sub(r"\s*```$", "", texto)

    inicio = texto.find("{")
    fin = texto.rfind("}")

    if inicio >= 0 and fin > inicio:
        return texto[inicio:fin + 1]

    return texto


async def llamar_openrouter_json(
    modelo_pydantic,
    mensajes: List[dict],
    temperatura: float = 0.2,
):
    if not OPENROUTER_API_KEY:
        return None

    headers = {
        "Authorization": (
            f"Bearer {OPENROUTER_API_KEY}"
        ),
        "Content-Type": "application/json",
        "HTTP-Referer": "https://www.mettryc.com",
        "X-Title": "Mettryc Realty Paty",
    }

    modelos = [
        MODELO_AGENTE_PRINCIPAL,
        MODELO_AGENTE_RESPALDO,
    ]

    for modelo in modelos:
        formatos = [
            {
                "type": "json_schema",
                "json_schema": {
                    "name": modelo_pydantic.__name__,
                    "strict": True,
                    "schema": (
                        modelo_pydantic.model_json_schema()
                    ),
                },
            },
            {"type": "json_object"},
        ]

    for response_format in formatos:
                payload = {
                    "model": modelo,
                    "messages": mensajes,
                    "temperature": temperatura,
                    "max_tokens": 900,
                    "response_format": response_format,
                }

                try:
                    # 🚀 Enlace limpio, IA Resucitada
                    respuesta = await http_client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers=headers,
                        json=payload,
                        timeout=OPENROUTER_TIMEOUT,
                    )
                    respuesta.raise_for_status()

                    contenido = (
                        respuesta.json()
                        .get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )

                    if isinstance(contenido, list):
                        contenido = "".join(
                            elemento.get("text", "")
                            for elemento in contenido
                            if isinstance(elemento, dict)
                        )

                    # 🌟 LOG DE RAYOS X 1: IA RAW RESPONSE
                    logger.info(f"🤖 [IA RAW RESPONSE] Éxito con {modelo}: {contenido[:200]}...")

                    contenido = limpiar_json_modelo(
                        contenido
                    )

                    return modelo_pydantic.model_validate_json(
                        contenido
                    )

                except ValidationError as exc:
                    # 🌟 LOG DE RAYOS X 2: IA ERROR FORMATO
                    logger.warning(f"🤖 [IA ERROR FORMATO] JSON de la IA inválido: {exc}")
                    
                except Exception as exc:
                    # 🌟 LOG DE RAYOS X 3: IA ERROR DE RED
                    logger.warning(f"🤖 [IA ERROR RED/API] Falla con {modelo}: {type(exc).__name__} - {str(exc)[:150]}")

    return None


def construir_estado_para_ia(
    estado: dict,
) -> dict:
    propiedad_interes = estado.get(
        "propiedad_interes"
    )

    return {
        "rol": estado.get("rol"),
        "confianza_rol": estado.get(
            "confianza_rol"
        ),
        "objetivo": estado.get("objetivo"),
        "filtros": estado.get("filtros"),
        "sin_preferencia": estado.get(
            "sin_preferencia"
        ),
        "esperando_codigo": estado.get(
            "esperando_codigo"
        ),
        "ultimo_lote": estado.get(
            "ultimo_lote"
        ),
        "propiedad_interes": (
            {
                "id": propiedad_interes.get("id"),
                "titulo": propiedad_interes.get(
                    "titulo"
                ),
            }
            if propiedad_interes
            else None
        ),
        "lead": {
            "nombre": estado["lead"].get(
                "nombre"
            ),
            "correo": estado["lead"].get(
                "correo"
            ),
            "whatsapp": (
                "disponible"
                if estado["lead"].get("whatsapp")
                else None
            ),
            "numero_actual_disponible": bool(
                estado.get("numero_canal")
            ),
        },
    }


async def decidir_con_ia(
    mensaje: str,
    estado: dict,
) -> DecisionAgente:
    contexto = {
        "estado_comercial": (
            construir_estado_para_ia(estado)
        ),
        "mensaje_actual": mensaje,
    }

    mensajes = [
        {
            "role": "system",
            "content": PROMPT_MAESTRO,
        },
        *estado.get("historial", [])[-12:],
        {
            "role": "user",
            "content": (
                "Analiza el mensaje actual usando el estado "
                "comercial. Devuelve la decisión estructurada.\n\n"
                + json.dumps(
                    contexto,
                    ensure_ascii=False,
                )
            ),
        },
    ]

    decision = await llamar_openrouter_json(
        DecisionAgente,
        mensajes,
        temperatura=0.2,
    )

    if decision:
        return decision

    return decision_fallback(
        mensaje,
        estado,
    )


async def redactar_resultado_ia(
    estado: dict,
    cantidad: int,
    aproximadas: int,
    especifica: bool = False,
) -> TextoResultado:
    rol = estado.get("rol") or "cliente"

    instrucciones = {
        "rol": rol,
        "cantidad": cantidad,
        "aproximadas": aproximadas,
        "propiedad_especifica": especifica,
        "reglas": [
            "Redacta una introducción breve y natural.",
            "No inventes información de propiedades.",
            "No incluyas fichas, precios ni enlaces.",
            "El cierre debe invitar a seleccionar una opción o pedir más.",
            "Si es colega, menciona que la ficha incluye el captador.",
            "Si es cliente, no menciones datos de captadores.",
        ],
    }

    mensajes = [
        {
            "role": "system",
            "content": (
                "Eres Paty de Mettryc Realty. Redacta el texto "
                "que acompaña fichas generadas por el sistema. "
                "DEBES devolver EXCLUSIVAMENTE un objeto JSON con dos claves exactas: "
                "'introduccion' y 'cierre'. NINGUNA OTRA CLAVE ESTÁ PERMITIDA."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                instrucciones,
                ensure_ascii=False,
            ),
        },
    ]

    resultado = await llamar_openrouter_json(
        TextoResultado,
        mensajes,
        temperatura=0.4,
    )

    if resultado:
        return resultado

    if especifica:
        introduccion = (
            "Claro, esta es la propiedad que consultaste:"
        )
    elif aproximadas:
        introduccion = (
            "Encontré estas opciones. Algunas son aproximadas, "
            "pero pueden valer la pena:"
        )
    else:
        introduccion = (
            "Encontré estas opciones que encajan muy bien:"
        )

    if rol == "colega_inmobiliario":
        cierre = (
            "Puedes contactar al captador indicado en cada ficha "
            "o pedirme más opciones."
        )
    else:
        cierre = (
            "Dime cuál te interesa o escribe “más opciones”."
        )

    return TextoResultado(
        introduccion=introduccion,
        cierre=cierre,
    )


def decision_fallback(
    mensaje: str,
    estado: dict,
) -> DecisionAgente:
    codigo = extraer_codigo_inmueble(mensaje)
    posicion = detectar_posicion(mensaje)
    rol = detectar_rol_explicito(mensaje)

    if codigo:
        return DecisionAgente(
            mensaje="",
            rol=rol,
            confianza_rol=1.0 if rol else 0,
            intencion="consulta_inmueble",
            accion=AccionAgente(
                tipo="buscar_por_codigo",
                codigo=codigo,
            ),
        )

    if pide_mas_opciones(mensaje):
        return DecisionAgente(
            mensaje="",
            rol=rol,
            confianza_rol=1.0 if rol else 0,
            intencion="mas_opciones",
            accion=AccionAgente(
                tipo="mostrar_mas_propiedades"
            ),
        )

    if posicion:
        return DecisionAgente(
            mensaje="",
            rol=rol,
            confianza_rol=1.0 if rol else 0,
            intencion="interes_propiedad",
            accion=AccionAgente(
                tipo="seleccionar_propiedad",
                posicion=posicion,
            ),
        )

    if menciona_anuncio_sin_codigo(mensaje):
        return DecisionAgente(
            mensaje=(
                "¡Claro! Envíame el código que aparece en el "
                "anuncio o el enlace de la propiedad y te muestro "
                "la ficha exacta."
            ),
            rol=rol,
            confianza_rol=1.0 if rol else 0,
            intencion="anuncio_sin_codigo",
            accion=AccionAgente(
                tipo="pedir_codigo_inmueble"
            ),
        )

    return DecisionAgente(
        mensaje=(
            "¡Con gusto te ayudo! Cuéntame qué tipo de propiedad "
            "buscas, si es para comprar o alquilar y la zona que "
            "prefieres."
        ),
        rol=rol,
        confianza_rol=1.0 if rol else 0,
        intencion="conversar",
        accion=AccionAgente(
            tipo="responder"
        ),
    )


# ============================================================
# ACTUALIZACIÓN DEL ESTADO
# ============================================================

def normalizar_campo_sin_preferencia(
    campo: str,
) -> Optional[str]:
    texto = normalizar_texto(campo).replace(
        " ",
        "_",
    )

    equivalencias = {
        "presupuesto": "presupuesto_max",
        "precio": "presupuesto_max",
        "habitaciones": "habitaciones_min",
        "habitacion": "habitaciones_min",
        "cuartos": "habitaciones_min",
        "banos": "banos_min",
        "bano": "banos_min",
        "garajes": "garajes_min",
        "garaje": "garajes_min",
        "puestos": "garajes_min",
        "caracteristica": "caracteristicas",
        "caracteristicas_especiales": "caracteristicas",
        "ubicacion": "zona",
        "tipo": "tipo_propiedad",
    }

    texto = equivalencias.get(texto, texto)

    validos = {
        "tipo_operacion",
        "tipo_propiedad",
        "zona",
        "presupuesto_max",
        "habitaciones_min",
        "banos_min",
        "garajes_min",
        "caracteristicas",
    }

    return texto if texto in validos else None


def aplicar_decision(
    estado: dict,
    decision: DecisionAgente,
    mensaje: str,
) -> bool:
    hubo_cambio_busqueda = False

    rol_explicito = detectar_rol_explicito(
        mensaje
    )

    if rol_explicito:
        estado["rol"] = rol_explicito
        estado["confianza_rol"] = 1.0

    elif (
        decision.rol
        and decision.confianza_rol >= 0.80
        and (
            not estado.get("rol")
            or decision.confianza_rol
            >= estado.get("confianza_rol", 0)
        )
    ):
        estado["rol"] = decision.rol
        estado["confianza_rol"] = (
            decision.confianza_rol
        )

    actualizaciones = (
        decision.actualizaciones.model_dump()
    )

    campos_busqueda = [
        "tipo_operacion",
        "tipo_propiedad",
        "zona",
        "presupuesto_max",
        "habitaciones_min",
        "banos_min",
        "garajes_min",
        "caracteristicas",
    ]

    for campo in campos_busqueda:
        valor = actualizaciones.get(campo)

        if valor in [None, "", []]:
            continue

        if campo == "tipo_propiedad":
            valor = normalizar_tipo_propiedad(
                valor
            )

        if campo == "caracteristicas":
            valor = list(dict.fromkeys(
                normalizar_texto(elemento)
                for elemento in valor
                if normalizar_texto(elemento)
            ))

        anterior = estado["filtros"].get(campo)

        if anterior != valor:
            estado["filtros"][campo] = valor
            hubo_cambio_busqueda = True

            if campo in estado["sin_preferencia"]:
                estado["sin_preferencia"].remove(
                    campo
                )

    for campo_original in (
        decision.campos_sin_preferencia
    ):
        campo = normalizar_campo_sin_preferencia(
            campo_original
        )

        if not campo:
            continue

        if campo not in estado["sin_preferencia"]:
            estado["sin_preferencia"].append(
                campo
            )

        nuevo_valor = (
            []
            if campo == "caracteristicas"
            else None
        )

        if estado["filtros"].get(campo) != nuevo_valor:
            estado["filtros"][campo] = nuevo_valor
            hubo_cambio_busqueda = True

    lead = estado["lead"]

    nombre = actualizaciones.get("nombre")

    if nombre and nombre_valido(nombre):
        lead["nombre"] = normalizar_nombre(
            nombre
        )

    correo = (
        extraer_correo(mensaje)
        or actualizaciones.get("correo")
    )

    if correo and correo_valido(correo):
        lead["correo"] = correo.lower()

    telefono = (
        extraer_telefono(mensaje)
        or actualizaciones.get("whatsapp")
    )

    if telefono:
        telefono = normalizar_telefono(telefono)

        if telefono:
            lead["whatsapp"] = telefono
            lead["whatsapp_confirmado"] = True

    if (
        actualizaciones.get(
            "usar_numero_actual"
        )
        and estado.get("numero_canal")
    ):
        lead["whatsapp"] = estado[
            "numero_canal"
        ]
        lead["whatsapp_confirmado"] = True

    if (
        hubo_cambio_busqueda
        and estado.get("propiedades_enviadas")
    ):
        estado["propiedades_enviadas"] = []
        estado["ultimo_lote"] = []
        estado["propiedad_interes"] = None

    return hubo_cambio_busqueda


def criterios_suficientes(
    estado: dict,
) -> bool:
    filtros = estado["filtros"]

    return bool(
        filtros.get("tipo_operacion")
        and filtros.get("tipo_propiedad")
        and (
            filtros.get("zona")
            or filtros.get("presupuesto_max")
        )
    )


def lead_completo(
    estado: dict,
) -> bool:
    lead = estado["lead"]

    return bool(
        nombre_valido(lead.get("nombre"))
        and correo_valido(lead.get("correo"))
        and normalizar_telefono(
            lead.get("whatsapp")
        )
        and lead.get("whatsapp_confirmado")
    )


def datos_lead_faltantes(
    estado: dict,
) -> List[str]:
    lead = estado["lead"]
    faltantes = []

    if not nombre_valido(lead.get("nombre")):
        faltantes.append("nombre completo")

    if not correo_valido(lead.get("correo")):
        faltantes.append("correo electrónico")

    if not (
        normalizar_telefono(
            lead.get("whatsapp")
        )
        and lead.get("whatsapp_confirmado")
    ):
        faltantes.append(
            "confirmación del número de WhatsApp"
        )

    return faltantes


# ============================================================
# RANKING Y EVALUACIÓN ESTRICTA
# ============================================================

def obtener_precio(propiedad: dict, operacion: str) -> float:
    if operacion == "alquiler":
        return convertir_float(propiedad.get("precio_renta_float"))
    return convertir_float(propiedad.get("precio_venta_float"))

def coincide_tipo(propiedad: dict, tipo_buscado: str) -> bool:
    if not tipo_buscado: return True
    buscado = normalizar_tipo_propiedad(tipo_buscado)
    tipo_wasi = normalizar_tipo_propiedad(propiedad.get("tipo_propiedad_wasi", ""))
    titulo = normalizar_texto(propiedad.get("titulo", ""))

    if buscado == "casa":
        aceptados = {"casa", "quinta", "townhouse", "apartoquinta"}
        return any(tipo in tipo_wasi or tipo in titulo for tipo in aceptados)
    if buscado == "apartamento":
        return any(tipo in tipo_wasi or tipo in titulo for tipo in ["apartamento", "penthouse"])

    return buscado in tipo_wasi or buscado in titulo

def evaluar_propiedad_estricta(original: dict, filtros: dict) -> tuple[bool, str, dict]:
    propiedad = deepcopy(original)
    
    operacion = filtros.get("tipo_operacion")
    tipo = filtros.get("tipo_propiedad")
    zona = filtros.get("zona")
    presupuesto = filtros.get("presupuesto_max")
    habs_req = filtros.get("habitaciones_min")
    banos_req = filtros.get("banos_min")
    garajes_req = filtros.get("garajes_min")
    caracteristicas = filtros.get("caracteristicas", [])

    precio = obtener_precio(propiedad, operacion) if operacion else obtener_precio(propiedad, "venta")
    
    # 1. 🛡️ ELIMINATORIO: Operación y Precio
    if operacion and precio <= 0:
        return False, "rechazada_operacion", propiedad

    # 2. 🛡️ ELIMINATORIO: Tipo de Inmueble
    if tipo and not coincide_tipo(propiedad, tipo):
        return False, "rechazada_tipo", propiedad

    # 3. 🛡️ ELIMINATORIO: Zona
    if zona and not zona_coincide(zona, propiedad.get("zona", ""), propiedad.get("ciudad", "")):
        return False, "rechazada_zona", propiedad

    es_exacta = True
    diferencias = []

    # 4. 🛡️ TOLERANCIA 20%: Presupuesto
    if presupuesto and precio > 0:
        if precio > presupuesto:
            if precio <= presupuesto * 1.20:
                es_exacta = False
                diferencias.append(f"Inversión {formato_moneda(precio)}")
            else:
                return False, "rechazada_precio", propiedad

    # 5. 🛡️ TOLERANCIA +/- 1: Espacios
    for val_req, key_prop, label in [(habs_req, "habitaciones", "habs"), (banos_req, "banos", "baños"), (garajes_req, "garajes", "puestos")]:
        if val_req is not None and val_req > 0:
            val_prop = convertir_entero(propiedad.get(key_prop))
            if val_prop < val_req - 1 or val_prop > val_req + 1:
                return False, f"rechazada_{key_prop}", propiedad
            elif val_prop != val_req:
                es_exacta = False
                diferencias.append(f"{val_prop} {label}")

    # 6. 🛡️ RECONOCIMIENTO DE PALABRAS CLAVE
    texto_propiedad = normalizar_texto(" ".join([str(propiedad.get("titulo", "")), str(propiedad.get("descripcion", "")), str(propiedad.get("caracteristicas_texto", ""))]))
    for car in caracteristicas:
        if normalizar_texto(car) not in texto_propiedad:
            es_exacta = False
            diferencias.append(f"No especifica '{car}'")

    propiedad["_coincidencia"] = "exacta" if es_exacta else "aproximada"
    propiedad["_diferencias"] = diferencias
    propiedad["operacion_buscada"] = operacion

    return True, ("exacta" if es_exacta else "tolerancia"), propiedad

def buscar_mejores_propiedades(estado: dict, cantidad: int = 3) -> tuple[List[dict], str]:
    excluir = {str(pid) for pid in estado.get("propiedades_enviadas", [])}
    exactas = []
    tolerancia = []
    pasaron_zona_tipo = 0
    
    stats = {"rechazada_operacion": 0, "rechazada_tipo": 0, "rechazada_zona": 0, "rechazada_precio": 0, "rechazada_habitaciones": 0, "rechazada_banos": 0, "rechazada_garajes": 0}
    
    for original in inventory_cache["inventario"]:
        if str(original.get("id", "")) in excluir:
            continue
            
        is_match, category, prop = evaluar_propiedad_estricta(original, estado["filtros"])
        
        if is_match:
            if category == "exacta": 
                exactas.append(prop)
            else: 
                tolerancia.append(prop)
        else:
            stats[category] = stats.get(category, 0) + 1
            if category in ["rechazada_precio", "rechazada_habitaciones", "rechazada_banos", "rechazada_garajes"]:
                pasaron_zona_tipo += 1

    op = estado["filtros"].get("tipo_operacion", "venta")
    exactas = sorted(exactas, key=lambda p: obtener_precio(p, op))
    tolerancia = sorted(tolerancia, key=lambda p: obtener_precio(p, op))
    
    resultado = exactas[:cantidad]
    if len(resultado) < cantidad:
        resultado.extend(tolerancia[:cantidad - len(resultado)])
        
    motivo_falla = "precio_o_caracteristicas" if not resultado and pasaron_zona_tipo > 0 else "zona_o_tipo"
    
    logger.info(f"📊 [MOTOR DE BÚSQUEDA] Evaluadas: {len(inventory_cache['inventario'])}")
    logger.info(f"📊 [MOTOR DE BÚSQUEDA] Descartadas -> Zona: {stats.get('rechazada_zona',0)} | Tipo: {stats.get('rechazada_tipo',0)} | Operación: {stats.get('rechazada_operacion',0)} | Precio: {stats.get('rechazada_precio',0)}")
    logger.info(f"📊 [MOTOR DE BÚSQUEDA] Encontradas -> Exactas: {len(exactas)} | Tolerancia: {len(tolerancia)}")
    
    return resultado, motivo_falla

def buscar_por_codigo(codigo: str) -> Optional[dict]:
    codigo_limpio = re.sub(r"\D", "", str(codigo or ""))
    if not codigo_limpio:
        return None
    for propiedad in inventory_cache["inventario"]:
        if str(propiedad.get("id", "")) == codigo_limpio:
            return deepcopy(propiedad)
    return None

# ============================================================
# FICHAS
# ============================================================

async def formatear_ficha(
    propiedad: dict,
    es_colega: bool,
    posicion: Optional[int] = None,
) -> str:
    operacion = propiedad.get(
        "operacion_buscada",
        "venta",
    )

    precio = obtener_precio(
        propiedad,
        operacion,
    )

    titulo = propiedad.get(
        "titulo",
        "Propiedad Mettryc",
    )

    if posicion:
        titulo = f"Opción {posicion}: {titulo}"

    area = propiedad.get("area", "N/D")

    area_texto = (
        f"{area} m²"
        if str(area) not in {
            "",
            "N/D",
            "None",
        }
        else "N/D"
    )

    lineas = [
        f"*{titulo}*",
        (
            f"📍 {propiedad.get('zona', 'N/D')}, "
            f"{propiedad.get('ciudad', 'N/D')}"
        ),
        f"💰 {formato_moneda(precio)}",
        (
            f"📐 {area_texto} | "
            f"🛏️ {propiedad.get('habitaciones', 'N/D')} | "
            f"🛁 {propiedad.get('banos', 'N/D')} | "
            f"🚗 {propiedad.get('garajes', 'N/D')}"
        ),
        f"🔗 {propiedad.get('enlace', '')}",
    ]

    diferencias = propiedad.get(
        "_diferencias",
        [],
    )

    if diferencias:
        lineas.append(
            "ℹ️ *Consideraciones:* "
            + "; ".join(diferencias[:2])
        )

    if es_colega:
        captador_wasi = propiedad.get(
            "captador_wasi",
            "Asesor Mettryc",
        )

        cruce = cruzar_captador_con_sheet(
            captador_wasi
        )

        telefono = cruce.get("telefono")

        lineas.append(
            f"👤 *Captador:* {cruce.get('nombre') or captador_wasi}"
        )

        if telefono:
            # 🚀 Enlace Limpio
            lineas.append(
                f"📲 *WhatsApp captador:* "
                f"https://wa.me/{telefono}"
            )
        else:
            lineas.append(
                "📲 *WhatsApp captador:* "
                "No localizado en el directorio."
            )

    return "\n".join(lineas)


async def construir_respuesta_fichas(
    estado: dict,
    propiedades: List[dict],
    especifica: bool = False,
) -> str:
    es_colega = (
        estado.get("rol")
        == "colega_inmobiliario"
    )

    if es_colega:
        await sincronizar_google_sheet()

    aproximadas = sum(
        1
        for propiedad in propiedades
        if propiedad.get("_coincidencia")
        == "aproximada"
    )

    textos = await redactar_resultado_ia(
        estado,
        cantidad=len(propiedades),
        aproximadas=aproximadas,
        especifica=especifica,
    )

    fichas = []

    for indice, propiedad in enumerate(
        propiedades,
        start=1,
    ):
        fichas.append(
            await formatear_ficha(
                propiedad,
                es_colega,
                indice if not especifica else None,
            )
        )

    return "\n\n".join([
        textos.introduccion.strip(),
        *fichas,
        textos.cierre.strip(),
    ])


# ============================================================
# TELEGRAM
# ============================================================

async def enviar_telegram(
    chat_id: str,
    mensaje: str,
) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False

    try:
        respuesta = await http_client.post(
            (
                "https://api.telegram.org/bot"
                f"{TELEGRAM_BOT_TOKEN}/sendMessage"
            ),
            json={
                "chat_id": chat_id,
                "text": mensaje,
                "disable_web_page_preview": True,
            },
            timeout=TELEGRAM_TIMEOUT,
        )

        respuesta.raise_for_status()
        payload = respuesta.json()

        return bool(payload.get("ok"))

    except Exception as exc:
        logger.error(
            "Error Telegram chat=%s tipo=%s",
            str(chat_id)[-4:],
            type(exc).__name__,
        )
        return False


def resumen_filtros(estado: dict) -> str:
    filtros = estado["filtros"]
    lineas = []

    etiquetas = {
        "tipo_operacion": "Operación",
        "tipo_propiedad": "Tipo",
        "zona": "Zona",
        "habitaciones_min": "Habitaciones mínimas",
        "banos_min": "Baños mínimos",
        "garajes_min": "Puestos de estacionamiento"
    }

    for campo, etiqueta in etiquetas.items():
        valor = filtros.get(campo)

        if valor not in [None, "", []]:
            lineas.append(
                f"- {etiqueta}: {valor}"
            )

    if filtros.get("presupuesto_max"):
        lineas.append(
            "- Presupuesto: "
            + formato_moneda(
                filtros["presupuesto_max"]
            )
        )

    if filtros.get("caracteristicas"):
        lineas.append(
            "- Características: "
            + ", ".join(
                filtros["caracteristicas"]
            )
        )

    return "\n".join(lineas) or "- Sin filtros específicos"


async def notificar_lead(
    estado: dict,
) -> bool:
    lead = estado["lead"]
    propiedad = estado.get(
        "propiedad_interes"
    )
    agente = estado.get(
        "agente_asignado"
    )

    propiedad_texto = "No especificada"

    if propiedad:
        propiedad_texto = (
            f"{propiedad.get('titulo')}\n"
            f"ID: {propiedad.get('id')}\n"
            f"{propiedad.get('enlace')}"
        )

    whatsapp = normalizar_telefono(
        lead.get("whatsapp")
    )

    # 🚀 Enlace Limpio
    mensaje = (
        "🏠 NUEVO LEAD METTRYC\n\n"
        f"ID: {estado.get('lead_id')}\n"
        f"Nombre: {lead.get('nombre')}\n"
        f"Correo: {lead.get('correo')}\n"
        f"WhatsApp: {whatsapp}\n"
        f"Contacto: https://wa.me/{whatsapp}\n\n"
        "📋 BÚSQUEDA\n"
        f"{resumen_filtros(estado)}\n\n"
        "⭐ PROPIEDAD DE INTERÉS\n"
        f"{propiedad_texto}\n\n"
        "👤 AGENTE ASIGNADO\n"
        f"{agente.get('nombre') if agente else 'Sin asignar'}"
    )

    destinos = set(TELEGRAM_ADMIN_IDS)

    if agente:
        telegram_id = (
            agente.get("telegram_id")
            or agente.get("telegram")
            or agente.get("chat_id")
        )

        if telegram_id:
            destinos.add(
                str(telegram_id).strip()
            )

    resultados = []

    for destino in destinos:
        resultados.append(
            await enviar_telegram(
                destino,
                mensaje,
            )
        )

    return any(resultados)


# ============================================================
# MOTOR CONVERSACIONAL
# ============================================================

async def mostrar_propiedades(
    estado: dict,
) -> str:
    propiedades, motivo_falla = buscar_mejores_propiedades(
        estado,
        MAX_PROPIEDADES_POR_LOTE,
    )

    # 🛡️ RESPUESTAS INTELIGENTES POR FALLA
    if not propiedades:
        estado["ultimo_lote"] = []
        
        filtros = estado.get("filtros", {})
        tipo_str = normalizar_nombre(filtros.get("tipo_propiedad", "inmuebles"))
        zona_str = normalizar_nombre(filtros.get("zona", "esa zona"))
        presupuesto = filtros.get("presupuesto_max")
        
        if motivo_falla == "precio_o_caracs":
            return (
                f"No tenemos {tipo_str} en {zona_str} por "
                f"{formato_moneda(presupuesto) if presupuesto else 'ese precio'}. "
                "¿Busco en otro rango de inversión o ampliamos la zona?"
            )
        else:
            return (
                f"No disponemos de {tipo_str} en {zona_str} en este momento. "
                "¿Te puedo ofrecer opciones en otra zona?"
            )

    ids = [
        str(propiedad["id"])
        for propiedad in propiedades
    ]

    estado["propiedades_enviadas"].extend(ids)
    estado["ultimo_lote"] = ids
    estado["objetivo"] = "evaluar_resultados"

    return await construir_respuesta_fichas(
        estado,
        propiedades,
    )


async def mostrar_inmueble_especifico(
    estado: dict,
    codigo: str,
) -> str:
    propiedad = buscar_por_codigo(codigo)

    if not propiedad:
        estado["esperando_codigo"] = True

        return (
            f"No encontré un inmueble activo con el código "
            f"{codigo}. Revisa si está escrito correctamente o "
            "envíame el enlace de la publicación."
        )

    operacion = estado["filtros"].get(
        "tipo_operacion"
    )

    if not operacion:
        if propiedad.get(
            "precio_venta_float",
            0,
        ) > 0:
            operacion = "venta"
        else:
            operacion = "alquiler"

    propiedad["operacion_buscada"] = operacion

    property_id = str(propiedad["id"])

    estado["ultimo_lote"] = [property_id]

    if property_id not in estado[
        "propiedades_enviadas"
    ]:
        estado["propiedades_enviadas"].append(
            property_id
        )

    estado["esperando_codigo"] = False
    estado["objetivo"] = "evaluar_resultados"

    return await construir_respuesta_fichas(
        estado,
        [propiedad],
        especifica=True,
    )


async def seleccionar_propiedad(
    estado: dict,
    posicion: Optional[int],
) -> str:
    lote = estado.get("ultimo_lote", [])

    if not lote:
        return (
            "Todavía no tengo una lista reciente para identificar "
            "esa opción. Si tienes el código o el enlace, envíamelo "
            "y la busco directamente."
        )

    if posicion is None:
        if len(lote) == 1:
            posicion = 1
        else:
            return (
                "¡Claro! ¿Te interesa la primera, segunda o "
                "tercera opción?"
            )

    indice = posicion - 1

    if indice < 0 or indice >= len(lote):
        return (
            "No pude identificar esa opción. Indícame si es la "
            "primera, segunda o tercera."
        )

    propiedad = buscar_por_codigo(
        lote[indice]
    )

    if not propiedad:
        return (
            "Esa propiedad ya no aparece activa en el inventario. "
            "Puedo ayudarte a buscar otra similar."
        )

    estado["propiedad_interes"] = propiedad

    if (
        estado.get("rol")
        == "colega_inmobiliario"
    ):
        await sincronizar_google_sheet()
        cruce = cruzar_captador_con_sheet(
            propiedad.get(
                "captador_wasi",
                "",
            )
        )

        if cruce.get("telefono"):
            # 🚀 Enlace Limpio
            return (
                "Perfecto, colega. El captador de esa propiedad "
                f"es {cruce['nombre']}. Puedes comunicarte por "
                f"WhatsApp aquí: https://wa.me/{cruce['telefono']}. "
                "Si quieres, también puedo revisar otras opciones."
            )

        return (
            "Perfecto, colega. Identifiqué la propiedad, pero el "
            "captador no aparece actualmente en el directorio de "
            "Google Sheets. Puedes solicitar apoyo a la oficina o "
            "pedirme otras opciones."
        )

    estado["objetivo"] = "captura_lead"

    faltantes = datos_lead_faltantes(
        estado
    )

    return (
        "¡Excelente elección! Para asignarte un asesor necesito "
        + ", ".join(faltantes)
        + ". Puedes enviarme esos datos en un solo mensaje si "
          "te resulta más cómodo."
    )


async def completar_y_asignar_lead(
    estado: dict,
) -> str:
    if not estado.get("lead_id"):
        estado["lead_id"] = str(uuid.uuid4())

    if not estado.get("agente_asignado"):
        estado["agente_asignado"] = (
            await asignar_agente_round_robin()
        )

    if not estado.get("notificacion_enviada"):
        estado["notificacion_enviada"] = (
            await notificar_lead(estado)
        )

    estado["objetivo"] = "lead_asignado"

    agente = estado.get("agente_asignado")

    if agente:
        return (
            f"¡Listo, {estado['lead']['nombre']}! "
            f"{agente.get('nombre')} recibió tu solicitud y "
            "te contactará por WhatsApp para ayudarte con la "
            "propiedad. ¡Gracias por confiar en Mettryc Realty!"
        )

    return (
        f"¡Listo, {estado['lead']['nombre']}! Registré tu solicitud "
        "y el equipo de Mettryc Realty te contactará por WhatsApp."
    )


def forzar_accion_evidente(
    decision: DecisionAgente,
    mensaje: str,
    estado: dict,
) -> DecisionAgente:
    codigo = extraer_codigo_inmueble(mensaje)

    if codigo:
        decision.accion = AccionAgente(
            tipo="buscar_por_codigo",
            codigo=codigo,
        )
        return decision

    if (
        estado.get("esperando_codigo")
        and re.fullmatch(
            r"\d{4,10}",
            mensaje.strip(),
        )
    ):
        decision.accion = AccionAgente(
            tipo="buscar_por_codigo",
            codigo=mensaje.strip(),
        )
        return decision

    if pide_mas_opciones(mensaje):
        decision.accion = AccionAgente(
            tipo="mostrar_mas_propiedades"
        )
        return decision

    posicion = detectar_posicion(mensaje)

    if posicion and estado.get("ultimo_lote"):
        decision.accion = AccionAgente(
            tipo="seleccionar_propiedad",
            posicion=posicion,
        )
        return decision

    if menciona_anuncio_sin_codigo(mensaje):
        decision.accion = AccionAgente(
            tipo="pedir_codigo_inmueble"
        )

    return decision


async def procesar_mensaje(
    sender: str,
    mensaje: str,
) -> str:
    # 🌟 LOG DE RAYOS X 5: INPUT DEL USUARIO
    logger.info(f"📨 [INPUT] Recibido de {sender[-4:]}: '{mensaje}'")
    estado = obtener_sesion(sender)

    # 1. Recuperamos el estado de la memoria RAM de Render
    estado = obtener_sesion(sender)

    # 🚨 LA INYECCIÓN MAESTRA: Validamos el tiempo de inactividad antes de que intervenga la IA
    estado = verificar_caducidad_y_amnesia(estado)

    # 2. Procedemos con el flujo normal que ya tienes funcionando perfectamente
    decision = await decidir_con_ia(mensaje, estado)
    
    # 🌟 LOG DE RAYOS X 6: EXTRACCIÓN DE LA IA
    logger.info(f"🧠 [IA EXTRACCIÓN] Decisión: {decision.accion.tipo} | Filtros extraídos: {decision.actualizaciones.model_dump(exclude_unset=True)}")

    decision = forzar_accion_evidente(
        decision,
        mensaje,
        estado,
    )

    hubo_cambio = aplicar_decision(
        estado,
        decision,
        mensaje,
    )

    # --- 🛡️ MEGA-CAZADOR DE PYTHON ---
    texto_normalizado = normalizar_texto(mensaje)
    filtros_actuales = estado.setdefault("filtros", {})
    
    # A. Cazador de Presupuesto (Mejorado para detectar números sueltos si son lógicos)
    if not filtros_actuales.get("presupuesto_max"):
        # Primero busca con símbolo de moneda
        match_precio = re.search(r"(\d{1,3}(?:[.,]\d{3})*|\d+)\s*(?:mil|k)?\s*(?:\$|usd|dolares|dls)", texto_normalizado)
        # Si no lo encuentra, busca un número entre 100 y 999999 que parezca presupuesto
        if not match_precio:
            match_precio = re.search(r"\b([1-9]\d{2,5})\b", texto_normalizado)
            
        if match_precio:
            try:
                num_str = match_precio.group(1).replace(".", "").replace(",", "")
                precio_forzado = float(num_str)
                if "mil" in texto_normalizado or "k" in texto_normalizado: precio_forzado *= 1000
                filtros_actuales["presupuesto_max"] = precio_forzado
                presupuesto_detectado = precio_forzado
                hubo_cambio = True
            except ValueError: pass

    # B. Cazador de Operación (Inteligencia Financiera)
    if not filtros_actuales.get("tipo_operacion"):
        if any(p in texto_normalizado for p in ["venta", "comprar", "compra", "inversion"]):
            filtros_actuales["tipo_operacion"] = "venta"
            hubo_cambio = True
        elif any(p in texto_normalizado for p in ["alquiler", "alquilar", "canon", "arrendar"]):
            filtros_actuales["tipo_operacion"] = "alquiler"
            hubo_cambio = True
        elif presupuesto_detectado > 0:
            # 🛡️ SENTIDO COMÚN: Presupuestos mayores de $4000 se asumen como venta.
            if presupuesto_detectado > 4000:
                filtros_actuales["tipo_operacion"] = "venta"
            else:
                filtros_actuales["tipo_operacion"] = "alquiler"
            hubo_cambio = True

    # C. Cazador de Tipo Inmueble
    if not filtros_actuales.get("tipo_propiedad"):
        tipos_posibles = ["apartamento", "townhouse", "casa", "local", "galpon", "oficina", "terreno", "apartoquinta"]
        for t in tipos_posibles:
            if t in texto_normalizado:
                filtros_actuales["tipo_propiedad"] = t
                hubo_cambio = True
                break

    # D. Cazador de Zonas (Acepta zonas múltiples y respeta el "o")
    try:
        from geografia import DICCIONARIO_GEOGRAFICO
        zonas_encontradas = []

        for estado_geo, ciudades in DICCIONARIO_GEOGRAFICO.items():
            for ciudad_dict, zonas_dict in ciudades.items():
                ciudad_norm = normalizar_texto(ciudad_dict)
                if ciudad_norm in texto_normalizado:
                    zonas_encontradas.append(ciudad_dict)
                for zona in zonas_dict:
                    zona_norm = normalizar_texto(zona)
                    if zona_norm in texto_normalizado and len(zona_norm) > 3:
                        zonas_encontradas.append(zona)

        if zonas_encontradas:
            zonas_unicas = list(dict.fromkeys(zonas_encontradas))
            # 🚀 FIX: Evitamos que Python lea 'None' como si fuera la palabra "None"
            zona_ia = str(filtros_actuales.get("zona") or "")
            
            if not zona_ia or len(zonas_unicas) > len(zona_ia.split(" o ")):
                zona_forzada = " o ".join(zonas_unicas)
                filtros_actuales["zona"] = zona_forzada
                hubo_cambio = True

    except ImportError:
        pass
        
    # 🌟 LOG DE RAYOS X 7: EXTRACCIÓN DE PYTHON Y FILTROS FINALES
    logger.info(f"🐍 [PYTHON EXTRACCIÓN] Forzados -> Op: {filtros_actuales.get('tipo_operacion')} | Tipo: {filtros_actuales.get('tipo_propiedad')} | Ppto: {filtros_actuales.get('presupuesto_max')} | Zona: {filtros_actuales.get('zona')}")
    logger.info(f"🎯 [FILTROS FINALES] {estado.get('filtros')} | Listos para buscar: {criterios_suficientes(estado)}")


    # -------------------------------------------------------------

    if not estado.get("rol"):
        estado["rol"] = "cliente"
        estado["confianza_rol"] = 0.40

    accion = decision.accion.tipo

    if accion == "reiniciar_busqueda":
        estado = reiniciar_busqueda(estado)
        sesiones[sender] = estado

        respuesta = (
            decision.mensaje.strip()
            or (
                "¡Perfecto! Comencemos una nueva búsqueda. "
                "Cuéntame qué propiedad tienes en mente."
            )
        )

    elif accion == "buscar_por_codigo":
        codigo = (
            decision.accion.codigo
            or extraer_codigo_inmueble(mensaje)
        )

        if not codigo:
            estado["esperando_codigo"] = True
            respuesta = (
                decision.mensaje.strip()
                or (
                    "Envíame el código del inmueble o el enlace "
                    "de la publicación para localizarlo."
                )
            )
        else:
            respuesta = (
                await mostrar_inmueble_especifico(
                    estado,
                    codigo,
                )
            )

    elif accion == "pedir_codigo_inmueble":
        estado["esperando_codigo"] = True

        respuesta = (
            decision.mensaje.strip()
            or (
                "¡Claro! Envíame el código que aparece en el "
                "anuncio o el enlace de la publicación y te "
                "muestro la ficha exacta."
            )
        )

    elif accion == "mostrar_mas_propiedades":
        if not estado["propiedades_enviadas"]:
            respuesta = (
                "Todavía no te he mostrado propiedades. "
                "Cuéntame qué tipo de inmueble buscas, la "
                "operación y la zona o presupuesto."
            )
        else:
            respuesta = await mostrar_propiedades(
                estado
            )

    elif accion == "seleccionar_propiedad":
        respuesta = await seleccionar_propiedad(
            estado,
            decision.accion.posicion,
        )

    elif accion == "buscar_propiedades":
        if criterios_suficientes(estado):
            respuesta = await mostrar_propiedades(
                estado
            )
        else:
            respuesta = (
                decision.mensaje.strip()
                or (
                    "Tengo una idea inicial. Para buscar opciones "
                    "relevantes, dime si deseas comprar o alquilar "
                    "y qué zona o presupuesto tienes en mente."
                )
            )

    else:
        # Si Python ya recolectó todos los filtros necesarios, enviamos opciones directo
        if hubo_cambio and criterios_suficientes(estado):
            respuesta = await mostrar_propiedades(estado)
            
        else:
            # 1. Python (El Director) decide qué falta
            pregunta_dinamica = obtener_pregunta_faltante(estado)
            texto_crudo = decision.mensaje.strip() or pregunta_dinamica
            
            # 2. Paty (La Actriz) lo humaniza
            respuesta = await humanizar_texto_con_ia(
                estado=estado, 
                instruccion_cruda=texto_crudo, 
                mensaje_usuario=mensaje
            )

    if (
        estado.get("objetivo")
        == "captura_lead"
    ):
        if lead_completo(estado):
            respuesta = (
                await completar_y_asignar_lead(
                    estado
                )
            )

        elif accion not in {
            "seleccionar_propiedad",
            "buscar_por_codigo",
        }:
            faltantes = datos_lead_faltantes(
                estado
            )

            if not decision.mensaje.strip():
                respuesta = (
                    "Gracias. Para completar la solicitud todavía "
                    "necesito "
                    + ", ".join(faltantes)
                    + "."
                )

    agregar_historial(
        estado,
        "user",
        mensaje,
    )

    agregar_historial(
        estado,
        "assistant",
        respuesta,
    )

    guardar_sesion(sender, estado)
    
    # 🌟 LOG DE RAYOS X 8: ACCIÓN FINAL
    logger.info(f"🏁 [SALIDA] Acción final ejecutada: {accion}")
    return respuesta


# ============================================================
# INICIALIZACIÓN
# ============================================================

async def inicializar_datos() -> None:
    resultados = await asyncio.gather(
        actualizar_inventario(force=True),
        sincronizar_google_sheet(force=True),
        return_exceptions=True,
    )

    for resultado in resultados:
        if isinstance(resultado, Exception):
            logger.error(
                "Error inicialización tipo=%s detalle=%s",
                type(resultado).__name__,
                str(resultado)[:200],
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client

    http_client = httpx.AsyncClient(
        follow_redirects=True,
        trust_env=False,  # 🚀 EL SALVAVIDAS: Ignora proxies basura de Render
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
        ),
        headers={
            "User-Agent": "Mettryc-Chatbot/2.0",
        },
    )

    tarea = asyncio.create_task(
        inicializar_datos()
    )

    yield

    if not tarea.done():
        tarea.cancel()

        try:
            await tarea
        except asyncio.CancelledError:
            pass

    await http_client.aclose()


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Mettryc Realty Paty",
    version="2.0.0",
    lifespan=lifespan,
)


def validar_api_key(
    api_key: Optional[str],
) -> None:
    if not API_KEYS_AGENTES:
        raise HTTPException(
            status_code=503,
            detail=(
                "API_KEYS_AGENTES no está configurado."
            ),
        )

    if not api_key or api_key not in API_KEYS_AGENTES:
        raise HTTPException(
            status_code=403,
            detail="Acceso denegado.",
        )


@app.api_route(
    "/",
    methods=["GET", "HEAD"],
)
async def root():
    return {
        "service": "Mettryc Realty Paty",
        "version": "2.0.0",
        "status": "online",
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "inventario": len(
            inventory_cache["inventario"]
        ),
        "ultima_actualizacion_inventario": (
            inventory_cache[
                "ultima_actualizacion"
            ].isoformat()
            if inventory_cache[
                "ultima_actualizacion"
            ]
            else None
        ),
        "agentes": len(
            sheets_cache["agentes"]
        ),
        "captadores": len(
            sheets_cache["captadores"]
        ),
        "sesiones_memoria": len(sesiones),
        "persistencia": "memoria",
    }


@app.post("/admin/refresh")
async def refresh(
    x_api_key: Optional[str] = Header(
        default=None,
        alias="x-api-key",
    ),
):
    validar_api_key(x_api_key)

    await asyncio.gather(
        actualizar_inventario(force=True),
        sincronizar_google_sheet(force=True),
    )

    return {
        "ok": True,
        "propiedades": len(
            inventory_cache["inventario"]
        ),
        "agentes": len(
            sheets_cache["agentes"]
        ),
        "captadores": len(
            sheets_cache["captadores"]
        ),
    }


@app.post("/admin/reset/{sender}")
async def reset_session(
    sender: str,
    x_api_key: Optional[str] = Header(
        default=None,
        alias="x-api-key",
    ),
):
    validar_api_key(x_api_key)

    sesiones.pop(sender, None)

    return {
        "ok": True,
        "sender": sender,
    }


@app.post("/webhook")
async def webhook(
    request: Request,
    x_api_key: Optional[str] = Header(
        default=None,
        alias="x-api-key",
    ),
):
    validar_api_key(x_api_key)

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="JSON inválido.",
        )

    payload = (
        data.get("query")
        if isinstance(data.get("query"), dict)
        else data
    )

    sender = str(
        payload.get("sender", "")
    ).strip()

    mensaje = str(
        payload.get("message", "")
    ).strip()

    message_id = str(
        payload.get("message_id")
        or payload.get("id")
        or ""
    ).strip()

    if not sender:
        raise HTTPException(
            status_code=422,
            detail="Falta sender.",
        )

    if not mensaje:
        return {"replies": []}

    if not message_id:
        message_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{sender}:{mensaje}",
            )
        )

    if mensaje_es_duplicado(
        sender,
        message_id,
    ):
        return {"replies": []}

    if sender not in locks_usuarios:
        locks_usuarios[sender] = asyncio.Lock()

    if not inventory_cache["inventario"]:
        await actualizar_inventario(force=True)

    elif inventario_necesita_actualizacion():
        asyncio.create_task(
            actualizar_inventario()
        )

    if sheets_necesita_actualizacion():
        asyncio.create_task(
            sincronizar_google_sheet()
        )

    try:
        async with locks_usuarios[sender]:
            respuesta = await procesar_mensaje(
                sender,
                mensaje,
            )

        return {
            "replies": [
                {
                    "message": respuesta.replace(
                        "**",
                        "*",
                    )
                }
            ]
        }

    except Exception as exc:
        logger.exception(
            "Error webhook sender=%s tipo=%s",
            sender[-4:],
            type(exc).__name__,
        )

        return {
            "replies": [
                {
                    "message": (
                        "Disculpa, tuve un inconveniente "
                        "procesando tu mensaje. ¿Puedes "
                        "intentarlo nuevamente?"
                    )
                }
            ]
        }
