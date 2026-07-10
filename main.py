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

try:
    import redis.asyncio as redis_async
except ImportError:
    redis_async = None


# ============================================================
# LOGS
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("mettryc-chatbot")

# Evita mostrar credenciales incluidas en URLs externas.
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
REDIS_URL = os.getenv("REDIS_URL", "")

MODELO_ANALISIS_PRINCIPAL = os.getenv(
    "MODELO_ANALISIS_PRINCIPAL",
    "google/gemini-2.5-flash-lite",
)

MODELO_ANALISIS_RESPALDO = os.getenv(
    "MODELO_ANALISIS_RESPALDO",
    "openai/gpt-4o-mini",
)

OPENROUTER_TIMEOUT = float(os.getenv("OPENROUTER_TIMEOUT", "25"))
WASI_TIMEOUT = float(os.getenv("WASI_TIMEOUT", "40"))
SHEETS_TIMEOUT = float(os.getenv("SHEETS_TIMEOUT", "15"))
TELEGRAM_TIMEOUT = float(os.getenv("TELEGRAM_TIMEOUT", "12"))

SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "604800"))
DUPLICATE_TTL_SECONDS = int(os.getenv("DUPLICATE_TTL_SECONDS", "120"))
LOCK_TIMEOUT_SECONDS = int(os.getenv("LOCK_TIMEOUT_SECONDS", "45"))

INTERVALO_ACTUALIZACION_SHEETS = timedelta(
    minutes=int(os.getenv("INTERVALO_ACTUALIZACION_SHEETS_MINUTOS", "60"))
)

HORARIOS_ACTUALIZACION_INVENTARIO = [0, 12]

API_KEYS_AGENTES = {
    valor.strip()
    for valor in os.getenv("API_KEYS_AGENTES", "").split(",")
    if valor.strip()
}

TELEGRAM_ADMIN_IDS = [
    valor.strip()
    for valor in os.getenv(
        "TELEGRAM_ADMIN_IDS",
        os.getenv("TELEGRAM_ADMIN_ID", ""),
    ).split(",")
    if valor.strip()
]

SENDER_ES_WHATSAPP = (
    os.getenv("SENDER_ES_WHATSAPP", "true").strip().lower() == "true"
)

MAX_PROPIEDADES_POR_LOTE = int(
    os.getenv("MAX_PROPIEDADES_POR_LOTE", "3")
)

MAX_EXCESO_PRESUPUESTO_APROXIMADO = float(
    os.getenv("MAX_EXCESO_PRESUPUESTO_APROXIMADO", "0.20")
)


# ============================================================
# CONSTANTES DE CONVERSACIÓN
# ============================================================

ESTADO_DIAGNOSTICO = "diagnostico"
ESTADO_RESULTADOS = "resultados"
ESTADO_CAPTURA_LEAD = "captura_lead"
ESTADO_ASIGNADO = "asignado"

CAMPOS_DIAGNOSTICO = [
    "tipo_operacion",
    "tipo_propiedad",
    "zona",
    "presupuesto_max",
    "habitaciones_min",
    "banos_min",
    "caracteristicas",
]

PREGUNTAS_DIAGNOSTICO = {
    "tipo_operacion": "¿La propiedad la buscas para comprar o alquilar?",
    "tipo_propiedad": (
        "¿Qué tipo de propiedad estás buscando? "
        "Por ejemplo: apartamento, casa o townhouse."
    ),
    "zona": "¿En qué zona, urbanización o ciudad te gustaría buscar?",
    "presupuesto_max": "¿Cuál es tu presupuesto máximo aproximado?",
    "habitaciones_min": (
        "¿Cuántas habitaciones necesitas como mínimo? "
        "También puedes decirme que no tienes preferencia."
    ),
    "banos_min": (
        "¿Cuántos baños necesitas como mínimo? "
        "También puedes decirme que no tienes preferencia."
    ),
    "caracteristicas": (
        "¿Hay alguna característica importante? Por ejemplo: "
        "vigilancia, planta eléctrica, piscina o que esté amoblada. "
        "También puedes decirme que no tienes preferencia."
    ),
}

PREGUNTAS_LEAD = {
    "nombre": (
        "¡Excelente! Para asignarte un asesor y ayudarte con esta propiedad, "
        "¿cuál es tu nombre completo?"
    ),
    "correo": "Gracias. ¿Cuál es tu correo electrónico?",
    "whatsapp": (
        "Por último, ¿cuál es tu número de WhatsApp con código de país?"
    ),
}

STOPWORDS_ZONA = {
    "el",
    "la",
    "los",
    "las",
    "de",
    "del",
    "en",
    "y",
    "urb",
    "urbanizacion",
    "sector",
    "zona",
    "ciudad",
    "estado",
    "venezuela",
}

TOKENS_DIRECCION_ZONA = {
    "norte",
    "sur",
    "este",
    "oeste",
    "centro",
}

SINONIMOS_TIPO = {
    "tohouse": "townhouse",
    "towhouse": "townhouse",
    "twhouse": "townhouse",
    "town house": "townhouse",
    "aparto quinta": "apartoquinta",
    "aptoquinta": "apartoquinta",
    "apartoquita": "apartoquinta",
    "apto": "apartamento",
    "apart": "apartamento",
}

PATRONES_COLEGA = [
    r"\bsoy\s+(asesor|agente|corredor|broker|realtor)\b",
    r"\bsoy\s+colega\b",
    r"\btrabajo\s+(en|con)\s+(una\s+)?inmobiliaria\b",
    r"\btengo\s+un\s+cliente\b",
    r"\bbusco\s+para\s+un\s+cliente\b",
    r"\bcompartimos\s+comision\b",
    r"\bcomparto\s+comision\b",
]

PATRONES_CLIENTE_EXPLICITO = [
    r"\bno\s+soy\s+(asesor|agente|corredor|broker|realtor)\b",
    r"\bsoy\s+cliente\b",
    r"\bbusco\s+para\s+mi\b",
    r"\bes\s+para\s+mi\b",
]


# ============================================================
# MODELOS PYDANTIC
# ============================================================

class FiltrosExtraidos(BaseModel):
    tipo_operacion: Optional[Literal["venta", "alquiler"]] = None
    tipo_propiedad: Optional[str] = None
    zona: Optional[str] = None
    presupuesto_max: Optional[float] = None
    habitaciones_min: Optional[int] = None
    banos_min: Optional[int] = None
    caracteristicas: List[str] = Field(default_factory=list)


class LeadExtraido(BaseModel):
    nombre: Optional[str] = None
    correo: Optional[str] = None
    whatsapp: Optional[str] = None


class AnalisisMensaje(BaseModel):
    intencion: Literal[
        "saludo",
        "buscar",
        "mas_opciones",
        "ajustar_busqueda",
        "interes_propiedad",
        "enviar_datos",
        "nueva_busqueda",
        "consulta_inmueble_especifico",
        "agradecimiento",
        "faq",
        "otro",
    ] = "otro"

    rol: Optional[Literal["cliente", "colega_inmobiliario"]] = None
    confianza_rol: float = 0.0

    codigo_inmueble: Optional[str] = None
    referencia_posicion: Optional[int] = None

    filtros: FiltrosExtraidos = Field(default_factory=FiltrosExtraidos)
    campos_sin_preferencia: List[str] = Field(default_factory=list)
    lead: LeadExtraido = Field(default_factory=LeadExtraido)


# ============================================================
# CACHÉS LOCALES
# ============================================================

inventory_cache: Dict[str, Any] = {
    "inventario": [],
    "ultima_actualizacion": None,
    "proxima_actualizacion": None,
}

sheets_cache: Dict[str, Any] = {
    "agentes": [],
    "captadores": {},
    "ultima_actualizacion": None,
}

inventory_refresh_lock = asyncio.Lock()
sheets_refresh_lock = asyncio.Lock()

memory_sessions: Dict[str, dict] = {}
memory_session_locks: Dict[str, asyncio.Lock] = {}
memory_duplicates: Dict[str, float] = {}
memory_round_robin_index = -1

redis_client = None
http_client: Optional[httpx.AsyncClient] = None


# ============================================================
# UTILIDADES GENERALES
# ============================================================

def normalizar_texto(texto: Any) -> str:
    if texto is None:
        return ""

    resultado = str(texto).lower().strip()
    resultado = unicodedata.normalize("NFD", resultado)
    resultado = "".join(
        caracter
        for caracter in resultado
        if unicodedata.category(caracter) != "Mn"
    )
    resultado = re.sub(r"[^a-z0-9\s@.+-]", " ", resultado)
    return re.sub(r"\s+", " ", resultado).strip()


def capitalizar_nombre(nombre: str) -> str:
    palabras = [
        palabra
        for palabra in re.split(r"\s+", str(nombre or "").strip())
        if palabra
    ]

    return " ".join(
        palabra[:1].upper() + palabra[1:].lower()
        for palabra in palabras
    )


def limpiar_telefono(valor: Any) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def convertir_entero_seguro(valor: Any) -> int:
    try:
        if valor in [None, "", "N/D"]:
            return 0
        return int(float(valor))
    except (TypeError, ValueError):
        return 0


def convertir_float_seguro(valor: Any) -> float:
    try:
        if valor in [None, "", "N/D"]:
            return 0.0
        return float(valor)
    except (TypeError, ValueError):
        return 0.0


def formato_moneda(valor: Any) -> str:
    numero = convertir_float_seguro(valor)

    if numero <= 0:
        return "N/D"

    return f"${numero:,.0f}".replace(",", ".")


def normalizar_tipo_propiedad(tipo: Any) -> str:
    tipo_normalizado = normalizar_texto(tipo)

    if tipo_normalizado in SINONIMOS_TIPO:
        return SINONIMOS_TIPO[tipo_normalizado]

    if "townhouse" in tipo_normalizado:
        return "townhouse"

    if any(
        valor in tipo_normalizado
        for valor in ["apartoquinta", "aparto quinta", "apartoquita"]
    ):
        return "apartoquinta"

    if "penthouse" in tipo_normalizado:
        return "penthouse"

    if "apartamento" in tipo_normalizado or tipo_normalizado == "apto":
        return "apartamento"

    if "casa" in tipo_normalizado:
        return "casa"

    return tipo_normalizado


def normalizar_operacion(operacion: Any) -> str:
    valor = normalizar_texto(operacion)

    if valor in {
        "venta",
        "comprar",
        "compra",
        "para comprar",
        "adquirir",
    }:
        return "venta"

    if valor in {
        "alquiler",
        "alquilar",
        "arrendar",
        "arrendamiento",
        "renta",
    }:
        return "alquiler"

    return ""


def parsear_presupuesto_texto(texto: str) -> Optional[float]:
    if not texto:
        return None

    normalizado = normalizar_texto(texto)

    parece_telefono = bool(
        re.search(r"\+?\d[\d\s\-()]{9,}\d", texto)
    )

    contiene_contexto_precio = any(
        palabra in normalizado
        for palabra in [
            "presupuesto",
            "maximo",
            "hasta",
            "usd",
            "dolar",
            "dolares",
            "$",
            "mil",
            "millon",
            "millones",
            "k",
            "mm",
        ]
    )

    if parece_telefono and not contiene_contexto_precio:
        return None

    coincidencia = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(mil|k)\b",
        normalizado,
    )

    if coincidencia:
        return (
            float(coincidencia.group(1).replace(",", "."))
            * 1000
        )

    coincidencia = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(millon|millones|mm)\b",
        normalizado,
    )

    if coincidencia:
        return (
            float(coincidencia.group(1).replace(",", "."))
            * 1_000_000
        )

    candidatos = re.findall(
        r"(?:usd|us|dolares|dolar|\$)?\s*([0-9][0-9.,]{3,})",
        normalizado,
    )

    for candidato in candidatos:
        limpio = candidato.strip()

        if "." in limpio and "," in limpio:
            if limpio.rfind(",") > limpio.rfind("."):
                limpio = limpio.replace(".", "").replace(",", ".")
            else:
                limpio = limpio.replace(",", "")
        elif "." in limpio and len(limpio.split(".")[-1]) == 3:
            limpio = limpio.replace(".", "")
        elif "," in limpio:
            if len(limpio.split(",")[-1]) == 3:
                limpio = limpio.replace(",", "")
            else:
                limpio = limpio.replace(",", ".")

        try:
            valor = float(limpio)
            if 1000 <= valor <= 20_000_000:
                return valor
        except ValueError:
            continue

    return None


def parsear_precio_wasi(
    valor_numerico: Any = None,
    valor_label: Any = None,
) -> float:
    numerico = convertir_float_seguro(valor_numerico)

    if numerico > 0:
        return numerico

    if valor_label in [None, "", "N/D"]:
        return 0.0

    texto = re.sub(r"[^\d.,]", "", str(valor_label))

    if not texto:
        return 0.0

    if "." in texto and "," in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "." in texto and len(texto.split(".")[-1]) == 3:
        texto = texto.replace(".", "")
    elif "," in texto:
        if len(texto.split(",")[-1]) == 3:
            texto = texto.replace(",", "")
        else:
            texto = texto.replace(",", ".")

    return convertir_float_seguro(texto)


def extraer_correo(texto: str) -> Optional[str]:
    coincidencia = re.search(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        texto or "",
        re.IGNORECASE,
    )

    return coincidencia.group(0).lower() if coincidencia else None


def correo_valido(correo: str) -> bool:
    return bool(
        re.fullmatch(
            r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
            correo or "",
            re.IGNORECASE,
        )
    )


def normalizar_telefono_venezolano(valor: Any) -> Optional[str]:
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

    return normalizar_telefono_venezolano(coincidencia.group(1))


def es_nombre_persona_valido(nombre: str) -> bool:
    if not nombre:
        return False

    palabras = re.findall(
        r"[A-Za-zÀ-ÖØ-ÿ'´-]+",
        nombre,
    )

    bloqueadas = {
        "hola",
        "buenas",
        "quiero",
        "busco",
        "interesa",
        "primera",
        "segunda",
        "tercera",
        "ultima",
        "opcion",
        "apartamento",
        "casa",
        "townhouse",
    }

    limpias = [
        palabra
        for palabra in palabras
        if normalizar_texto(palabra) not in bloqueadas
        and len(palabra) >= 2
    ]

    return 2 <= len(limpias) <= 5


def extraer_entero_mensaje(
    texto: str,
    palabras_clave: List[str],
) -> Optional[int]:
    normalizado = normalizar_texto(texto)

    for palabra in palabras_clave:
        patrones = [
            rf"(\d+)\s*{re.escape(palabra)}",
            rf"{re.escape(palabra)}\s*(?:minimo|minima|de)?\s*(\d+)",
        ]

        for patron in patrones:
            coincidencia = re.search(patron, normalizado)
            if coincidencia:
                return int(coincidencia.group(1))

    return None


def tokens_relevantes(texto: Any) -> Set[str]:
    return {
        token
        for token in normalizar_texto(texto).split()
        if token not in STOPWORDS_ZONA and len(token) > 1
    }


def zona_coincide(
    zona_buscada: str,
    zona_propiedad: str,
    ciudad_propiedad: str = "",
) -> bool:
    if not zona_buscada:
        return True

    tokens_busqueda = tokens_relevantes(zona_buscada)
    tokens_propiedad = tokens_relevantes(
        f"{zona_propiedad} {ciudad_propiedad}"
    )

    if not tokens_busqueda:
        return True

    if not tokens_propiedad:
        return False

    direcciones = tokens_busqueda.intersection(
        TOKENS_DIRECCION_ZONA
    )

    if direcciones and not direcciones.issubset(tokens_propiedad):
        return False

    interseccion = tokens_busqueda.intersection(tokens_propiedad)
    proporcion = len(interseccion) / len(tokens_busqueda)

    return proporcion >= 0.6


def detectar_rol_determinista(
    mensaje: str,
) -> Optional[str]:
    texto = normalizar_texto(mensaje)

    if any(
        re.search(patron, texto)
        for patron in PATRONES_CLIENTE_EXPLICITO
    ):
        return "cliente"

    if any(
        re.search(patron, texto)
        for patron in PATRONES_COLEGA
    ):
        return "colega_inmobiliario"

    return None


def detectar_mas_opciones(texto: str) -> bool:
    normalizado = normalizar_texto(texto)

    frases = [
        "mas opciones",
        "otras opciones",
        "otras tres",
        "siguientes tres",
        "ninguna me interesa",
        "ninguna me gusto",
        "muestrame otras",
        "enviame otras",
        "quiero ver mas",
        "otra tanda",
    ]

    return any(frase in normalizado for frase in frases)


def detectar_nueva_busqueda(texto: str) -> bool:
    normalizado = normalizar_texto(texto)

    return any(
        frase in normalizado
        for frase in [
            "nueva busqueda",
            "empezar de nuevo",
            "otra busqueda",
            "reiniciar busqueda",
            "busco otra propiedad",
        ]
    )


def detectar_posicion_propiedad(texto: str) -> Optional[int]:
    normalizado = normalizar_texto(texto)

    patrones = {
        1: ["la primera", "primera opcion", "opcion 1", "numero 1"],
        2: ["la segunda", "segunda opcion", "opcion 2", "numero 2"],
        3: [
            "la tercera",
            "tercera opcion",
            "opcion 3",
            "numero 3",
            "la ultima",
        ],
    }

    for posicion, frases in patrones.items():
        if any(frase in normalizado for frase in frases):
            return posicion

    return None


def extraer_codigo_inmueble(texto: str) -> Optional[str]:
    patrones = [
        r"mettryc\.com/inmueble/(\d+)",
        r"\b(?:codigo|cod|inmueble)\s*[:#-]?\s*(\d{3,})\b",
        r"\bALM-?(\d+)\b",
    ]

    for patron in patrones:
        coincidencia = re.search(
            patron,
            texto or "",
            re.IGNORECASE,
        )

        if coincidencia:
            return coincidencia.group(1)

    return None


# ============================================================
# ESTADO DE CONVERSACIÓN
# ============================================================

def crear_estado(sender: str) -> dict:
    whatsapp = None

    if SENDER_ES_WHATSAPP:
        whatsapp = normalizar_telefono_venezolano(sender)

    return {
        "version": 1,
        "estado": ESTADO_DIAGNOSTICO,
        "rol": None,
        "saludado": False,
        "campo_esperado": None,
        "filtros": {
            "tipo_operacion": None,
            "tipo_propiedad": None,
            "zona": None,
            "presupuesto_max": None,
            "habitaciones_min": None,
            "banos_min": None,
            "caracteristicas": [],
        },
        "sin_preferencia": [],
        "propiedades_enviadas": [],
        "ultimo_lote_propiedades": [],
        "propiedad_interes": None,
        "lead": {
            "nombre": None,
            "correo": None,
            "whatsapp": whatsapp,
        },
        "lead_id": None,
        "agente_asignado": None,
        "notificacion_enviada": False,
        "ultima_intencion": None,
        "ultimo_mensaje_id": None,
        "creado_en": datetime.utcnow().isoformat(),
        "actualizado_en": datetime.utcnow().isoformat(),
    }


def reiniciar_busqueda(estado: dict) -> dict:
    rol = estado.get("rol")
    lead = deepcopy(estado.get("lead", {}))
    saludado = estado.get("saludado", True)

    nuevo = crear_estado(
        lead.get("whatsapp") or ""
    )

    nuevo["rol"] = rol
    nuevo["lead"] = lead
    nuevo["saludado"] = saludado

    return nuevo


class SessionStore:
    async def get(self, sender: str) -> dict:
        if redis_client:
            contenido = await redis_client.get(
                f"mettryc:session:{sender}"
            )

            if contenido:
                try:
                    return json.loads(contenido)
                except json.JSONDecodeError:
                    logger.warning(
                        "Sesión Redis inválida para sender=%s",
                        sender[-4:],
                    )

        if sender not in memory_sessions:
            memory_sessions[sender] = crear_estado(sender)

        return deepcopy(memory_sessions[sender])

    async def set(self, sender: str, estado: dict) -> None:
        estado["actualizado_en"] = datetime.utcnow().isoformat()

        if redis_client:
            await redis_client.set(
                f"mettryc:session:{sender}",
                json.dumps(estado, ensure_ascii=False),
                ex=SESSION_TTL_SECONDS,
            )
        else:
            memory_sessions[sender] = deepcopy(estado)

    async def is_duplicate(
        self,
        sender: str,
        message_id: str,
    ) -> bool:
        clave = f"mettryc:duplicate:{sender}:{message_id}"

        if redis_client:
            creado = await redis_client.set(
                clave,
                "1",
                nx=True,
                ex=DUPLICATE_TTL_SECONDS,
            )
            return not bool(creado)

        ahora = time.time()

        expiradas = [
            key
            for key, expiracion in memory_duplicates.items()
            if expiracion <= ahora
        ]

        for key in expiradas:
            memory_duplicates.pop(key, None)

        if clave in memory_duplicates:
            return True

        memory_duplicates[clave] = ahora + DUPLICATE_TTL_SECONDS
        return False

    async def next_round_robin(self, total: int) -> int:
        global memory_round_robin_index

        if total <= 0:
            return -1

        if redis_client:
            indice = await redis_client.incr(
                "mettryc:round_robin"
            )
            return (indice - 1) % total

        memory_round_robin_index = (
            memory_round_robin_index + 1
        ) % total

        return memory_round_robin_index


session_store = SessionStore()


@asynccontextmanager
async def user_lock(sender: str):
    if redis_client:
        lock = redis_client.lock(
            f"mettryc:lock:{sender}",
            timeout=LOCK_TIMEOUT_SECONDS,
            blocking_timeout=10,
        )

        adquirido = await lock.acquire()

        if not adquirido:
            raise HTTPException(
                status_code=409,
                detail="Conversación ocupada, intenta nuevamente.",
            )

        try:
            yield
        finally:
            try:
                await lock.release()
            except Exception:
                logger.warning(
                    "No se pudo liberar lock Redis sender=%s",
                    sender[-4:],
                )
    else:
        if sender not in memory_session_locks:
            memory_session_locks[sender] = asyncio.Lock()

        async with memory_session_locks[sender]:
            yield


# ============================================================
# WASI
# ============================================================

def calcular_proxima_actualizacion(
    ahora: Optional[datetime] = None,
) -> datetime:
    ahora = ahora or datetime.now()

    for hora in sorted(HORARIOS_ACTUALIZACION_INVENTARIO):
        candidato = ahora.replace(
            hour=hora,
            minute=0,
            second=0,
            microsecond=0,
        )

        if candidato > ahora:
            return candidato

    return (
        ahora + timedelta(days=1)
    ).replace(
        hour=HORARIOS_ACTUALIZACION_INVENTARIO[0],
        minute=0,
        second=0,
        microsecond=0,
    )


def necesita_actualizar_inventario() -> bool:
    if not inventory_cache["inventario"]:
        return True

    proxima = inventory_cache.get("proxima_actualizacion")

    if not proxima:
        return True

    return datetime.now() >= proxima


def convertir_caracteristicas_wasi(valor: Any) -> str:
    if isinstance(valor, str):
        return valor

    if isinstance(valor, list):
        return " ".join(str(item) for item in valor)

    if isinstance(valor, dict):
        partes = []

        for clave, contenido in valor.items():
            if contenido not in [None, "", False, 0, "0"]:
                partes.append(f"{clave} {contenido}")

        return " ".join(partes)

    return ""


async def obtener_inventario_desde_wasi() -> List[dict]:
    if not WASI_TOKEN or not WASI_COMPANY_ID:
        logger.error(
            "Faltan WASI_TOKEN o WASI_COMPANY_ID."
        )
        return []

    propiedades: List[dict] = []
    take = 100
    skip = 0
    max_paginas = 100

    for pagina in range(max_paginas):
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
                    "Error Wasi página=%s intento=%s tipo=%s",
                    pagina,
                    intento + 1,
                    type(exc).__name__,
                )
                await asyncio.sleep(2 ** intento)

        if not isinstance(data, dict):
            break

        contador = 0

        for clave, valor in data.items():
            if not (
                isinstance(valor, dict)
                and str(clave).isdigit()
            ):
                continue

            contador += 1

            id_propiedad = valor.get("id_property")

            if not id_propiedad:
                continue

            usuario = valor.get("user_data") or {}

            descripcion = (
                valor.get("description")
                or valor.get("observations")
                or ""
            )

            caracteristicas = " ".join([
                convertir_caracteristicas_wasi(
                    valor.get("features")
                ),
                convertir_caracteristicas_wasi(
                    valor.get("internal_features")
                ),
                convertir_caracteristicas_wasi(
                    valor.get("external_features")
                ),
            ]).strip()

            propiedades.append({
                "id": str(id_propiedad),
                "titulo": valor.get(
                    "title",
                    "Propiedad sin título",
                ),
                "descripcion": descripcion,
                "ciudad": valor.get("city_label", "N/D"),
                "zona": valor.get("zone_label", "N/D"),
                "venta": valor.get(
                    "sale_price_label",
                    "N/D",
                ),
                "renta": valor.get(
                    "rent_price_label",
                    "N/D",
                ),
                "precio_venta_float": parsear_precio_wasi(
                    valor.get("sale_price"),
                    valor.get("sale_price_label"),
                ),
                "precio_renta_float": parsear_precio_wasi(
                    valor.get("rent_price"),
                    valor.get("rent_price_label"),
                ),
                "area": valor.get("area", "N/D"),
                "habitaciones": valor.get("bedrooms", "N/D"),
                "banos": valor.get("bathrooms", "N/D"),
                "garajes": valor.get("garages", "N/D"),
                "tipo_propiedad_wasi": valor.get(
                    "type_label",
                    "Indefinido",
                ),
                "caracteristicas_texto": caracteristicas,
                "enlace": (
                    "https://www.mettryc.com/inmueble/"
                    f"{id_propiedad}"
                ),
                "captador_propiedad": (
                    f"{usuario.get('first_name', '')} "
                    f"{usuario.get('last_name', '')}"
                ).strip() or "Asesor Mettryc",
                "telefono_captador_wasi": usuario.get(
                    "phone",
                    "",
                ),
            })

        if contador < take:
            break

        skip += take
        await asyncio.sleep(0.4)

    logger.info(
        "Inventario Wasi cargado propiedades=%s",
        len(propiedades),
    )

    return propiedades


async def actualizar_inventario(
    force: bool = False,
) -> bool:
    if not force and not necesita_actualizar_inventario():
        return False

    async with inventory_refresh_lock:
        if not force and not necesita_actualizar_inventario():
            return False

        propiedades = await obtener_inventario_desde_wasi()

        if not propiedades:
            logger.error(
                "Wasi no devolvió propiedades. "
                "Se conserva el inventario anterior."
            )
            return False

        ahora = datetime.now()

        inventory_cache["inventario"] = propiedades
        inventory_cache["ultima_actualizacion"] = ahora
        inventory_cache["proxima_actualizacion"] = (
            calcular_proxima_actualizacion(
                ahora + timedelta(seconds=1)
            )
        )

        return True


# ============================================================
# GOOGLE SHEETS
# ============================================================

async def sincronizar_google_sheet(
    force: bool = False,
) -> None:
    if not GOOGLE_SHEET_TURNOS_URL:
        return

    ultima = sheets_cache.get("ultima_actualizacion")

    if (
        not force
        and ultima
        and datetime.now() - ultima
        <= INTERVALO_ACTUALIZACION_SHEETS
        and sheets_cache["agentes"]
    ):
        return

    async with sheets_refresh_lock:
        ultima = sheets_cache.get("ultima_actualizacion")

        if (
            not force
            and ultima
            and datetime.now() - ultima
            <= INTERVALO_ACTUALIZACION_SHEETS
            and sheets_cache["agentes"]
        ):
            return

        try:
            respuesta = await http_client.get(
                GOOGLE_SHEET_TURNOS_URL,
                timeout=SHEETS_TIMEOUT,
            )
            respuesta.raise_for_status()
            payload = respuesta.json()

            if not isinstance(payload, dict):
                raise ValueError(
                    "Google Sheets no devolvió un objeto."
                )

            agentes = payload.get("agentes", [])

            if not isinstance(agentes, list):
                agentes = []

            captadores_payload = payload.get(
                "captadores",
                {},
            )

            captadores: Dict[str, str] = {}

            if isinstance(captadores_payload, dict):
                for nombre, telefono in captadores_payload.items():
                    if nombre and telefono:
                        captadores[str(nombre).strip()] = str(
                            telefono
                        ).strip()

            elif isinstance(captadores_payload, list):
                for registro in captadores_payload:
                    if not isinstance(registro, dict):
                        continue

                    nombre = (
                        registro.get("nombre")
                        or registro.get("name")
                    )
                    telefono = (
                        registro.get("telefono")
                        or registro.get("phone")
                    )

                    if nombre and telefono:
                        captadores[str(nombre).strip()] = str(
                            telefono
                        ).strip()

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

        except Exception as exc:
            logger.error(
                "Error sincronizando Sheets tipo=%s detalle=%s",
                type(exc).__name__,
                str(exc)[:200],
            )


async def asignar_agente_round_robin() -> Optional[dict]:
    await sincronizar_google_sheet()

    agentes = [
        agente
        for agente in sheets_cache["agentes"]
        if isinstance(agente, dict)
        and agente.get("nombre")
    ]

    if not agentes:
        return None

    indice = await session_store.next_round_robin(
        len(agentes)
    )

    if indice < 0:
        return None

    return deepcopy(agentes[indice])


def obtener_telefono_captador(
    nombre_captador: str,
) -> str:
    if not nombre_captador:
        return "N/D"

    captadores = sheets_cache.get("captadores", {})
    nombre_normalizado = normalizar_texto(
        nombre_captador
    )

    for nombre, telefono in captadores.items():
        if normalizar_texto(nombre) == nombre_normalizado:
            return telefono

    tokens_wasi = tokens_relevantes(nombre_captador)
    mejor_score = 0
    mejor_telefono = "N/D"

    for nombre, telefono in captadores.items():
        score = len(
            tokens_wasi.intersection(
                tokens_relevantes(nombre)
            )
        )

        if score > mejor_score:
            mejor_score = score
            mejor_telefono = telefono

    return mejor_telefono if mejor_score >= 2 else "N/D"


# ============================================================
# OPENROUTER: ANALIZADOR ESTRUCTURADO
# ============================================================

def analisis_fallback(mensaje: str) -> AnalisisMensaje:
    texto = normalizar_texto(mensaje)

    intencion = "otro"

    if detectar_nueva_busqueda(texto):
        intencion = "nueva_busqueda"
    elif detectar_mas_opciones(texto):
        intencion = "mas_opciones"
    elif extraer_codigo_inmueble(mensaje):
        intencion = "consulta_inmueble_especifico"
    elif detectar_posicion_propiedad(texto):
        intencion = "interes_propiedad"
    elif any(
        palabra in texto
        for palabra in ["me interesa", "quiero verla", "agendar visita"]
    ):
        intencion = "interes_propiedad"
    elif any(
        palabra in texto
        for palabra in ["hola", "buenas", "buen dia"]
    ):
        intencion = "saludo"
    elif "gracias" in texto:
        intencion = "agradecimiento"
    elif any(
        palabra in texto
        for palabra in [
            "busco",
            "comprar",
            "alquilar",
            "apartamento",
            "casa",
            "townhouse",
        ]
    ):
        intencion = "buscar"

    filtros = FiltrosExtraidos()

    if any(
        palabra in texto
        for palabra in ["comprar", "compra", "venta"]
    ):
        filtros.tipo_operacion = "venta"

    if any(
        palabra in texto
        for palabra in ["alquilar", "alquiler", "renta"]
    ):
        filtros.tipo_operacion = "alquiler"

    for tipo in [
        "townhouse",
        "apartamento",
        "casa",
        "penthouse",
        "apartoquinta",
    ]:
        if tipo in normalizar_tipo_propiedad(texto):
            filtros.tipo_propiedad = tipo
            break

    filtros.presupuesto_max = parsear_presupuesto_texto(
        mensaje
    )

    filtros.habitaciones_min = extraer_entero_mensaje(
        mensaje,
        ["habitaciones", "habitacion", "habs", "cuartos"],
    )

    filtros.banos_min = extraer_entero_mensaje(
        mensaje,
        ["banos", "bano"],
    )

    return AnalisisMensaje(
        intencion=intencion,
        rol=detectar_rol_determinista(mensaje),
        confianza_rol=1.0
        if detectar_rol_determinista(mensaje)
        else 0.0,
        codigo_inmueble=extraer_codigo_inmueble(mensaje),
        referencia_posicion=detectar_posicion_propiedad(
            mensaje
        ),
        filtros=filtros,
        lead=LeadExtraido(
            correo=extraer_correo(mensaje),
            whatsapp=extraer_telefono(mensaje),
        ),
    )


async def analizar_mensaje_ia(
    mensaje: str,
    estado: dict,
) -> AnalisisMensaje:
    fallback = analisis_fallback(mensaje)

    if not OPENROUTER_API_KEY:
        return fallback

    esquema_estado = {
        "estado": estado.get("estado"),
        "rol_actual": estado.get("rol"),
        "campo_esperado": estado.get("campo_esperado"),
        "filtros_actuales": estado.get("filtros"),
        "sin_preferencia": estado.get("sin_preferencia"),
    }

    system_prompt = """
Eres el analizador de un chatbot inmobiliario venezolano.

Responde únicamente usando el esquema JSON solicitado.

Debes interpretar el mensaje actual, sin inventar datos.

Reglas:
1. "mas_opciones" aplica cuando el usuario pide otras propiedades o dice que ninguna le gustó.
2. "ajustar_busqueda" aplica cuando modifica zona, presupuesto, tipo, habitaciones, baños o características.
3. "interes_propiedad" aplica cuando elige una propiedad o quiere visita/información.
4. referencia_posicion debe ser 1, 2 o 3 cuando diga primera, segunda, tercera o última.
5. "nueva_busqueda" aplica cuando quiere comenzar otra búsqueda.
6. Un colega inmobiliario se identifica explícitamente como asesor, agente, broker, realtor, corredor o dice que busca para un cliente.
7. No clasifiques como colega a alguien que solamente pide hablar con un asesor.
8. Extrae todos los filtros presentes en el mensaje.
9. Si dice "no importa", "cualquiera", "me da igual" o "sin preferencia", agrega el campo correspondiente a campos_sin_preferencia.
10. Extrae nombre solo cuando el usuario lo proporciona explícitamente o cuando el campo esperado es nombre.
11. No confundas teléfono con presupuesto.
12. No inventes zona, nombre, correo, teléfono ni código.
"""

    user_prompt = (
        f"Estado actual:\n"
        f"{json.dumps(esquema_estado, ensure_ascii=False)}\n\n"
        f"Mensaje actual:\n{mensaje}"
    )

    modelos = [
        MODELO_ANALISIS_PRINCIPAL,
        MODELO_ANALISIS_RESPALDO,
    ]

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://www.mettryc.com",
        "X-Title": "Mettryc Realty Chatbot",
    }

    for modelo in modelos:
        payload = {
            "model": modelo,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            "temperature": 0,
            "max_tokens": 500,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "analisis_mensaje",
                    "strict": True,
                    "schema": AnalisisMensaje.model_json_schema(),
                },
            },
        }

        try:
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
                    item.get("text", "")
                    for item in contenido
                    if isinstance(item, dict)
                )

            analisis = AnalisisMensaje.model_validate_json(
                contenido
            )

            return combinar_analisis_con_fallback(
                analisis,
                fallback,
            )

        except (ValidationError, ValueError) as exc:
            logger.warning(
                "JSON inválido OpenRouter modelo=%s tipo=%s",
                modelo,
                type(exc).__name__,
            )
        except Exception as exc:
            logger.warning(
                "Error OpenRouter modelo=%s tipo=%s detalle=%s",
                modelo,
                type(exc).__name__,
                str(exc)[:160],
            )

    return fallback


def combinar_analisis_con_fallback(
    analisis: AnalisisMensaje,
    fallback: AnalisisMensaje,
) -> AnalisisMensaje:
    if fallback.codigo_inmueble and not analisis.codigo_inmueble:
        analisis.codigo_inmueble = fallback.codigo_inmueble

    if (
        fallback.referencia_posicion
        and not analisis.referencia_posicion
    ):
        analisis.referencia_posicion = (
            fallback.referencia_posicion
        )

    if fallback.rol and (
        not analisis.rol
        or fallback.confianza_rol > analisis.confianza_rol
    ):
        analisis.rol = fallback.rol
        analisis.confianza_rol = fallback.confianza_rol

    if (
        fallback.filtros.presupuesto_max
        and not analisis.filtros.presupuesto_max
    ):
        analisis.filtros.presupuesto_max = (
            fallback.filtros.presupuesto_max
        )

    if (
        fallback.filtros.habitaciones_min is not None
        and analisis.filtros.habitaciones_min is None
    ):
        analisis.filtros.habitaciones_min = (
            fallback.filtros.habitaciones_min
        )

    if (
        fallback.filtros.banos_min is not None
        and analisis.filtros.banos_min is None
    ):
        analisis.filtros.banos_min = (
            fallback.filtros.banos_min
        )

    if fallback.lead.correo and not analisis.lead.correo:
        analisis.lead.correo = fallback.lead.correo

    if (
        fallback.lead.whatsapp
        and not analisis.lead.whatsapp
    ):
        analisis.lead.whatsapp = fallback.lead.whatsapp

    if fallback.intencion in {
        "mas_opciones",
        "nueva_busqueda",
        "consulta_inmueble_especifico",
    }:
        analisis.intencion = fallback.intencion

    return analisis


# ============================================================
# FUSIÓN DE DATOS
# ============================================================

def obtener_filtros_analisis(
    analisis: AnalisisMensaje,
) -> dict:
    filtros = analisis.filtros.model_dump()

    if filtros.get("tipo_operacion"):
        filtros["tipo_operacion"] = normalizar_operacion(
            filtros["tipo_operacion"]
        )

    if filtros.get("tipo_propiedad"):
        filtros["tipo_propiedad"] = (
            normalizar_tipo_propiedad(
                filtros["tipo_propiedad"]
            )
        )

    if filtros.get("zona"):
        filtros["zona"] = str(
            filtros["zona"]
        ).strip()

    filtros["caracteristicas"] = [
        normalizar_texto(valor)
        for valor in filtros.get("caracteristicas", [])
        if normalizar_texto(valor)
    ]

    return filtros


def aplicar_filtros(
    estado: dict,
    analisis: AnalisisMensaje,
) -> bool:
    actuales = estado["filtros"]
    nuevos = obtener_filtros_analisis(analisis)
    hubo_cambio = False

    permitir_sobrescritura = (
        analisis.intencion == "ajustar_busqueda"
        or estado.get("estado") == ESTADO_RESULTADOS
    )

    for campo, valor in nuevos.items():
        if valor in [None, "", []]:
            continue

        anterior = actuales.get(campo)

        if (
            anterior in [None, "", []]
            or permitir_sobrescritura
        ):
            if anterior != valor:
                actuales[campo] = valor
                hubo_cambio = True

    sin_preferencia = set(
        estado.get("sin_preferencia", [])
    )

    for campo in analisis.campos_sin_preferencia:
        if campo not in CAMPOS_DIAGNOSTICO:
            continue

        sin_preferencia.add(campo)

        if campo == "caracteristicas":
            actuales[campo] = []
        else:
            actuales[campo] = None

        hubo_cambio = True

    estado["sin_preferencia"] = list(sin_preferencia)

    if hubo_cambio and estado.get("estado") == ESTADO_RESULTADOS:
        estado["propiedades_enviadas"] = []
        estado["ultimo_lote_propiedades"] = []
        estado["propiedad_interes"] = None
        estado["estado"] = ESTADO_DIAGNOSTICO

    return hubo_cambio


def fusionar_lead(
    estado: dict,
    analisis: AnalisisMensaje,
    mensaje: str,
) -> None:
    lead = estado["lead"]
    esperado = estado.get("campo_esperado")

    nombre = analisis.lead.nombre

    if esperado == "nombre" and not nombre:
        candidato = capitalizar_nombre(mensaje)

        if es_nombre_persona_valido(candidato):
            nombre = candidato

    if nombre and not lead.get("nombre"):
        candidato = capitalizar_nombre(nombre)

        if es_nombre_persona_valido(candidato):
            lead["nombre"] = candidato

    correo = (
        extraer_correo(mensaje)
        or analisis.lead.correo
    )

    if correo and correo_valido(correo):
        lead["correo"] = correo.lower()

    telefono = (
        extraer_telefono(mensaje)
        or analisis.lead.whatsapp
    )

    if telefono:
        normalizado = normalizar_telefono_venezolano(
            telefono
        )

        if normalizado:
            lead["whatsapp"] = normalizado


def obtener_campo_diagnostico_faltante(
    estado: dict,
) -> Optional[str]:
    filtros = estado["filtros"]
    sin_preferencia = set(
        estado.get("sin_preferencia", [])
    )

    for campo in CAMPOS_DIAGNOSTICO:
        if campo in sin_preferencia:
            continue

        valor = filtros.get(campo)

        if valor in [None, "", []]:
            return campo

    return None


def obtener_campo_lead_faltante(
    estado: dict,
) -> Optional[str]:
    lead = estado["lead"]

    if not es_nombre_persona_valido(
        lead.get("nombre") or ""
    ):
        return "nombre"

    if not correo_valido(lead.get("correo") or ""):
        return "correo"

    if not normalizar_telefono_venezolano(
        lead.get("whatsapp")
    ):
        return "whatsapp"

    return None


# ============================================================
# RANKING
# ============================================================

def coincide_tipo_propiedad(
    propiedad: dict,
    tipo_buscado: str,
) -> bool:
    tipo = normalizar_tipo_propiedad(tipo_buscado)

    if not tipo:
        return True

    tipo_wasi = normalizar_tipo_propiedad(
        propiedad.get("tipo_propiedad_wasi", "")
    )

    titulo = normalizar_texto(
        propiedad.get("titulo", "")
    )

    if tipo == "casa":
        return any(
            valor in tipo_wasi or valor in titulo
            for valor in [
                "casa",
                "townhouse",
                "apartoquinta",
            ]
        )

    if tipo == "apartamento":
        return any(
            valor in tipo_wasi or valor in titulo
            for valor in [
                "apartamento",
                "penthouse",
            ]
        )

    return tipo in tipo_wasi or tipo in titulo


def obtener_precio_operacion(
    propiedad: dict,
    operacion: str,
) -> float:
    if operacion == "alquiler":
        return convertir_float_seguro(
            propiedad.get("precio_renta_float")
        )

    return convertir_float_seguro(
        propiedad.get("precio_venta_float")
    )


def evaluar_propiedad(
    propiedad_original: dict,
    filtros: dict,
) -> Optional[dict]:
    propiedad = deepcopy(propiedad_original)

    operacion = filtros.get("tipo_operacion")
    precio = obtener_precio_operacion(
        propiedad,
        operacion,
    )

    if precio <= 0:
        return None

    score = 0.0
    diferencias: List[str] = []

    tipo = filtros.get("tipo_propiedad")
    zona = filtros.get("zona")
    presupuesto = filtros.get("presupuesto_max")
    habitaciones_min = filtros.get("habitaciones_min")
    banos_min = filtros.get("banos_min")
    caracteristicas = filtros.get(
        "caracteristicas",
        [],
    )

    if tipo:
        if coincide_tipo_propiedad(propiedad, tipo):
            score += 35
        else:
            diferencias.append(
                f"Tipo diferente a {tipo}"
            )
            score -= 20

    if zona:
        if zona_coincide(
            zona,
            propiedad.get("zona", ""),
            propiedad.get("ciudad", ""),
        ):
            score += 30
        else:
            diferencias.append(
                f"Está en {propiedad.get('zona', 'otra zona')}"
            )
            score -= 12

    if presupuesto:
        if precio <= presupuesto:
            proporcion = precio / presupuesto
            score += 20 * proporcion
        else:
            exceso = (precio - presupuesto) / presupuesto

            if exceso > MAX_EXCESO_PRESUPUESTO_APROXIMADO:
                score -= 35
            else:
                score -= exceso * 50

            diferencias.append(
                f"Supera el presupuesto por "
                f"{formato_moneda(precio - presupuesto)}"
            )

    habitaciones = convertir_entero_seguro(
        propiedad.get("habitaciones")
    )

    if habitaciones_min is not None:
        if habitaciones >= habitaciones_min:
            score += 10
        else:
            diferencias.append(
                f"Tiene {habitaciones} habitaciones"
            )
            score -= 15

    banos = convertir_entero_seguro(
        propiedad.get("banos")
    )

    if banos_min is not None:
        if banos >= banos_min:
            score += 5
        else:
            diferencias.append(
                f"Tiene {banos} baños"
            )
            score -= 8

    texto_propiedad = normalizar_texto(
        " ".join([
            str(propiedad.get("titulo", "")),
            str(propiedad.get("descripcion", "")),
            str(
                propiedad.get(
                    "caracteristicas_texto",
                    "",
                )
            ),
        ])
    )

    caracteristicas_no_encontradas = []

    for caracteristica in caracteristicas:
        normalizada = normalizar_texto(
            caracteristica
        )

        if normalizada and normalizada in texto_propiedad:
            score += 5
        elif normalizada:
            caracteristicas_no_encontradas.append(
                caracteristica
            )

    if caracteristicas_no_encontradas:
        diferencias.append(
            "No se confirmaron: "
            + ", ".join(caracteristicas_no_encontradas)
        )

    propiedad["operacion_buscada"] = operacion
    propiedad["_score"] = round(score, 2)
    propiedad["_diferencias"] = diferencias
    propiedad["_coincidencia"] = (
        "exacta" if not diferencias else "aproximada"
    )

    return propiedad


def elegir_top_n_propiedades(
    inventario: List[dict],
    filtros: dict,
    n: int = 3,
    excluir_ids: Optional[List[str]] = None,
) -> List[dict]:
    excluir = {
        str(valor)
        for valor in (excluir_ids or [])
    }

    evaluadas: List[dict] = []

    for propiedad in inventario:
        propiedad_id = str(propiedad.get("id", ""))

        if not propiedad_id or propiedad_id in excluir:
            continue

        evaluada = evaluar_propiedad(
            propiedad,
            filtros,
        )

        if evaluada is not None:
            evaluadas.append(evaluada)

    exactas = sorted(
        [
            propiedad
            for propiedad in evaluadas
            if propiedad["_coincidencia"] == "exacta"
        ],
        key=lambda propiedad: propiedad["_score"],
        reverse=True,
    )

    aproximadas = sorted(
        [
            propiedad
            for propiedad in evaluadas
            if propiedad["_coincidencia"] == "aproximada"
        ],
        key=lambda propiedad: propiedad["_score"],
        reverse=True,
    )

    resultado = exactas[:n]

    if len(resultado) < n:
        faltantes = n - len(resultado)
        resultado.extend(aproximadas[:faltantes])

    return resultado


def buscar_propiedad_por_id(
    codigo: str,
) -> Optional[dict]:
    codigo_limpio = re.sub(r"\D", "", codigo or "")

    if not codigo_limpio:
        return None

    for propiedad in inventory_cache["inventario"]:
        if str(propiedad.get("id", "")) == codigo_limpio:
            return deepcopy(propiedad)

    return None


def obtener_propiedad_ultimo_lote(
    estado: dict,
    posicion: Optional[int],
) -> Optional[dict]:
    if not posicion:
        return None

    lote = estado.get(
        "ultimo_lote_propiedades",
        [],
    )

    indice = posicion - 1

    if indice < 0 or indice >= len(lote):
        return None

    return buscar_propiedad_por_id(
        lote[indice]
    )


# ============================================================
# FORMATO DE RESPUESTAS
# ============================================================

def formatear_ficha_propiedad(
    propiedad: dict,
    es_colega: bool,
    posicion: Optional[int] = None,
) -> str:
    operacion = propiedad.get(
        "operacion_buscada",
        "venta",
    )

    precio = obtener_precio_operacion(
        propiedad,
        operacion,
    )

    area = propiedad.get("area", "N/D")
    area_texto = (
        f"{area} m²"
        if str(area).strip()
        not in {"", "N/D", "None"}
        else "N/D"
    )

    titulo = propiedad.get(
        "titulo",
        "Propiedad",
    )

    if posicion:
        titulo = f"Opción {posicion}: {titulo}"

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
            "ℹ️ Coincidencia aproximada: "
            + "; ".join(diferencias[:2])
        )

    if es_colega:
        captador = propiedad.get(
            "captador_propiedad",
            "Asesor Mettryc",
        )

        telefono_sheet = obtener_telefono_captador(
            captador
        )

        telefono = (
            telefono_sheet
            if telefono_sheet != "N/D"
            else propiedad.get(
                "telefono_captador_wasi",
                "N/D",
            )
        )

        lineas.extend([
            f"👤 Captador: {captador}",
            f"📲 WhatsApp captador: {telefono or 'N/D'}",
        ])

    return "\n".join(lineas)


def construir_respuesta_propiedades(
    propiedades: List[dict],
    es_colega: bool,
) -> str:
    if not propiedades:
        return ""

    introduccion = (
        "Estas son las opciones que mejor se ajustan "
        "a la solicitud:"
    )

    fichas = []

    for indice, propiedad in enumerate(
        propiedades,
        start=1,
    ):
        fichas.append(
            formatear_ficha_propiedad(
                propiedad,
                es_colega,
                indice,
            )
        )

    if es_colega:
        cierre = (
            "Indícame cuál opción te interesa o escribe "
            "*más opciones* para enviarte las siguientes tres."
        )
    else:
        cierre = (
            "¿Te interesa alguna? Puedes decirme "
            "*la primera*, *la segunda*, *la tercera* "
            "o escribir *más opciones*."
        )

    return "\n\n".join(
        [introduccion] + fichas + [cierre]
    )


def construir_resumen_solicitud(
    estado: dict,
) -> str:
    filtros = estado["filtros"]
    partes = []

    if filtros.get("tipo_operacion"):
        partes.append(
            f"- Operación: {filtros['tipo_operacion']}"
        )

    if filtros.get("tipo_propiedad"):
        partes.append(
            f"- Tipo: {filtros['tipo_propiedad']}"
        )

    if filtros.get("zona"):
        partes.append(
            f"- Zona: {filtros['zona']}"
        )

    if filtros.get("presupuesto_max"):
        partes.append(
            "- Presupuesto máximo: "
            + formato_moneda(
                filtros["presupuesto_max"]
            )
        )

    if filtros.get("habitaciones_min") is not None:
        partes.append(
            "- Habitaciones mínimas: "
            + str(filtros["habitaciones_min"])
        )

    if filtros.get("banos_min") is not None:
        partes.append(
            f"- Baños mínimos: {filtros['banos_min']}"
        )

    if filtros.get("caracteristicas"):
        partes.append(
            "- Características: "
            + ", ".join(
                filtros["caracteristicas"]
            )
        )

    return "\n".join(partes) or "- Búsqueda general"


# ============================================================
# TELEGRAM
# ============================================================

async def enviar_mensaje_telegram(
    chat_id: str,
    mensaje: str,
) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    try:
        respuesta = await http_client.post(
            url,
            json={
                "chat_id": chat_id,
                "text": mensaje,
                "disable_web_page_preview": True,
            },
            timeout=TELEGRAM_TIMEOUT,
        )
        respuesta.raise_for_status()

        payload = respuesta.json()

        if not payload.get("ok"):
            raise ValueError(
                payload.get(
                    "description",
                    "Telegram rechazó el mensaje.",
                )
            )

        return True

    except Exception as exc:
        logger.error(
            "Error Telegram chat=%s tipo=%s detalle=%s",
            str(chat_id)[-4:],
            type(exc).__name__,
            str(exc)[:160],
        )
        return False


async def notificar_lead(
    estado: dict,
    agente: Optional[dict],
) -> bool:
    lead = estado["lead"]

    whatsapp = normalizar_telefono_venezolano(
        lead.get("whatsapp")
    )

    propiedad = estado.get("propiedad_interes")
    propiedad_texto = "No especificada"

    if propiedad:
        propiedad_texto = (
            f"{propiedad.get('titulo', 'Propiedad')} "
            f"(ID {propiedad.get('id', 'N/D')})\n"
            f"{propiedad.get('enlace', '')}"
        )

    nombre_agente = (
        agente.get("nombre")
        if agente
        else "Sin asignar"
    )

    mensaje_base = (
        "🏠 NUEVO LEAD METTRYC\n\n"
        f"ID lead: {estado.get('lead_id')}\n"
        f"Cliente: {lead.get('nombre')}\n"
        f"Correo: {lead.get('correo')}\n"
        f"WhatsApp: {whatsapp}\n"
        f"Enlace: https://wa.me/{whatsapp}\n\n"
        f"📋 SOLICITUD\n"
        f"{construir_resumen_solicitud(estado)}\n\n"
        f"⭐ PROPIEDAD DE INTERÉS\n"
        f"{propiedad_texto}\n\n"
        f"👤 Agente asignado: {nombre_agente}"
    )

    resultados = []

    if agente and agente.get("telegram_id"):
        resultados.append(
            await enviar_mensaje_telegram(
                str(agente["telegram_id"]).strip(),
                mensaje_base,
            )
        )

    for admin_id in TELEGRAM_ADMIN_IDS:
        resultados.append(
            await enviar_mensaje_telegram(
                admin_id,
                mensaje_base,
            )
        )

    return any(resultados)


# ============================================================
# FAQ DETERMINÍSTICA
# ============================================================

def responder_faq(mensaje: str) -> Optional[str]:
    texto = normalizar_texto(mensaje)

    if "honorarios" in texto or "comision" in texto:
        return (
            "Nuestros honorarios son 5% en operaciones de venta "
            "y un mes en alquiler, conforme a las prácticas del sector."
        )

    if any(
        palabra in texto
        for palabra in ["ubicacion", "direccion", "donde estan"]
    ):
        return (
            "Estamos en Valencia, Carabobo, CC Patio Trigal, "
            "local 300-6. Ubicación: "
            "https://maps.app.goo.gl/dSofuNmF89vNLv7X8"
        )

    if "negociable" in texto:
        return (
            "Sería cuestión de que hagas tu mejor oferta y "
            "se la presentaremos al propietario."
        )

    if any(
        frase in texto
        for frase in [
            "telefono del propietario",
            "contacto del propietario",
            "numero del propietario",
        ]
    ):
        return (
            "Por privacidad no compartimos el contacto directo "
            "del propietario. Un asesor puede ayudarte a gestionar "
            "la información o coordinar una visita."
        )

    if any(
        palabra in texto
        for palabra in ["trabajar", "unirme", "reclutamiento"]
    ):
        return (
            "El ingreso tiene un costo de $50 e incluye curso "
            "y credenciales. Formulario: "
            "https://forms.gle/SbLtHrey69fhf3Xt8"
        )

    return None


# ============================================================
# MOTOR DE CONVERSACIÓN
# ============================================================

async def mostrar_siguiente_lote(
    estado: dict,
) -> str:
    propiedades = elegir_top_n_propiedades(
        inventory_cache["inventario"],
        estado["filtros"],
        n=MAX_PROPIEDADES_POR_LOTE,
        excluir_ids=estado["propiedades_enviadas"],
    )

    if not propiedades:
        estado["ultimo_lote_propiedades"] = []

        return (
            "Ya te mostré todas las opciones disponibles con "
            "estos criterios. Podemos cambiar la zona, ampliar "
            "el presupuesto o ajustar alguna característica. "
            "¿Qué te gustaría modificar?"
        )

    ids = [
        str(propiedad["id"])
        for propiedad in propiedades
    ]

    estado["propiedades_enviadas"].extend(ids)
    estado["ultimo_lote_propiedades"] = ids
    estado["estado"] = ESTADO_RESULTADOS
    estado["campo_esperado"] = None

    return construir_respuesta_propiedades(
        propiedades,
        estado.get("rol") == "colega_inmobiliario",
    )


async def procesar_conversacion(
    sender: str,
    mensaje: str,
    message_id: str,
) -> str:
    estado = await session_store.get(sender)

    analisis = await analizar_mensaje_ia(
        mensaje,
        estado,
    )

    rol_determinista = detectar_rol_determinista(
        mensaje
    )

    if rol_determinista:
        estado["rol"] = rol_determinista
    elif (
        analisis.rol
        and analisis.confianza_rol >= 0.85
    ):
        estado["rol"] = analisis.rol
    elif not estado.get("rol"):
        estado["rol"] = "cliente"

    if (
        analisis.intencion == "nueva_busqueda"
        or detectar_nueva_busqueda(mensaje)
    ):
        estado = reiniciar_busqueda(estado)
        estado["campo_esperado"] = "tipo_operacion"

        respuesta = (
            "¡Perfecto! Comencemos una nueva búsqueda. "
            + PREGUNTAS_DIAGNOSTICO["tipo_operacion"]
        )

        await session_store.set(sender, estado)
        return respuesta

    fusionar_lead(
        estado,
        analisis,
        mensaje,
    )

    aplicar_filtros(
        estado,
        analisis,
    )

    estado["ultima_intencion"] = analisis.intencion
    estado["ultimo_mensaje_id"] = message_id

    codigo = (
        analisis.codigo_inmueble
        or extraer_codigo_inmueble(mensaje)
    )

    if codigo:
        propiedad = buscar_propiedad_por_id(codigo)

        if propiedad:
            propiedad["operacion_buscada"] = (
                estado["filtros"].get(
                    "tipo_operacion"
                )
                or (
                    "venta"
                    if propiedad.get(
                        "precio_venta_float",
                        0,
                    ) > 0
                    else "alquiler"
                )
            )

            estado["ultimo_lote_propiedades"] = [
                propiedad["id"]
            ]

            if propiedad["id"] not in estado[
                "propiedades_enviadas"
            ]:
                estado["propiedades_enviadas"].append(
                    propiedad["id"]
                )

            estado["estado"] = ESTADO_RESULTADOS

            respuesta = construir_respuesta_propiedades(
                [propiedad],
                estado["rol"] == "colega_inmobiliario",
            )

            await session_store.set(sender, estado)
            return respuesta

        respuesta = (
            "No encontré ese código dentro del inventario activo. "
            "Verifica el número o dime qué tipo de propiedad buscas."
        )

        await session_store.set(sender, estado)
        return respuesta

    faq = responder_faq(mensaje)

    if faq and analisis.intencion in {
        "faq",
        "otro",
        "saludo",
    }:
        await session_store.set(sender, estado)
        return faq

    if analisis.intencion == "agradecimiento":
        await session_store.set(sender, estado)
        return "¡Siempre a la orden! ☺️"

    if (
        not estado.get("saludado")
        and analisis.intencion == "saludo"
        and not any(
            valor
            for valor in estado["filtros"].values()
        )
    ):
        estado["saludado"] = True

        respuesta = (
            "¡Hola! Soy Paty, asistente de Mettryc Realty, "
            "la Primera Tecnoinmobiliaria de Venezuela. "
            "¿Cómo te puedo ayudar hoy?"
        )

        await session_store.set(sender, estado)
        return respuesta

    estado["saludado"] = True

    if (
        estado["estado"] == ESTADO_RESULTADOS
        and (
            analisis.intencion == "mas_opciones"
            or detectar_mas_opciones(mensaje)
        )
    ):
        respuesta = await mostrar_siguiente_lote(
            estado
        )

        await session_store.set(sender, estado)
        return respuesta

    if estado["estado"] == ESTADO_RESULTADOS:
        posicion = (
            analisis.referencia_posicion
            or detectar_posicion_propiedad(mensaje)
        )

        manifiesta_interes = (
            analisis.intencion == "interes_propiedad"
            or posicion is not None
            or any(
                frase in normalizar_texto(mensaje)
                for frase in [
                    "me interesa",
                    "quiero verla",
                    "agendar visita",
                    "quiero visitar",
                    "mas informacion de esa",
                ]
            )
        )

        if manifiesta_interes:
            propiedad = obtener_propiedad_ultimo_lote(
                estado,
                posicion,
            )

            if not propiedad and len(
                estado.get(
                    "ultimo_lote_propiedades",
                    [],
                )
            ) == 1:
                propiedad = buscar_propiedad_por_id(
                    estado[
                        "ultimo_lote_propiedades"
                    ][0]
                )

            if not propiedad:
                respuesta = (
                    "¡Con gusto! Indícame si te interesa "
                    "la primera, la segunda o la tercera opción."
                )

                await session_store.set(sender, estado)
                return respuesta

            estado["propiedad_interes"] = propiedad

            if estado["rol"] == "colega_inmobiliario":
                respuesta = (
                    "Perfecto, colega. En la ficha tienes el "
                    "contacto del captador para coordinar la "
                    "información o la visita. Si deseas, también "
                    "puedo enviarte más opciones."
                )

                await session_store.set(sender, estado)
                return respuesta

            estado["estado"] = ESTADO_CAPTURA_LEAD

    if (
        estado["estado"] == ESTADO_CAPTURA_LEAD
        and estado["rol"] == "cliente"
    ):
        faltante = obtener_campo_lead_faltante(
            estado
        )

        if faltante:
            estado["campo_esperado"] = faltante

            respuesta = PREGUNTAS_LEAD[faltante]

            await session_store.set(sender, estado)
            return respuesta

        if not estado.get("lead_id"):
            estado["lead_id"] = str(uuid.uuid4())

        if not estado.get("agente_asignado"):
            estado["agente_asignado"] = (
                await asignar_agente_round_robin()
            )

        if not estado.get("notificacion_enviada"):
            notificada = await notificar_lead(
                estado,
                estado.get("agente_asignado"),
            )

            estado["notificacion_enviada"] = notificada

        estado["estado"] = ESTADO_ASIGNADO
        estado["campo_esperado"] = None

        agente = estado.get("agente_asignado")
        nombre_agente = (
            agente.get("nombre")
            if agente
            else "uno de nuestros asesores"
        )

        respuesta = (
            f"¡Listo! {nombre_agente} recibió tu solicitud "
            "y te contactará por WhatsApp para ayudarte con "
            "la propiedad. ¡Gracias por confiar en Mettryc Realty!"
        )

        await session_store.set(sender, estado)
        return respuesta

    if estado["estado"] == ESTADO_ASIGNADO:
        if analisis.intencion == "buscar":
            estado = reiniciar_busqueda(estado)

        else:
            respuesta = (
                "Tu solicitud ya fue asignada. Un asesor de "
                "Mettryc Realty te contactará muy pronto. "
                "Si quieres buscar otra propiedad, escribe "
                "*nueva búsqueda*."
            )

            await session_store.set(sender, estado)
            return respuesta

    faltante = obtener_campo_diagnostico_faltante(
        estado
    )

    if faltante:
        estado["estado"] = ESTADO_DIAGNOSTICO
        estado["campo_esperado"] = faltante

        respuesta = PREGUNTAS_DIAGNOSTICO[faltante]

        await session_store.set(sender, estado)
        return respuesta

    respuesta = await mostrar_siguiente_lote(
        estado
    )

    await session_store.set(sender, estado)
    return respuesta


# ============================================================
# FASTAPI
# ============================================================
async def inicializar_datos_en_segundo_plano() -> None:
    resultados = await asyncio.gather(
        actualizar_inventario(force=True),
        sincronizar_google_sheet(force=True),
        return_exceptions=True,
    )

    for resultado in resultados:
        if isinstance(resultado, Exception):
            logger.error(
                "Error inicializando datos tipo=%s detalle=%s",
                type(resultado).__name__,
                str(resultado)[:200],
            )

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    global http_client

    http_client = httpx.AsyncClient(
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
        ),
        headers={
            "User-Agent": "Mettryc-Chatbot/1.0",
        },
    )

    if REDIS_URL and redis_async:
        try:
            redis_client = redis_async.from_url(
                REDIS_URL,
                decode_responses=True,
            )
            await redis_client.ping()
            logger.info("Redis conectado.")
        except Exception as exc:
            logger.error(
                "No fue posible conectar Redis: %s",
                str(exc)[:200],
            )
            redis_client = None
    else:
        logger.warning(
            "Redis no está configurado. "
            "El estado funcionará solo en memoria."
        )

    tarea_inicializacion = asyncio.create_task(
        inicializar_datos_en_segundo_plano()
    )

    yield

    if not tarea_inicializacion.done():
        tarea_inicializacion.cancel()

        try:
            await tarea_inicializacion
        except asyncio.CancelledError:
            pass

    if http_client:
        await http_client.aclose()

    if redis_client:
        await redis_client.aclose()


app = FastAPI(
    title="Mettryc Realty Chatbot",
    version="1.0.0",
    lifespan=lifespan,
)

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {
        "service": "Mettryc Realty Chatbot",
        "status": "online",
    }

@app.get("/health")
async def health():
    redis_ok = False

    if redis_client:
        try:
            redis_ok = bool(
                await redis_client.ping()
            )
        except Exception:
            redis_ok = False

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
        "redis": redis_ok,
    }


@app.post("/admin/refresh")
async def refresh_data(
    x_api_key: Optional[str] = Header(
        default=None,
        alias="x-api-key",
    ),
):
    validar_api_key(x_api_key)

    inventario = await actualizar_inventario(
        force=True
    )

    await sincronizar_google_sheet(
        force=True
    )

    return {
        "ok": True,
        "inventario_actualizado": inventario,
        "propiedades": len(
            inventory_cache["inventario"]
        ),
        "agentes": len(
            sheets_cache["agentes"]
        ),
    }


def validar_api_key(api_key: Optional[str]) -> None:
    if not API_KEYS_AGENTES:
        logger.error(
            "API_KEYS_AGENTES no está configurado."
        )
        raise HTTPException(
            status_code=503,
            detail="Servicio no configurado.",
        )

    if not api_key or api_key not in API_KEYS_AGENTES:
        raise HTTPException(
            status_code=403,
            detail="Acceso denegado.",
        )


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

    if await session_store.is_duplicate(
        sender,
        message_id,
    ):
        return {"replies": []}

    if not inventory_cache["inventario"]:
        await actualizar_inventario(force=True)
    elif necesita_actualizar_inventario():
        asyncio.create_task(
            actualizar_inventario(force=False)
        )

    asyncio.create_task(
        sincronizar_google_sheet()
    )

    try:
        async with user_lock(sender):
            respuesta = await procesar_conversacion(
                sender=sender,
                mensaje=mensaje,
                message_id=message_id,
            )

        if not respuesta:
            respuesta = (
                "No pude procesar correctamente tu mensaje. "
                "¿Puedes intentarlo nuevamente?"
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

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception(
            "Error crítico webhook sender=%s tipo=%s",
            sender[-4:],
            type(exc).__name__,
        )

        return {
            "replies": [
                {
                    "message": (
                        "Lo siento, tuve un inconveniente "
                        "procesando tu solicitud. ¿Puedes "
                        "intentarlo nuevamente? 🙏"
                    )
                }
            ]
        }
