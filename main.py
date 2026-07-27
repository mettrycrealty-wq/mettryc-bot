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
from typing import Any, Dict, List, Literal, Optional, Set, Tuple, Type

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

# ============================================================
# LOGS Y CONFIGURACIÓN
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("mettryc-chatbot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

DEBUG_MODE = os.getenv("DEBUG_MODE", "true").lower() == "true"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
WASI_TOKEN = os.getenv("WASI_TOKEN", "")
WASI_COMPANY_ID = os.getenv("WASI_COMPANY_ID", "")
GOOGLE_SHEET_TURNOS_URL = os.getenv("GOOGLE_SHEET_TURNOS_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

MODELO_AGENTE_PRINCIPAL = os.getenv(
    "MODELO_AGENTE_PRINCIPAL",
    os.getenv("MODELO_ANALISIS_PRINCIPAL", "google/gemini-2.5-flash"),
)
MODELO_AGENTE_RESPALDO = os.getenv(
    "MODELO_AGENTE_RESPALDO",
    os.getenv("MODELO_ANALISIS_RESPALDO", "openai/gpt-4o-mini"),
)

OPENROUTER_TIMEOUT = float(os.getenv("OPENROUTER_TIMEOUT", "35"))
WASI_TIMEOUT = float(os.getenv("WASI_TIMEOUT", "40"))
SHEETS_TIMEOUT = float(os.getenv("SHEETS_TIMEOUT", "20"))
TELEGRAM_TIMEOUT = float(os.getenv("TELEGRAM_TIMEOUT", "15"))

MAX_PROPIEDADES_POR_LOTE = int(
    os.getenv("MAX_PROPIEDADES_POR_LOTE", "3")
)
MAX_EXCESO_PRESUPUESTO = float(
    os.getenv("MAX_EXCESO_PRESUPUESTO", "0.20")
)
MAX_HISTORIAL = int(os.getenv("MAX_HISTORIAL", "24"))
DUPLICATE_TTL_SECONDS = int(
    os.getenv("DUPLICATE_TTL_SECONDS", "180")
)

SENDER_ES_WHATSAPP = (
    os.getenv("SENDER_ES_WHATSAPP", "true").lower() == "true"
)

API_KEYS_AGENTES = {
    clave.strip()
    for clave in os.getenv("API_KEYS_AGENTES", "").split(",")
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
        os.getenv("INTERVALO_ACTUALIZACION_SHEETS_MINUTOS", "60")
    )
)
INTERVALO_ACTUALIZACION_WASI = timedelta(
    hours=int(os.getenv("INTERVALO_ACTUALIZACION_WASI_HORAS", "12"))
)
# ============================================================
# MODELOS ESTRUCTURADOS PARA LA IA
# ============================================================

class ActualizacionesConversacion(BaseModel):
    tipo_operacion: Optional[Literal["venta", "alquiler"]] = None
    tipo_propiedad: Optional[str] = None
    ciudad: Optional[str] = None
    zona: Optional[str] = None
    presupuesto_max: Optional[float] = None
    habitaciones_min: Optional[int] = None
    banos_min: Optional[int] = None
    garajes_min: Optional[int] = None
    caracteristicas: List[str] = Field(default_factory=list)

    nombre: Optional[str] = None
    correo: Optional[str] = None
    whatsapp: Optional[str] = None
    usar_numero_actual: bool = False


class ReferenciaPropiedad(BaseModel):
    codigo: Optional[str] = None
    posicion: Optional[int] = None
    enlace: Optional[str] = None
    origen: Optional[
        Literal[
            "mercadolibre",
            "mettryc",
            "instagram",
            "facebook",
            "portal",
            "anuncio",
            "desconocido",
        ]
    ] = None


class SolicitudesUsuario(BaseModel):
    quiere_agendar_visita: bool = False
    quiere_hablar_con_humano: bool = False
    quiere_mas_opciones: bool = False
    pregunta_sobre_propiedad: Optional[str] = None
    pregunta_sobre_mettryc: Optional[str] = None


class AccionAgente(BaseModel):
    tipo: Literal[
        "responder",
        "buscar_propiedades",
        "mostrar_mas_propiedades",
        "buscar_por_codigo",
        "consultar_propiedad",
        "solicitar_captador",
        "seleccionar_propiedad",
        "agendar_visita",
        "hablar_con_humano",
        "pedir_codigo_inmueble",
        "consultar_mettryc",
        "reiniciar_busqueda",
        "pedir_aclaracion",
    ] = "responder"

    codigo: Optional[str] = None
    posicion: Optional[int] = None

class DecisionAgente(BaseModel):
    mensaje: str = ""
    intencion_principal: str = "conversar"
    intenciones_secundarias: List[str] = Field(default_factory=list)

    rol: Optional[Literal["cliente", "colega_inmobiliario"]] = None
    confianza_rol: float = 0.0

    actualizaciones: ActualizacionesConversacion = Field(
        default_factory=ActualizacionesConversacion
    )
    referencia_propiedad: ReferenciaPropiedad = Field(
        default_factory=ReferenciaPropiedad
    )
    solicitudes: SolicitudesUsuario = Field(
        default_factory=SolicitudesUsuario
    )

    campos_sin_preferencia: List[str] = Field(default_factory=list)
    responde_pregunta_pendiente: bool = False
    razon_accion: str = ""
    accion: AccionAgente = Field(default_factory=AccionAgente)


class TextoResultado(BaseModel):
    introduccion: str = ""
    cierre: str = ""


class RespuestaPropiedadIA(BaseModel):
    respuesta: str
    informacion_no_especificada: List[str] = Field(default_factory=list)

class InterpretacionRespuestaCortaIA(BaseModel):
    significado: Literal[
        "afirmativa",
        "negativa",
        "aceptacion",
        "ambigua",
    ] = "ambigua"

    responde_a_la_pregunta: bool = True
    requiere_aclaracion: bool = False
    mantener_pregunta_pendiente: bool = False
    respuesta: str

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

property_detail_cache: Dict[str, Dict[str, Any]] = {}

sheets_cache: Dict[str, Any] = {
    "agentes": [],
    "captadores": {},
    "ultima_actualizacion": None,
}

catalogo_geografico: Dict[str, Any] = {
    "ciudades_norm": {},
    "zonas_norm": {},
    "zonas_por_ciudad": {},
    "frases_ciudades": [],
    "frases_zonas": [],
}

inventory_refresh_lock = asyncio.Lock()
sheets_refresh_lock = asyncio.Lock()
round_robin_lock = asyncio.Lock()
round_robin_index = -1

http_client: Optional[httpx.AsyncClient] = None
# ============================================================
# UTILIDADES GENERALES
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
    texto = re.sub(r"[^a-z0-9@.+\-\s]", " ", texto)
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
        if valor in (None, "", "N/D"):
            return 0.0
        return float(valor)
    except (TypeError, ValueError):
        return 0.0


def convertir_entero(valor: Any) -> int:
    try:
        if valor in (None, "", "N/D"):
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
    return normalizar_telefono(coincidencia.group(1))


def extraer_correo(texto: str) -> Optional[str]:
    coincidencia = re.search(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        texto or "",
        re.IGNORECASE,
    )
    return coincidencia.group(0).lower() if coincidencia else None


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
    palabras = nombre.split()

    bloqueadas = {
        "Hola", "Buenas", "Gracias", "Apartamento", "Casa",
        "Quiero", "Visitar", "Opcion", "Cliente", "Busco",
        "Buscar", "Necesito", "Propiedad", "Información",
    }

    if not 2 <= len(palabras) <= 6:
        return False
    if any(palabra in bloqueadas for palabra in palabras):
        return False
    if any(re.search(r"\d", palabra) for palabra in palabras):
        return False

    return True
def parsear_precio_wasi(valor: Any, etiqueta: Any) -> float:
    numero = convertir_float(valor)
    if numero > 0:
        return numero

    texto = re.sub(r"[^\d.,]", "", str(etiqueta or ""))
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

    return convertir_float(texto)


def normalizar_tipo_propiedad(valor: Any) -> str:
    texto = normalizar_texto(valor)

    equivalencias = {
        "apto": "apartamento",
        "apart": "apartamento",
        "apartamento tipo estudio": "apartamento",
        "town house": "townhouse",
        "towhouse": "townhouse",
        "aptoquinta": "apartoquinta",
        "aparto quinta": "apartoquinta",
    }

    if texto in equivalencias:
        return equivalencias[texto]

    tipos = [
        "townhouse", "apartoquinta", "penthouse", "apartamento",
        "casa", "quinta", "oficina", "local", "galpon", "terreno",
    ]

    for tipo in tipos:
        if tipo in texto:
            return tipo

    return texto


def tokens_nombre(valor: Any) -> Set[str]:
    bloqueadas = {
        "de", "del", "la", "el", "los", "las", "asesor", "asesora",
    }
    return {
        token
        for token in normalizar_texto(valor).split()
        if len(token) >= 2 and token not in bloqueadas
    }


def tokens_zona(valor: Any) -> Set[str]:
    bloqueadas = {
        "el", "la", "los", "las", "de", "del", "en",
        "zona", "sector", "urbanizacion", "ciudad", "venezuela",
    }
    return {
        token
        for token in normalizar_texto(valor).split()
        if len(token) >= 2 and token not in bloqueadas
    }


def contiene_termino(texto: str, termino: str) -> bool:
    if not texto or not termino:
        return False
    return re.search(
        r"\b" + re.escape(termino) + r"\b",
        texto,
    ) is not None


def convertir_caracteristicas_lista(valor: Any) -> List[str]:
    resultado: List[str] = []

    if isinstance(valor, str):
        partes = re.split(r"[,;|\n]+", valor)
        resultado.extend(parte.strip() for parte in partes if parte.strip())

    elif isinstance(valor, list):
        for elemento in valor:
            if isinstance(elemento, dict):
                nombre = (
                    elemento.get("name")
                    or elemento.get("label")
                    or elemento.get("nombre")
                    or elemento.get("feature")
                )
                activo = elemento.get("value", elemento.get("active", True))
                if nombre and activo not in (False, 0, "0", None, ""):
                    resultado.append(str(nombre).strip())
            elif elemento not in (None, "", False, 0):
                resultado.append(str(elemento).strip())

    elif isinstance(valor, dict):
        for clave, contenido in valor.items():
            if contenido in (None, "", False, 0, "0"):
                continue
            if isinstance(contenido, str) and contenido not in ("1", "true"):
                resultado.append(f"{clave}: {contenido}")
            else:
                resultado.append(str(clave))

    normalizadas = {}
    for elemento in resultado:
        clave = normalizar_texto(elemento)
        if clave:
            normalizadas.setdefault(clave, elemento)

    return list(normalizadas.values())


def extraer_area_principal_wasi(payload: Dict[str, Any]) -> Optional[float]:
    claves = [
        "area", "constructed_area", "construction_area", "built_area",
        "building_area", "total_area", "surface", "surface_total",
        "lot_area", "land_area",
    ]

    for clave in claves:
        valor = payload.get(clave)
        if isinstance(valor, dict):
            valor = (
                valor.get("value")
                or valor.get("valor")
                or valor.get("amount")
            )

        if isinstance(valor, str):
            numero = re.sub(r"[^\d.,]", "", valor)
            if numero:
                valor = numero.replace(".", "").replace(",", ".")

        numero_float = convertir_float(valor)
        if numero_float > 0:
            return numero_float

    return None
# ============================================================
# DETECCIONES TÉCNICAS Y CONVERSACIONALES
# ============================================================

MERCADOLIBRE_URL_RE = re.compile(
    r"https?://[^\s]+?-(\d+)-_JM\b",
    re.IGNORECASE,
)

PALABRAS_CONSULTA_DIRECTA = {
    "precio", "informacion", "información", "info",
    "disponibilidad", "disponible", "sigue disponible",
}

FRASES_HUMANO = [
    "hablar con un agente",
    "hablar con un asesor",
    "hablar con una persona",
    "hablar con un humano",
    "comunicarme con un agente",
    "comunicarme con un asesor",
    "que me llame un asesor",
    "necesito un agente",
    "necesito un asesor",
    "atencion humana",
    "atención humana",
]

FRASES_VISITA = [
    "agendar una visita",
    "coordinar una visita",
    "quiero visitarla",
    "quiero visitarlo",
    "quiero verla",
    "quiero verlo",
    "hacer una visita",
    "visitar la propiedad",
]


def extraer_codigo_mercadolibre(texto: str) -> Optional[str]:
    coincidencia = MERCADOLIBRE_URL_RE.search(texto or "")
    return coincidencia.group(1) if coincidencia else None


def extraer_codigo_inmueble(
    texto: str,
    permitir_solo_digitos: bool = False,
) -> Optional[str]:
    texto_original = str(texto or "").strip()

    if not texto_original:
        return None

    # Un correo nunca debe ser interpretado como código de inmueble.
    if extraer_correo(texto_original):
        return None

    patrones = [
        r"mettryc\.com/inmueble/(\d+)",
        r"\b(?:codigo|código|cod|inmueble)\s*[:#-]?\s*(\d{4,})\b",
        r"\b(?:ALM|EJL|LR|JM|MFR|TH|AM|ABJ|WD|JH|EM|JD)-?(\d{4,})\b",
        r"/MLV-\d+-[A-Za-z0-9\-]+-(\d+)-?_JM",
    ]

    for patron in patrones:
        coincidencia = re.search(
            patron,
            texto_original,
            re.IGNORECASE,
        )

        if coincidencia:
            codigo = re.sub(r"\D", "", coincidencia.group(1))

            if len(codigo) >= 4:
                return codigo

    # Solo se permite un número sin prefijo cuando el sistema
    # se encuentra explícitamente esperando el código.
    if permitir_solo_digitos:
        numero_limpio = re.sub(r"[\s.,-]", "", texto_original)

        if re.fullmatch(r"\d{4,10}", numero_limpio):
            return numero_limpio

    return None


def detectar_posicion(texto: str) -> Optional[int]:
    normalizado = normalizar_texto(texto)

    patrones = {
        1: ["primera", "opcion 1", "propiedad 1", "numero 1"],
        2: ["segunda", "opcion 2", "propiedad 2", "numero 2"],
        3: ["tercera", "opcion 3", "propiedad 3", "numero 3"],
        4: ["cuarta", "opcion 4", "propiedad 4", "numero 4"],
        5: ["quinta", "opcion 5", "propiedad 5", "numero 5"],
    }

    for posicion, frases in patrones.items():
        if any(frase in normalizado for frase in frases):
            return posicion

    coincidencia = re.search(
        r"\b(?:opcion|propiedad|inmueble|numero)\s*#?\s*([1-5])\b",
        normalizado,
    )
    return int(coincidencia.group(1)) if coincidencia else None


def detectar_rol_explicito(texto: str) -> Optional[str]:
    normalizado = normalizar_texto(texto)

    patrones_cliente = [
        r"\bpara\s+mi\b",
        r"\bes\s+para\s+mi\b",
        r"\bbusco\s+para\s+mi\b",
        r"\blo\s+quiero\s+para\s+mi\b",
        r"\bsoy\s+cliente\b",
        r"\bpara\s+uso\s+personal\b",
        r"\bpara\s+mi\s+(uso|familia|mama|madre|padre|esposa|esposo|pareja|abuela|abuelo)\b",
        r"\bno\s+soy\s+(asesor|asesora|agente|corredor|corredora|broker|realtor)\b",
    ]

    patrones_colega = [
        r"\bsoy\s+(asesor|asesora|agente|corredor|corredora|broker|realtor)\b",
        r"\bsoy\s+colega\b",
        r"\b(?:hola|saludos|buenas)\s+colega\b",
        r"\bcolega\b",
        r"\btengo\s+un\s+cliente\b",
        r"\bbusco\s+para\s+(un|una|mi)\s+cliente\b",
        r"\bes\s+para\s+(un|una|mi)\s+cliente\b",
        r"\bpara\s+(un|una|mi)\s+cliente\b",
        r"\btrabajo\s+en\s+una\s+inmobiliaria\b",
        r"\bcomparto\s+comision\b",
    ]

    if any(
        re.search(patron, normalizado)
        for patron in patrones_cliente
    ):
        return "cliente"

    if any(
        re.search(patron, normalizado)
        for patron in patrones_colega
    ):
        return "colega_inmobiliario"

    return None


def solicita_humano(texto: str) -> bool:
    normalizado = normalizar_texto(texto)
    return any(
        normalizar_texto(frase) in normalizado
        for frase in FRASES_HUMANO
    )


def solicita_visita(texto: str) -> bool:
    normalizado = normalizar_texto(texto)
    return any(
        normalizar_texto(frase) in normalizado
        for frase in FRASES_VISITA
    )


def pide_mas_opciones(texto: str) -> bool:
    normalizado = normalizar_texto(texto)
    frases = [
        "mas opciones", "otras opciones", "quiero ver mas",
        "muestrame otras", "ninguna me interesa",
        "ninguna me gusto", "siguientes opciones",
    ]
    return any(frase in normalizado for frase in frases)

def solicita_datos_captador(texto: str) -> bool:
    normalizado = normalizar_texto(texto)

    frases = [
        "captador",
        "contacto del captador",
        "telefono del captador",
        "whatsapp del captador",
        "numero del captador",
        "quien capto",
        "quien es el captador",
        "dame el contacto",
        "pasame el contacto",
        "contacto del asesor de la propiedad",
        "asesor captador",
        "quien lleva esa propiedad",
        "quien lleva la propiedad",
        "con quien coordino",
        "con quien puedo coordinar",
    ]

    return any(
        frase in normalizado
        for frase in frases
    )


def es_solicitud_informacion_ambigua(
    texto: str,
) -> bool:
    normalizado = normalizar_texto(texto)

    mensajes_ambiguos = {
        "informacion",
        "info",
        "quiero informacion",
        "necesito informacion",
        "solicito informacion",
        "deseo informacion",
        "me das informacion",
        "puedes darme informacion",
    }

    return normalizado in mensajes_ambiguos


def es_consulta_inventario_directa(
    texto: str,
) -> bool:
    normalizado = normalizar_texto(texto)

    frases = [
        "precio",
        "cual es el precio",
        "cuanto cuesta",
        "cuanto vale",
        "disponible",
        "esta disponible",
        "sigue disponible",
        "disponibilidad",
        "canon",
        "precio de venta",
        "precio de alquiler",
        "informacion de la propiedad",
        "informacion del inmueble",
        "informacion de la casa",
        "informacion del apartamento",
        "informacion del local",
        "informacion del anuncio",
        "informacion de la publicacion",
    ]

    return any(
        normalizado == frase
        or normalizado.startswith(frase)
        for frase in frases
    )


def tiene_propiedad_en_contexto(
    estado: dict,
) -> bool:
    return bool(
        estado.get("propiedad_interes")
        or estado.get("propiedad_activa_id")
        or len(estado.get("ultimo_lote", [])) == 1
    )


def mensaje_solicitud_codigo_inmueble() -> str:
    return (
        "Para ubicar la propiedad exacta, envíame el código que "
        "aparece al final del título o en la descripción del "
        "anuncio. Por ejemplo: AM-9935990 o 9935990."
    )

def detectar_operacion(texto: str) -> Optional[str]:
    normalizado = normalizar_texto(texto)

    if any(
        palabra in normalizado
        for palabra in ["comprar", "compra", "venta", "compro"]
    ):
        return "venta"

    if any(
        palabra in normalizado
        for palabra in ["alquiler", "alquilar", "renta", "arrendar"]
    ):
        return "alquiler"

    return None


def detectar_tipo_propiedad(texto: str) -> Optional[str]:
    normalizado = normalizar_texto(texto)

    patrones = [
        (
            "apartamento",
            [
                r"\bapartamentos?\b",
                r"\baptos?\b",
                r"\bpenthouses?\b",
                r"\blofts?\b",
            ],
        ),
        (
            "casa",
            [
                r"\bcasas?\b",
                r"\bquintas?\b",
                r"\bchalets?\b",
                r"\bvillas?\b",
            ],
        ),
        (
            "townhouse",
            [
                r"\btownhouses?\b",
                r"\btown\s+houses?\b",
            ],
        ),
        (
            "oficina",
            [
                r"\boficinas?\b",
            ],
        ),
        (
            "local",
            [
                r"\blocal(?:es)?\b",
                r"\blocal(?:es)?\s+comercial(?:es)?\b",
            ],
        ),
        (
            "galpon",
            [
                r"\bgalpones?\b",
                r"\bdepositos?\b",
            ],
        ),
        (
            "terreno",
            [
                r"\bterrenos?\b",
                r"\blotes?\b",
                r"\bparcelas?\b",
            ],
        ),
    ]

    for tipo, expresiones in patrones:
        if any(
            re.search(expresion, normalizado)
            for expresion in expresiones
        ):
            return tipo

    return None


def detectar_presupuesto(texto: str) -> float:
    if not texto:
        return 0.0

    # Eliminar correos para no tomar sus números como presupuesto.
    texto_sin_correos = re.sub(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        " ",
        texto,
        flags=re.IGNORECASE,
    )

    normalizado = normalizar_texto(texto_sin_correos)

    patron = re.compile(
        r"(?<!\w)"
        r"(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)"
        r"\s*(millones?|millon|mil|k|m)?"
        r"\s*(?:usd|dolares|\$)?"
        r"(?!\w)",
        re.IGNORECASE,
    )

    candidatos: List[float] = []

    for coincidencia in patron.finditer(normalizado):
        fragmento = coincidencia.group(1)
        multiplicador = coincidencia.group(2)

        contexto = normalizado[
            max(0, coincidencia.start() - 25):
            min(len(normalizado), coincidencia.end() + 25)
        ]

        if any(
            palabra in contexto
            for palabra in [
                "habitacion",
                "habitaciones",
                "cuarto",
                "cuartos",
                "bano",
                "banos",
                "puesto",
                "puestos",
                "garaje",
                "garajes",
                "codigo",
                "cod ",
                "inmueble",
            ]
        ):
            continue

        try:
            if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", fragmento):
                numero = float(fragmento.replace(".", ""))

            elif re.fullmatch(r"\d{1,3}(?:,\d{3})+", fragmento):
                numero = float(fragmento.replace(",", ""))

            elif "," in fragmento and "." not in fragmento:
                parte_decimal = fragmento.split(",")[-1]

                if len(parte_decimal) == 3:
                    numero = float(fragmento.replace(",", ""))
                else:
                    numero = float(fragmento.replace(",", "."))

            elif "." in fragmento:
                parte_decimal = fragmento.split(".")[-1]

                if len(parte_decimal) == 3:
                    numero = float(fragmento.replace(".", ""))
                else:
                    numero = float(fragmento)

            else:
                numero = float(fragmento)

        except ValueError:
            continue

        if multiplicador:
            multiplicador = normalizar_texto(multiplicador)

            if multiplicador in {"mil", "k"}:
                numero *= 1000

            elif multiplicador in {
                "m",
                "millon",
                "millones",
            }:
                numero *= 1000000

        if numero >= 100:
            candidatos.append(numero)

    return max(candidatos) if candidatos else 0.0


def detectar_numero_preferencia(
    texto: str,
    palabras: List[str],
) -> Optional[int]:
    normalizado = normalizar_texto(texto)

    for palabra in palabras:
        patrones = [
            rf"\b(\d{{1,2}})\s*{re.escape(palabra)}",
            rf"\b{re.escape(palabra)}\s*(\d{{1,2}})",
        ]
        for patron in patrones:
            coincidencia = re.search(patron, normalizado)
            if coincidencia:
                return int(coincidencia.group(1))

    return None


CARACTERISTICAS_CLAVE = {
    "jardin": "jardín",
    "terraza": "terraza",
    "patio": "patio",
    "piscina": "piscina",
    "amoblado": "amoblado",
    "amoblada": "amoblado",
    "ascensor": "ascensor",
    "vigilancia": "vigilancia",
    "seguridad": "seguridad",
    "tanque": "tanque de agua",
    "pozo": "pozo de agua",
    "planta electrica": "planta eléctrica",
    "estudio": "estudio",
    "family room": "family room",
    "parrillera": "parrillera",
}


def detectar_caracteristicas(texto: str) -> List[str]:
    normalizado = normalizar_texto(texto)
    resultado = {
        etiqueta
        for clave, etiqueta in CARACTERISTICAS_CLAVE.items()
        if contiene_termino(normalizado, normalizar_texto(clave))
    }
    return sorted(resultado)


def es_consulta_directa_anuncio(texto: str) -> bool:
    normalizado = normalizar_texto(texto)
    empieza_directo = any(
        normalizado.startswith(normalizar_texto(palabra))
        for palabra in PALABRAS_CONSULTA_DIRECTA
    )
    menciona_anuncio = any(
        frase in normalizado
        for frase in [
            "vi un anuncio", "vi una publicacion", "instagram",
            "facebook", "portal", "vi una propiedad",
            "vi una casa", "vi un apartamento",
        ]
    )
    return empieza_directo or menciona_anuncio
# ============================================================
# SESIONES Y MEMORIA
# ============================================================

def crear_sesion(sender: str) -> dict:
    return {
        "rol": None,
        "rol_confirmado": False,
        "confianza_rol": 0.0,
        "accion_pendiente_rol": None,
        "objetivo": "conversar",
        "estado_conversacion": "inicio",

        "filtros": {
            "tipo_operacion": None,
            "tipo_propiedad": None,
            "ciudad": None,
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
        "propiedad_activa_id": None,

        "esperando_codigo": False,
        "pregunta_pendiente": None,

        "lead": {
            "nombre": None,
            "correo": None,
            "whatsapp": None,
            "whatsapp_confirmado": False,
        },
        "motivo_contacto": None,
        "contacto_colega": {
            "nombre": None,
            "whatsapp": None,
        },
        "asunto_contacto_colega": None,
        "asunto_contacto_colega_escrito": False,
        "mensaje_contacto_colega": None,
        "lead_confirmacion_pendiente": False,
        "lead_confirmado": False,
        "lead_id": None,
        "agente_asignado": None,
        "notificacion_enviada": False,

        "numero_canal": (
            normalizar_telefono(sender)
            if SENDER_ES_WHATSAPP
            else None
        ),

        # Se conserva el historial completo mientras viva el proceso.
        "historial": [],
        "creado_en": datetime.utcnow().isoformat(),
        "actualizado_en": datetime.utcnow().isoformat(),
    }


def obtener_sesion(sender: str) -> dict:
    if sender not in sesiones:
        sesiones[sender] = crear_sesion(sender)
    return sesiones[sender]


def guardar_sesion(sender: str, estado: dict) -> None:
    estado["actualizado_en"] = datetime.utcnow().isoformat()
    sesiones[sender] = estado


def agregar_historial(
    estado: dict,
    rol: Literal["user", "assistant"],
    contenido: str,
) -> None:
    contenido = str(contenido or "").strip()
    if not contenido:
        return

    estado.setdefault("historial", []).append(
        {
            "role": rol,
            "content": contenido[:8000],
            "timestamp": datetime.utcnow().isoformat(),
        }
    )


def historial_para_ia(estado: dict) -> List[dict]:
    historial = estado.get("historial", [])

    # Aunque MAX_HISTORIAL tenga actualmente un valor bajo en Render,
    # se entregan a la IA al menos los últimos 60 mensajes.
    limite = max(MAX_HISTORIAL, 60)
    seleccion = historial[-limite:]

    mensajes: List[dict] = []
    caracteres_acumulados = 0
    limite_caracteres = 60000

    for mensaje in reversed(seleccion):
        if mensaje.get("role") not in {"user", "assistant"}:
            continue

        contenido = str(mensaje.get("content") or "").strip()
        if not contenido:
            continue

        if caracteres_acumulados + len(contenido) > limite_caracteres:
            break

        mensajes.append(
            {
                "role": mensaje["role"],
                "content": contenido,
            }
        )
        caracteres_acumulados += len(contenido)

    mensajes.reverse()
    return mensajes


def reiniciar_busqueda(estado: dict) -> dict:
    estado["filtros"] = {
        "tipo_operacion": None,
        "tipo_propiedad": None,
        "ciudad": None,
        "zona": None,
        "presupuesto_max": None,
        "habitaciones_min": None,
        "banos_min": None,
        "garajes_min": None,
        "caracteristicas": [],
    }
    estado["sin_preferencia"] = []
    estado["propiedades_enviadas"] = []
    estado["ultimo_lote"] = []
    estado["propiedad_interes"] = None
    estado["propiedad_activa_id"] = None
    estado["esperando_codigo"] = False
    estado["objetivo"] = "conversar"
    estado["estado_conversacion"] = "inicio"
    estado["pregunta_pendiente"] = None

    estado["contacto_colega"] = {
        "nombre": None,
        "whatsapp": None,
    }
    estado["asunto_contacto_colega"] = None
    estado["asunto_contacto_colega_escrito"] = False
    estado["mensaje_contacto_colega"] = None

    return estado


def mensaje_es_duplicado(sender: str, message_id: str) -> bool:
    ahora = time.time()

    for clave, expiracion in list(mensajes_duplicados.items()):
        if expiracion <= ahora:
            mensajes_duplicados.pop(clave, None)

    clave = f"{sender}:{message_id}"
    if clave in mensajes_duplicados:
        return True

    mensajes_duplicados[clave] = ahora + DUPLICATE_TTL_SECONDS
    return False
# ============================================================
# INVENTARIO WASI
# ============================================================

def inventario_necesita_actualizacion() -> bool:
    ultima = inventory_cache.get("ultima_actualizacion")
    if not inventory_cache.get("inventario") or not ultima:
        return True
    return datetime.utcnow() - ultima >= INTERVALO_ACTUALIZACION_WASI


def normalizar_propiedad_wasi(valor: Dict[str, Any]) -> dict:
    property_id = str(valor.get("id_property") or valor.get("id") or "")
    usuario = valor.get("user_data") or {}

    internas = convertir_caracteristicas_lista(
        valor.get("internal_features")
    )
    externas = convertir_caracteristicas_lista(
        valor.get("external_features")
    )
    generales = convertir_caracteristicas_lista(
        valor.get("features")
    )

    localidad = str(valor.get("location_label") or "").strip()
    zona = str(valor.get("zone_label") or "").strip()
    zona_combinada = " ".join(
        parte for parte in [localidad, zona] if parte
    ) or "N/D"

    captador = (
        f"{usuario.get('first_name', '')} "
        f"{usuario.get('last_name', '')}"
    ).strip()

    descripcion = str(valor.get("description") or "")
    observaciones = str(valor.get("observations") or "")

    return {
        "id": property_id,
        "titulo": valor.get("title") or "Propiedad Mettryc",
        "descripcion": descripcion,
        "observaciones": observaciones,

        "ciudad": valor.get("city_label") or "N/D",
        "zona": zona_combinada,
        "direccion_publica": valor.get("address") or "",

        "tipo_propiedad_wasi": valor.get("type_label") or "N/D",
        "estado_wasi": valor.get("status"),
        "activa": str(valor.get("status", "1")) not in {"0", "False", "false"},

        "precio_venta": parsear_precio_wasi(
            valor.get("sale_price"),
            valor.get("sale_price_label"),
        ),
        "precio_alquiler": parsear_precio_wasi(
            valor.get("rent_price"),
            valor.get("rent_price_label"),
        ),
        "precio_venta_label": valor.get("sale_price_label") or "N/D",
        "precio_alquiler_label": valor.get("rent_price_label") or "N/D",

        "area": extraer_area_principal_wasi(valor) or "N/D",
        "area_construida": convertir_float(
            valor.get("constructed_area")
            or valor.get("construction_area")
        ) or None,
        "area_terreno": convertir_float(
            valor.get("lot_area")
            or valor.get("land_area")
        ) or None,

        "habitaciones": valor.get("bedrooms") or "N/D",
        "banos": valor.get("bathrooms") or "N/D",
        "garajes": valor.get("garages") or "N/D",

        "caracteristicas_generales": generales,
        "caracteristicas_internas": internas,
        "caracteristicas_externas": externas,
        "caracteristicas_texto": " ".join(
            generales + internas + externas
        ),

        "captador_wasi": captador or "Asesor Mettryc",
        "telefono_captador_wasi": usuario.get("phone") or "",

        "imagenes": valor.get("galleries") or valor.get("images") or [],
        "video": valor.get("video") or valor.get("video_url"),
        "enlace": f"https://www.mettryc.com/inmueble/{property_id}",

        "actualizado_en": datetime.utcnow().isoformat(),
        "detalle_raw": valor,
    }


async def obtener_inventario_wasi() -> List[dict]:
    if not WASI_TOKEN or not WASI_COMPANY_ID:
        logger.error("Faltan WASI_TOKEN o WASI_COMPANY_ID.")
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
            propiedad = normalizar_propiedad_wasi(valor)

            if propiedad.get("id"):
                propiedades.append(propiedad)
                property_detail_cache[propiedad["id"]] = propiedad

        if cantidad_pagina < take:
            break

        skip += take
        await asyncio.sleep(0.2)

    logger.info("Inventario Wasi cargado: %s propiedades", len(propiedades))
    return propiedades


async def actualizar_inventario(force: bool = False) -> bool:
    if not force and not inventario_necesita_actualizacion():
        return False

    async with inventory_refresh_lock:
        if not force and not inventario_necesita_actualizacion():
            return False

        propiedades = await obtener_inventario_wasi()
        if not propiedades:
            logger.error(
                "Wasi no devolvió propiedades; se conserva el inventario anterior."
            )
            return False

        inventory_cache["inventario"] = propiedades
        inventory_cache["ultima_actualizacion"] = datetime.utcnow()
        reconstruir_catalogo_geografico()
        return True


def buscar_por_codigo(codigo: str) -> Optional[dict]:
    codigo = re.sub(r"\D", "", str(codigo or ""))
    if not codigo:
        return None

    for propiedad in inventory_cache.get("inventario", []):
        if str(propiedad.get("id")) == codigo:
            return deepcopy(propiedad)

    return None


async def consultar_detalle_propiedad_wasi(
    codigo: str,
) -> Optional[dict]:
    codigo = re.sub(r"\D", "", str(codigo or ""))
    if not codigo:
        return None

    propiedad_cache = property_detail_cache.get(codigo)
    if propiedad_cache:
        return deepcopy(propiedad_cache)

    propiedad_resumen = buscar_por_codigo(codigo)
    if propiedad_resumen:
        property_detail_cache[codigo] = propiedad_resumen

    # Se prueban formatos comunes de la API Wasi. Si el endpoint detallado
    # no está habilitado, se utiliza el resultado de property/search.
    endpoints = [
        f"https://api.wasi.co/v1/property/get/{codigo}",
        "https://api.wasi.co/v1/property/get",
    ]

    params = {
        "wasi_token": WASI_TOKEN,
        "id_company": WASI_COMPANY_ID,
        "id_property": codigo,
    }

    for endpoint in endpoints:
        try:
            respuesta = await http_client.get(
                endpoint,
                params=params,
                timeout=WASI_TIMEOUT,
            )
            respuesta.raise_for_status()
            payload = respuesta.json()

            detalle = None
            if isinstance(payload, dict):
                if payload.get("id_property") or payload.get("id"):
                    detalle = payload
                else:
                    for valor in payload.values():
                        if (
                            isinstance(valor, dict)
                            and str(
                                valor.get("id_property")
                                or valor.get("id")
                                or ""
                            ) == codigo
                        ):
                            detalle = valor
                            break

            if detalle:
                propiedad = normalizar_propiedad_wasi(detalle)
                property_detail_cache[codigo] = propiedad
                return deepcopy(propiedad)

        except Exception as exc:
            logger.debug(
                "Detalle Wasi no disponible endpoint=%s tipo=%s",
                endpoint,
                type(exc).__name__,
            )

    return deepcopy(propiedad_resumen) if propiedad_resumen else None


# ============================================================
# CATÁLOGO GEOGRÁFICO
# ============================================================

FALLBACK_ZONAS_AMBIGUAS = {
    "el trigal": ["Valencia", "Cabudare"],
    "trigal": ["Valencia", "Cabudare"],
    "trigal norte": ["Valencia", "Cabudare"],
    "los caobos": ["Valencia", "Caracas"],
    "centro": ["Valencia", "Barquisimeto", "Caracas"],
}
def reconstruir_catalogo_geografico() -> None:
    ciudades_norm: Dict[str, str] = {}
    zonas_norm: Dict[str, Set[str]] = {}
    zonas_por_ciudad: Dict[str, Set[str]] = {}

    for propiedad in inventory_cache.get("inventario", []):
        ciudad = str(propiedad.get("ciudad") or "").strip()
        zona_completa = str(propiedad.get("zona") or "").strip()
        ciudad_norm = normalizar_texto(ciudad)

        if ciudad_norm:
            ciudades_norm.setdefault(ciudad_norm, ciudad)

        for zona in re.split(r"[\/|·–,]", zona_completa):
            zona = zona.strip()
            zona_norm = normalizar_texto(zona)

            if len(zona_norm) < 4:
                continue

            zonas_norm.setdefault(zona_norm, set()).add(zona)

            if ciudad:
                zonas_por_ciudad.setdefault(
                    zona_norm,
                    set(),
                ).add(ciudad)

    catalogo_geografico["ciudades_norm"] = ciudades_norm
    catalogo_geografico["zonas_norm"] = zonas_norm
    catalogo_geografico["zonas_por_ciudad"] = zonas_por_ciudad

    catalogo_geografico["frases_ciudades"] = sorted(
        ciudades_norm.items(),
        key=lambda elemento: len(elemento[0]),
        reverse=True,
    )

    frases_zonas: List[Tuple[str, str]] = []

    for zona_norm, variantes in zonas_norm.items():
        preferida = sorted(variantes, key=len)[0]
        frases_zonas.append((zona_norm, preferida))

    catalogo_geografico["frases_zonas"] = sorted(
        frases_zonas,
        key=lambda elemento: len(elemento[0]),
        reverse=True,
    )


def obtener_ciudades_para_zona(zona: Optional[str]) -> Set[str]:
    zona_norm = normalizar_texto(zona)
    if not zona_norm:
        return set()

    ciudades = set(
        catalogo_geografico["zonas_por_ciudad"].get(
            zona_norm,
            set(),
        )
    )

    tokens_objetivo = tokens_zona(zona_norm)

    for zona_catalogo, ciudades_catalogo in (
        catalogo_geografico["zonas_por_ciudad"].items()
    ):
        tokens_catalogo = tokens_zona(zona_catalogo)

        if (
            tokens_objetivo
            and tokens_catalogo
            and (
                tokens_objetivo.issubset(tokens_catalogo)
                or tokens_catalogo.issubset(tokens_objetivo)
            )
        ):
            ciudades.update(ciudades_catalogo)

    if not ciudades:
        ciudades.update(FALLBACK_ZONAS_AMBIGUAS.get(zona_norm, []))

    return ciudades


def detectar_zona_ciudad(
    texto: str,
) -> Dict[str, Any]:
    normalizado = normalizar_texto(texto)
    resultado: Dict[str, Any] = {}

    if not normalizado:
        return resultado

    # Primero se detectan municipios y ciudades conocidos.
    # Esto evita interpretar Naguanagua y San Diego como zonas
    # pertenecientes a Valencia.
    ciudad_explicita = detectar_ciudad_canonica(
        normalizado
    )

    if ciudad_explicita:
        resultado["ciudad"] = ciudad_explicita

    # Si no se encontró una ciudad canónica, se consulta el
    # catálogo dinámico construido desde Wasi.
    if not ciudad_explicita:
        for ciudad_norm, ciudad in (
            catalogo_geografico["frases_ciudades"]
        ):
            if contiene_termino(
                normalizado,
                ciudad_norm,
            ):
                resultado["ciudad"] = ciudad
                break

    zona_detectada = None

    for zona_norm, zona in (
        catalogo_geografico["frases_zonas"]
    ):
        if contiene_termino(
            normalizado,
            zona_norm,
        ):
            zona_detectada = zona
            resultado["zona"] = zona
            break

    # Si el término detectado como zona es exactamente igual a
    # la ciudad o municipio, se conserva como ciudad y no como
    # zona específica.
    if (
        ciudad_explicita
        and resultado.get("zona")
        and normalizar_texto(resultado["zona"])
        == normalizar_texto(ciudad_explicita)
    ):
        resultado.pop("zona", None)
        zona_detectada = None

    # Si existe una ciudad explícita, nunca debe ser reemplazada
    # por las ciudades asociadas a una zona del catálogo.
    if ciudad_explicita:
        resultado["ciudad"] = ciudad_explicita
        resultado.pop("ambiguedad", None)
        resultado.pop("ciudades_posibles", None)
        return resultado

    if zona_detectada:
        ciudades = sorted(
            obtener_ciudades_para_zona(
                zona_detectada
            )
        )

        if len(ciudades) == 1:
            resultado.setdefault(
                "ciudad",
                ciudades[0],
            )

        elif len(ciudades) > 1:
            ciudad_detectada_norm = normalizar_texto(
                resultado.get("ciudad")
            )
            ciudades_norm = {
                normalizar_texto(ciudad)
                for ciudad in ciudades
            }

            if ciudad_detectada_norm not in ciudades_norm:
                resultado.pop("ciudad", None)
                resultado["ambiguedad"] = True
                resultado["ciudades_posibles"] = ciudades

    if "zona" not in resultado:
        for zona_alias, ciudades in (
            FALLBACK_ZONAS_AMBIGUAS.items()
        ):
            if contiene_termino(
                normalizado,
                zona_alias,
            ):
                resultado["zona"] = normalizar_nombre(
                    zona_alias
                )
                resultado["ciudades_posibles"] = ciudades

                ciudad_detectada_norm = normalizar_texto(
                    resultado.get("ciudad")
                )
                ciudades_norm = {
                    normalizar_texto(ciudad)
                    for ciudad in ciudades
                }

                if ciudad_detectada_norm not in ciudades_norm:
                    resultado.pop("ciudad", None)
                    resultado["ambiguedad"] = True

                break

    return resultado


# ============================================================
# GOOGLE SHEETS, AGENTES Y CAPTADORES
# ============================================================

def sheets_necesita_actualizacion() -> bool:
    ultima = sheets_cache.get("ultima_actualizacion")
    if not ultima:
        return True
    return datetime.utcnow() - ultima >= INTERVALO_ACTUALIZACION_SHEETS


def agregar_captador_sheet(
    resultado: Dict[str, str],
    nombre: Any,
    telefono: Any,
) -> None:
    nombre_limpio = str(nombre or "").strip()
    telefono_limpio = normalizar_telefono(telefono)

    if nombre_limpio and telefono_limpio:
        resultado[nombre_limpio] = telefono_limpio


def procesar_captadores_sheet(payload: Any) -> Dict[str, str]:
    captadores: Dict[str, str] = {}

    if isinstance(payload, dict):
        for nombre, contenido in payload.items():
            if isinstance(contenido, dict):
                agregar_captador_sheet(
                    captadores,
                    contenido.get("nombre") or nombre,
                    contenido.get("telefono")
                    or contenido.get("phone")
                    or contenido.get("whatsapp"),
                )
            else:
                agregar_captador_sheet(
                    captadores,
                    nombre,
                    contenido,
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


async def sincronizar_google_sheet(force: bool = False) -> bool:
    if not GOOGLE_SHEET_TURNOS_URL:
        logger.warning("GOOGLE_SHEET_TURNOS_URL no está configurada.")
        return False

    if not force and not sheets_necesita_actualizacion():
        return False

    async with sheets_refresh_lock:
        if not force and not sheets_necesita_actualizacion():
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
                    "Google Sheets no devolvió un objeto JSON."
                )

            agentes = payload.get("agentes", [])
            if not isinstance(agentes, list):
                agentes = []

            captadores = procesar_captadores_sheet(
                payload.get("captadores", {})
            )

            if not captadores:
                captadores = procesar_captadores_sheet(
                    payload.get("captadores_data")
                    or payload.get("asesores")
                    or []
                )

            sheets_cache["agentes"] = agentes
            sheets_cache["captadores"] = captadores
            sheets_cache["ultima_actualizacion"] = datetime.utcnow()

            logger.info(
                "Sheets sincronizado: agentes=%s captadores=%s",
                len(agentes),
                len(captadores),
            )
            return True

        except Exception as exc:
            logger.error(
                "Error sincronizando Sheets tipo=%s detalle=%s",
                type(exc).__name__,
                str(exc)[:200],
            )
            return False
def cruzar_captador_con_sheet(nombre_wasi: str) -> dict:
    nombre_normalizado = normalizar_texto(nombre_wasi)
    captadores = sheets_cache.get("captadores", {})

    for nombre_sheet, telefono in captadores.items():
        if normalizar_texto(nombre_sheet) == nombre_normalizado:
            return {
                "nombre": nombre_sheet,
                "telefono": telefono,
                "tipo_coincidencia": "exacta",
            }

    tokens_wasi = tokens_nombre(nombre_wasi)
    mejor: Optional[dict] = None
    mejor_score = 0.0

    for nombre_sheet, telefono in captadores.items():
        tokens_sheet = tokens_nombre(nombre_sheet)

        if not tokens_wasi or not tokens_sheet:
            continue

        interseccion = tokens_wasi.intersection(tokens_sheet)
        union = tokens_wasi.union(tokens_sheet)

        score_jaccard = (
            len(interseccion) / len(union)
            if union
            else 0.0
        )
        cobertura = (
            len(interseccion) / len(tokens_wasi)
            if tokens_wasi
            else 0.0
        )
        score = max(score_jaccard, cobertura)

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
    await sincronizar_google_sheet()

    agentes = [
        deepcopy(agente)
        for agente in sheets_cache.get("agentes", [])
        if (
            isinstance(agente, dict)
            and (agente.get("nombre") or agente.get("name"))
            and str(agente.get("activo", "true")).lower()
            not in {"false", "0", "no", "inactivo"}
        )
    ]

    if not agentes:
        return None

    global round_robin_index

    async with round_robin_lock:
        round_robin_index = (
            round_robin_index + 1
        ) % len(agentes)
        agente = agentes[round_robin_index]

    if not agente.get("nombre"):
        agente["nombre"] = agente.get("name")

    logger.info(
        "Round robin índice=%s agente=%s",
        round_robin_index,
        agente.get("nombre"),
    )
    return agente


# ============================================================
# BASE DE CONOCIMIENTOS METTRYC
# ============================================================

BASE_CONOCIMIENTO_METTRYC = {
    "descripcion": (
        "Mettryc Realty es la Primera Tecnoinmobiliaria de Venezuela."
    ),
    "honorarios": {
        "ventas": "Los honorarios inmobiliarios son del 5% en ventas.",
        "alquiler": (
            "Los honorarios inmobiliarios corresponden a un mes "
            "de canon en alquiler."
        ),
    },
    "oficinas": [
        {
            "ciudad": "Valencia",
            "direccion": (
                "CC Patio Trigal, local 300-6, Valencia, Carabobo."
            ),
        },
        {
            "ciudad": "San Diego",
            "direccion": (
                "CC Metroplaza, sector Los Jarales, San Diego."
            ),
        },
        {
            "ciudad": "Barquisimeto",
            "direccion": (
                "Av. Los Leones, Torre Bel, piso 4, oficina 4-6."
            ),
        },
    ],
    "negociacion": (
        "Si el precio es negociable, el interesado puede presentar "
        "su mejor oferta para que sea evaluada por el propietario."
    ),
    "privacidad": (
        "Mettryc Realty no comparte el teléfono directo de los "
        "propietarios."
    ),
    "reclutamiento": {
        "costo": "El ingreso tiene un costo de $50.",
        "incluye": "Incluye curso y credenciales.",
        "formulario": "https://forms.gle/SbLtHrey69fhf3Xt8",
    },
}


def construir_contexto_conocimiento() -> str:
    return json.dumps(
        BASE_CONOCIMIENTO_METTRYC,
        ensure_ascii=False,
        indent=2,
    )


# ============================================================
# OPENROUTER
# ============================================================

PROMPT_MAESTRO = """
Eres Paty, asesora virtual de Mettryc Realty, la Primera
Tecnoinmobiliaria de Venezuela.

Eres quien dirige la conversación. Hablas en español venezolano de
forma cálida, humana, profesional, breve y natural.

Debes analizar cada mensaje usando todo el historial y el estado
comercial entregado. Extrae todos los datos e intenciones presentes,
aunque el usuario los diga en desorden o incluya varias solicitudes
en el mismo mensaje.

REGLAS PRINCIPALES

1. No inventes propiedades, precios, disponibilidad, códigos,
   características, agentes, captadores ni enlaces.
2. El programa ejecuta las herramientas y construye las fichas.
3. Nunca preguntes de nuevo un dato que ya aparezca en el estado.
4. Puedes responder una pregunta y continuar naturalmente el flujo.
5. Haz una sola pregunta principal por turno, salvo que sea natural
   pedir nombre, correo y WhatsApp juntos.
6. Si el usuario solo saluda, responde el saludo y pregunta cómo
   puedes ayudarlo. No inicies un interrogatorio inmobiliario.
7. Si solicita hablar con una persona, agente, asesor o humano,
   usa hablar_con_humano inmediatamente.
8. Si quiere visitar una propiedad, usa agendar_visita.
9. Si pregunta algo sobre una ficha mostrada, usa
   consultar_propiedad e incluye la pregunta completa.
10. Si hay varias propiedades y no se identifica cuál desea visitar
    o consultar, usa pedir_aclaracion.
11. Después de mostrar propiedades, el sistema preguntará si quiere
    agendar una visita o hacer una pregunta sobre ellas.
12. Si cambia un requisito, extrae el valor nuevo.
13. Si dice cualquiera, no importa, sin límite o me da igual,
    registra el campo en campos_sin_preferencia.
"""
PROMPT_MAESTRO += """

ROLES

- Usa colega_inmobiliario únicamente si la persona dice que es
  asesor, agente, broker, corredor, realtor, colega o que busca para
  un cliente.
- Pedir hablar con un asesor no convierte a la persona en colega.
- Si es colega, nunca solicites datos personales de su cliente.
- Los colegas reciben los datos del captador en las fichas.
- Si un colega solicita atención humana, el sistema notificará
  solamente a los administradores.
- Los clientes que soliciten visita o atención humana entran en el
  flujo de captura de lead y round robin.

BÚSQUEDA

Para un cliente, los criterios mínimos son:
- tipo de operación;
- tipo de propiedad;
- ciudad;
- zona.

Para un colega basta normalmente con:
- tipo de operación;
- tipo de propiedad;
- ciudad;
- zona.

REGLAS ESPECIALES PARA COLEGAS

- Si la persona se identifica como colega, asesor, agente, corredor,
  broker o realtor, conserva ese rol durante toda la conversación.
- Antes de buscar para un colega, intenta obtener:
  tipo de operación, tipo de propiedad, ciudad, zona y presupuesto.
- Si no tiene preferencia de zona o presupuesto, registra el campo
  correspondiente en campos_sin_preferencia.
- Pregunta por habitaciones o características especiales junto con
  la pregunta de presupuesto, pero no las hagas obligatorias.
- Cuando los criterios estén listos, usa buscar_propiedades.
- El programa enviará cinco opciones.
- Todas las fichas para colegas deben incluir nombre y WhatsApp del
  captador.
- Nunca solicites datos personales del cliente del colega.
- Nunca conviertas a un colega en cliente en mensajes posteriores.

ROL OBLIGATORIO ANTES DEL CONTACTO

- Antes de capturar datos personales, asignar un agente, iniciar
  round robin, entregar datos de un captador o coordinar una visita,
  el rol debe estar confirmado explícitamente.
- La pregunta obligatoria es:
  "¿Buscas la propiedad para ti o para un cliente?"
- Si responde para sí mismo, el rol es cliente.
- Si responde para un cliente, el rol es colega_inmobiliario.
- Mostrar una ficha de Mercado Libre o de un anuncio no requiere
  conocer todavía el rol.
- Si después de ver la ficha desea agendar una visita, primero
  confirma el rol.
- Cliente que desea visitar: captura de lead y round robin.
- Colega que desea visitar: entrega nombre y WhatsApp del captador.
- Nunca asumas que una persona es cliente solo porque no ha dicho
  que es colega.

CONTACTO DE COLEGAS

- Si un colega proporciona su nombre, extráelo en
  actualizaciones.nombre.
- Si proporciona su WhatsApp, extráelo en
  actualizaciones.whatsapp.
- Estos datos pertenecen al colega, no al cliente del colega.
- Si un colega solicita atención humana, el programa usará esos
  datos para notificar únicamente a los administradores.

Presupuesto, habitaciones, baños, garajes y características son
preferencias opcionales tanto para clientes como colegas.

ANUNCIOS

- Si recibe un código o enlace de Mettryc, usa buscar_por_codigo.
- Si recibe una URL de Mercado Libre, el programa extraerá el código
  y mostrará la ficha.
- Si pregunta precio, información o disponibilidad de un anuncio sin
  código, usa pedir_codigo_inmueble.
- El código suele aparecer al final del título o en la descripción.

PROPIEDADES

- Si dice primera, segunda, tercera, cuarta o quinta, registra la
  posición.
- Si pide más opciones, usa mostrar_mas_propiedades.
- Si pregunta por características de una propiedad, usa
  consultar_propiedad.
- Si Wasi no especifica algo, debes decir que no está especificado.
  Nunca concluyas que una propiedad no tiene una característica solo
  porque no aparece registrada.

CAPTURA DE LEAD

Extrae nombre, correo y WhatsApp de cualquier mensaje.
Si quiere usar el número del chat, establece usar_numero_actual.
No vuelvas a pedir información existente.

SOLICITUD DEL CAPTADOR

- Si el usuario pregunta quién es el captador, pide su contacto,
  teléfono o WhatsApp, usa solicitar_captador.
- Nunca respondas una solicitud del captador como una pregunta
  genérica sobre la propiedad.
- Antes de revelar datos del captador, el rol debe estar confirmado.
- Si es colega, el sistema consultará Wasi y Google Sheets.
- Si es cliente, no reveles el captador; ofrece atención de un
  agente de Mettryc Realty.
- Si el cliente acepta, inicia captura de lead.

MENSAJES DE INFORMACIÓN

- Si el usuario escribe solamente “Información” o una frase ambigua
  equivalente, pregunta si desea información sobre una propiedad,
  sobre Mettryc Realty o sobre cómo trabajar con nosotros.
- Si pregunta precio, disponibilidad, canon o información de un
  anuncio sin código ni propiedad activa, usa pedir_codigo_inmueble.
- Si ya existe una propiedad activa, responde usando esa propiedad
  sin volver a solicitar el código.

RESPUESTAS CORTAS Y CONTEXTO

- Interpreta siempre respuestas como sí, no, ok, está bien, fino,
  perfecto, listo, dale o claro usando la última pregunta que hizo
  Paty.
- Una respuesta corta nunca debe analizarse de forma aislada.
- Si la pregunta anterior ofrecía dos alternativas y el usuario
  responde solamente sí, ok o perfecto, no selecciones una por él.
  Pregunta cuál de las alternativas prefiere usando palabras
  diferentes.
- No repitas literalmente la misma pregunta.
- Si la respuesta permite ejecutar una acción inequívoca, continúa
  el flujo sin volver a pedir confirmación.
- Si no permite identificar una opción concreta, reformula la
  pregunta de forma breve y natural.

ACCIONES DISPONIBLES

- responder
- buscar_propiedades
- mostrar_mas_propiedades
- buscar_por_codigo
- consultar_propiedad
- seleccionar_propiedad
- agendar_visita
- hablar_con_humano
- pedir_codigo_inmueble
- consultar_mettryc
- reiniciar_busqueda
- pedir_aclaracion

Devuelve exclusivamente el JSON solicitado.
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
    modelo_pydantic: Type[BaseModel],
    mensajes: List[dict],
    temperatura: float = 0.15,
    max_tokens: int = 1200,
) -> Optional[BaseModel]:
    if not OPENROUTER_API_KEY or not http_client:
        return None

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://www.mettryc.com",
        "X-Title": "Mettryc Realty Paty",
    }

    modelos = list(
        dict.fromkeys(
            [
                MODELO_AGENTE_PRINCIPAL,
                MODELO_AGENTE_RESPALDO,
            ]
        )
    )

    for modelo in modelos:
        formatos = [
            {
                "type": "json_schema",
                "json_schema": {
                    "name": modelo_pydantic.__name__,
                    "strict": True,
                    "schema": modelo_pydantic.model_json_schema(),
                },
            },
            {"type": "json_object"},
        ]

        for response_format in formatos:
            payload = {
                "model": modelo,
                "messages": mensajes,
                "temperature": temperatura,
                "max_tokens": max_tokens,
                "response_format": response_format,
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
                        elemento.get("text", "")
                        for elemento in contenido
                        if isinstance(elemento, dict)
                    )

                contenido = limpiar_json_modelo(contenido)
                return modelo_pydantic.model_validate_json(contenido)

            except (
                ValidationError,
                ValueError,
                httpx.HTTPError,
            ) as exc:
                logger.warning(
                    "OpenRouter modelo=%s formato=%s tipo=%s",
                    modelo,
                    response_format.get("type"),
                    type(exc).__name__,
                )

            except Exception as exc:
                logger.warning(
                    "OpenRouter modelo=%s tipo=%s detalle=%s",
                    modelo,
                    type(exc).__name__,
                    str(exc)[:160],
                )

    return None


def resumen_propiedad_para_ia(propiedad: Optional[dict]) -> Optional[dict]:
    if not propiedad:
        return None

    return {
        "id": propiedad.get("id"),
        "titulo": propiedad.get("titulo"),
        "ciudad": propiedad.get("ciudad"),
        "zona": propiedad.get("zona"),
        "tipo": propiedad.get("tipo_propiedad_wasi"),
    }


def construir_estado_para_ia(estado: dict) -> dict:
    ultimo_lote = []

    for indice, property_id in enumerate(
        estado.get("ultimo_lote", []),
        start=1,
    ):
        propiedad = buscar_por_codigo(property_id)
        if propiedad:
            ultimo_lote.append(
                {
                    "posicion": indice,
                    **resumen_propiedad_para_ia(propiedad),
                }
            )

    return {
        "rol": estado.get("rol"),
        "confianza_rol": estado.get("confianza_rol"),
        "objetivo": estado.get("objetivo"),
        "estado_conversacion": estado.get("estado_conversacion"),
        "filtros": estado.get("filtros"),
        "sin_preferencia": estado.get("sin_preferencia"),
        "esperando_codigo": estado.get("esperando_codigo"),
        "pregunta_pendiente": estado.get("pregunta_pendiente"),
        "ultimo_lote": ultimo_lote,
        "propiedad_interes": resumen_propiedad_para_ia(
            estado.get("propiedad_interes")
        ),
        "detalle_pregunta_pendiente": estado.get(
            "detalle_pregunta_pendiente"
        ),
        "ultimo_mensaje_asistente": (
            ultima_respuesta_asistente(estado)
        ),
        "lead": {
            "nombre": estado.get("lead", {}).get("nombre"),
            "correo": estado.get("lead", {}).get("correo"),
            "whatsapp_disponible": bool(
                estado.get("lead", {}).get("whatsapp")
            ),
            "numero_canal_disponible": bool(
                estado.get("numero_canal")
            ),
            "confirmacion_pendiente": estado.get(
                "lead_confirmacion_pendiente",
                False,
            ),
        },
    }
async def decidir_con_ia(
    mensaje: str,
    estado: dict,
) -> DecisionAgente:
    contexto = {
        "estado_comercial": construir_estado_para_ia(estado),
        "mensaje_actual": mensaje,
        "base_conocimiento_mettryc": BASE_CONOCIMIENTO_METTRYC,
    }

    mensajes = [
        {
            "role": "system",
            "content": PROMPT_MAESTRO,
        },
        *historial_para_ia(estado),
        {
            "role": "user",
            "content": (
                "Analiza el mensaje actual, extrae todos los valores "
                "y selecciona la acción más lógica.\n\n"
                + json.dumps(contexto, ensure_ascii=False)
            ),
        },
    ]

    resultado = await llamar_openrouter_json(
        DecisionAgente,
        mensajes,
        temperatura=0.15,
        max_tokens=1400,
    )

    if isinstance(resultado, DecisionAgente):
        return resultado

    return decision_fallback(mensaje, estado)


def decision_fallback(
    mensaje: str,
    estado: dict,
) -> DecisionAgente:
    codigo = (
        extraer_codigo_mercadolibre(mensaje)
        or extraer_codigo_inmueble(mensaje)
    )
    posicion = detectar_posicion(mensaje)
    rol = detectar_rol_explicito(mensaje)

    if solicita_humano(mensaje):
        return DecisionAgente(
            rol=rol,
            confianza_rol=1.0 if rol else 0.0,
            intencion_principal="hablar_con_humano",
            solicitudes=SolicitudesUsuario(
                quiere_hablar_con_humano=True
            ),
            accion=AccionAgente(tipo="hablar_con_humano"),
        )

    if solicita_visita(mensaje):
        return DecisionAgente(
            rol=rol,
            confianza_rol=1.0 if rol else 0.0,
            intencion_principal="agendar_visita",
            referencia_propiedad=ReferenciaPropiedad(
                posicion=posicion
            ),
            solicitudes=SolicitudesUsuario(
                quiere_agendar_visita=True
            ),
            accion=AccionAgente(
                tipo="agendar_visita",
                posicion=posicion,
            ),
        )

    if codigo:
        return DecisionAgente(
            rol=rol,
            confianza_rol=1.0 if rol else 0.0,
            intencion_principal="consulta_inmueble",
            referencia_propiedad=ReferenciaPropiedad(
                codigo=codigo
            ),
            accion=AccionAgente(
                tipo="buscar_por_codigo",
                codigo=codigo,
            ),
        )

    if pide_mas_opciones(mensaje):
        return DecisionAgente(
            rol=rol,
            confianza_rol=1.0 if rol else 0.0,
            intencion_principal="mas_opciones",
            solicitudes=SolicitudesUsuario(
                quiere_mas_opciones=True
            ),
            accion=AccionAgente(
                tipo="mostrar_mas_propiedades"
            ),
        )

    if posicion and estado.get("ultimo_lote"):
        return DecisionAgente(
            rol=rol,
            confianza_rol=1.0 if rol else 0.0,
            intencion_principal="seleccionar_propiedad",
            referencia_propiedad=ReferenciaPropiedad(
                posicion=posicion
            ),
            accion=AccionAgente(
                tipo="seleccionar_propiedad",
                posicion=posicion,
            ),
        )

    if es_consulta_directa_anuncio(mensaje):
        return DecisionAgente(
            rol=rol,
            confianza_rol=1.0 if rol else 0.0,
            intencion_principal="anuncio_sin_codigo",
            accion=AccionAgente(
                tipo="pedir_codigo_inmueble"
            ),
        )

    return DecisionAgente(
        mensaje=(
            "¡Hola! Soy Paty, asesora virtual de Mettryc Realty. "
            "¿Cómo puedo ayudarte?"
        ),
        rol=rol,
        confianza_rol=1.0 if rol else 0.0,
        intencion_principal="conversar",
        accion=AccionAgente(tipo="responder"),
    )

def es_agradecimiento_simple(texto: str) -> bool:
    normalizado = normalizar_texto(texto)

    agradecimientos = {
        "gracias",
        "muchas gracias",
        "mil gracias",
        "gracias por la informacion",
        "gracias por la información",
        "perfecto gracias",
        "ok gracias",
        "vale gracias",
        "excelente gracias",
        "muy amable",
        "muchisimas gracias",
    }

    return normalizado in {
        normalizar_texto(valor)
        for valor in agradecimientos
    }


def respuesta_agradecimiento() -> str:
    return (
        "¡Con mucho gusto! Estoy aquí para ayudarte. "
        "Si necesitas algo más sobre Mettryc Realty o alguna "
        "propiedad, solo dime."
    )

    # --------------------------------------------------------
    # SOLICITUD CONTROLADA DE DATOS DEL CAPTADOR
    # --------------------------------------------------------

    if solicita_datos_captador(texto):
        posicion_captador = detectar_posicion(
            texto
        )

        respuesta = await atender_solicitud_captador(
            estado,
            posicion=posicion_captador,
            codigo=None,
        )
        return await finalizar(respuesta)

def es_pregunta_sobre_propiedad_activa(
    texto: str,
    estado: dict,
) -> bool:
    tiene_contexto = bool(
        estado.get("propiedad_interes")
        or estado.get("propiedad_activa_id")
        or len(estado.get("ultimo_lote", [])) == 1
    )

    if not tiene_contexto:
        return False

    normalizado = normalizar_texto(texto)

    indicadores = [
        "cuanto",
        "cuantos",
        "cuanta",
        "cuantas",
        "tiene",
        "posee",
        "incluye",
        "negociacion",
        "condiciones",
        "deposito",
        "depositos",
        "adelanto",
        "comision",
        "contrato",
        "amoblado",
        "amoblada",
        "techado",
        "techados",
        "planta electrica",
        "pozo",
        "mascotas",
        "servicios",
        "condominio",
        "disponible",
        "esa propiedad",
        "ese inmueble",
        "esa casa",
        "ese apartamento",
        "ese alquiler",
        "esa opcion",
    ]

    return (
        "?" in texto
        or any(indicador in normalizado for indicador in indicadores)
    )


def interpretar_respuesta_rol(
    texto: str,
    estado: dict,
) -> Optional[str]:
    normalizado = normalizar_texto(texto)
    pregunta_pendiente = estado.get(
        "pregunta_pendiente"
    )

    # --------------------------------------------------------
    # RESPUESTA A LA PREGUNTA:
    # "¿Buscas la propiedad para ti o para un cliente?"
    # --------------------------------------------------------

    if pregunta_pendiente == "confirmar_rol":
        respuestas_para_cliente_tercero = {
            "cliente",
            "un cliente",
            "una cliente",
            "para cliente",
            "para un cliente",
            "para una cliente",
            "para mi cliente",
            "para nuestro cliente",
            "es para un cliente",
            "es para una cliente",
            "es para mi cliente",
            "mi cliente",
        }

        respuestas_para_si_mismo = {
            "para mi",
            "para mí",
            "yo",
            "para yo",
            "personal",
            "uso personal",
            "para uso personal",
            "es para mi",
            "es para mí",
            "para nosotros",
            "para mi familia",
            "para mi esposa",
            "para mi esposo",
            "para mi pareja",
            "para mi mama",
            "para mi mamá",
            "para mi madre",
            "para mi padre",
        }

        respuestas_cliente_normalizadas = {
            normalizar_texto(respuesta)
            for respuesta in respuestas_para_cliente_tercero
        }

        respuestas_personales_normalizadas = {
            normalizar_texto(respuesta)
            for respuesta in respuestas_para_si_mismo
        }

        if normalizado in respuestas_cliente_normalizadas:
            estado["rol"] = "colega_inmobiliario"
            estado["rol_confirmado"] = True
            estado["confianza_rol"] = 1.0
            estado["pregunta_pendiente"] = None
            return "colega_inmobiliario"

        if normalizado in respuestas_personales_normalizadas:
            estado["rol"] = "cliente"
            estado["rol_confirmado"] = True
            estado["confianza_rol"] = 1.0
            estado["pregunta_pendiente"] = None
            return "cliente"

    # --------------------------------------------------------
    # DETECCIÓN EXPLÍCITA EN CUALQUIER MOMENTO
    # --------------------------------------------------------

    rol = detectar_rol_explicito(texto)

    if rol:
        estado["rol"] = rol
        estado["rol_confirmado"] = True
        estado["confianza_rol"] = 1.0
        estado["pregunta_pendiente"] = None
        return rol

    # --------------------------------------------------------
    # RESPUESTAS PERSONALES BREVES
    # --------------------------------------------------------

    if normalizado in {
        "yo",
        "personal",
        "uso personal",
        "para mi",
        "para mí",
        "es para mi",
        "es para mí",
        "es mia",
        "es mía",
        "es mio",
        "es mío",
        "es para nosotros",
    }:
        estado["rol"] = "cliente"
        estado["rol_confirmado"] = True
        estado["confianza_rol"] = 1.0
        estado["pregunta_pendiente"] = None
        return "cliente"

    return None

def rol_esta_confirmado(estado: dict) -> bool:
    return bool(
        estado.get("rol")
        in {
            "cliente",
            "colega_inmobiliario",
        }
        and estado.get("rol_confirmado", False)
    )


def mensaje_confirmacion_rol() -> str:
    return (
        "Antes de continuar, ¿buscas la propiedad para ti "
        "o para un cliente?"
    )


def solicitar_rol_para_accion(
    estado: dict,
    accion: str,
    *,
    propiedad_id: Optional[str] = None,
    posicion: Optional[int] = None,
    mensaje_original: Optional[str] = None,
) -> str:
    estado["accion_pendiente_rol"] = {
        "tipo": accion,
        "propiedad_id": propiedad_id,
        "posicion": posicion,
        "mensaje_original": mensaje_original,
    }
    estado["pregunta_pendiente"] = "confirmar_rol"
    estado["estado_conversacion"] = "esperando_rol"

    return mensaje_confirmacion_rol()

def normalizar_campo_sin_preferencia(
    campo: str,
) -> Optional[str]:
    texto = normalizar_texto(campo).replace(" ", "_")

    equivalencias = {
        "presupuesto": "presupuesto_max",
        "precio": "presupuesto_max",
        "habitaciones": "habitaciones_min",
        "cuartos": "habitaciones_min",
        "banos": "banos_min",
        "bano": "banos_min",
        "garajes": "garajes_min",
        "puestos": "garajes_min",
        "ubicacion": "zona",
        "tipo": "tipo_propiedad",
        "caracteristica": "caracteristicas",
    }

    texto = equivalencias.get(texto, texto)

    validos = {
        "tipo_operacion",
        "tipo_propiedad",
        "ciudad",
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
    hubo_cambio = False
    rol_explicito = detectar_rol_explicito(mensaje)

    if rol_explicito:
        estado["rol"] = rol_explicito
        estado["rol_confirmado"] = True
        estado["confianza_rol"] = 1.0

    elif (
        decision.rol
        and decision.confianza_rol >= 0.75
        and not estado.get("rol")
    ):
        estado["rol"] = decision.rol
        estado["confianza_rol"] = decision.confianza_rol

    actualizaciones = decision.actualizaciones.model_dump()

    ciudad_explicita = detectar_ciudad_canonica(
        mensaje
    )

    if ciudad_explicita:
        actualizaciones["ciudad"] = ciudad_explicita

        zona_ia = actualizaciones.get("zona")

        if (
            zona_ia
            and normalizar_texto(zona_ia)
            == normalizar_texto(ciudad_explicita)
        ):
            actualizaciones["zona"] = None


    filtros = estado.setdefault("filtros", {})

    campos_busqueda = [
        "tipo_operacion",
        "tipo_propiedad",
        "ciudad",
        "zona",
        "presupuesto_max",
        "habitaciones_min",
        "banos_min",
        "garajes_min",
    ]

    for campo in campos_busqueda:
        valor = actualizaciones.get(campo)

        if valor in (None, ""):
            continue

        if campo == "tipo_propiedad":
            valor = normalizar_tipo_propiedad(valor)

        anterior = filtros.get(campo)

        # La IA completa campos vacíos, pero no sobrescribe filtros
        # confirmados usando información recuperada del contexto.
        if anterior not in (None, "") and anterior != valor:
            continue

        if anterior != valor:
            filtros[campo] = valor
            hubo_cambio = True

        if campo in estado.get("sin_preferencia", []):
            estado["sin_preferencia"].remove(campo)

    caracteristicas = actualizaciones.get("caracteristicas") or []

    if caracteristicas:
        existentes = {
            normalizar_texto(caracteristica): caracteristica
            for caracteristica in filtros.get(
                "caracteristicas",
                [],
            )
            if normalizar_texto(caracteristica)
        }

        for caracteristica in caracteristicas:
            clave = normalizar_texto(caracteristica)

            if clave:
                existentes.setdefault(clave, caracteristica)

        lista_nueva = list(existentes.values())

        if lista_nueva != filtros.get("caracteristicas", []):
            filtros["caracteristicas"] = lista_nueva
            hubo_cambio = True

    for campo_original in decision.campos_sin_preferencia:
        campo = normalizar_campo_sin_preferencia(
            campo_original
        )

        if not campo:
            continue

        if campo not in estado["sin_preferencia"]:
            estado["sin_preferencia"].append(campo)

        filtros[campo] = (
            []
            if campo == "caracteristicas"
            else None
        )
        hubo_cambio = True

    lead = estado.setdefault("lead", {})

    nombre = actualizaciones.get("nombre")

    if nombre and nombre_valido(nombre):
        lead["nombre"] = normalizar_nombre(nombre)

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
    telefono = normalizar_telefono(telefono)

    if telefono:
        lead["whatsapp"] = telefono
        lead["whatsapp_confirmado"] = True

    if (
        actualizaciones.get("usar_numero_actual")
        and estado.get("numero_canal")
    ):
        lead["whatsapp"] = estado["numero_canal"]
        lead["whatsapp_confirmado"] = True

    return hubo_cambio


def aplicar_extracciones_tecnicas(
    estado: dict,
    mensaje: str,
) -> bool:
    filtros = estado["filtros"]
    hubo_cambio = False
    normalizado = normalizar_texto(mensaje)

    es_reclamo_resultados = any(
        frase in normalizado
        for frase in [
            "me mostraste",
            "me enviaste",
            "me volviste a enviar",
            "ninguna queda",
            "ninguna esta",
            "esas no quedan",
            "no corresponde",
            "no son de la zona",
            "te dije",
        ]
    )

    operacion = detectar_operacion(mensaje)

    if (
        operacion
        and filtros.get("tipo_operacion") != operacion
        and not es_reclamo_resultados
    ):
        filtros["tipo_operacion"] = operacion
        hubo_cambio = True

    tipo = detectar_tipo_propiedad(mensaje)

    if (
        tipo
        and filtros.get("tipo_propiedad") != tipo
        and not es_reclamo_resultados
    ):
        filtros["tipo_propiedad"] = tipo
        hubo_cambio = True

    presupuesto = detectar_presupuesto(mensaje)

    if (
        presupuesto > 0
        and not estado.get("esperando_codigo")
        and not extraer_correo(mensaje)
        and filtros.get("presupuesto_max") != presupuesto
        and not es_reclamo_resultados
    ):
        filtros["presupuesto_max"] = presupuesto
        hubo_cambio = True

    habitaciones = detectar_numero_preferencia(
        mensaje,
        ["habitaciones", "habitacion", "cuartos", "hab"],
    )

    if (
        habitaciones is not None
        and not es_reclamo_resultados
        and filtros.get("habitaciones_min") != habitaciones
    ):
        filtros["habitaciones_min"] = habitaciones
        hubo_cambio = True

    banos = detectar_numero_preferencia(
        mensaje,
        ["banos", "bano", "wc"],
    )

    if (
        banos is not None
        and not es_reclamo_resultados
        and filtros.get("banos_min") != banos
    ):
        filtros["banos_min"] = banos
        hubo_cambio = True

    garajes = detectar_numero_preferencia(
        mensaje,
        ["puestos", "estacionamientos", "garajes", "garaje"],
    )

    if (
        garajes is not None
        and not es_reclamo_resultados
        and filtros.get("garajes_min") != garajes
    ):
        filtros["garajes_min"] = garajes
        hubo_cambio = True

    caracteristicas = detectar_caracteristicas(
        mensaje
    )

    if (
        caracteristicas
        and not es_reclamo_resultados
    ):
        existentes = {
            normalizar_texto(valor): valor
            for valor in filtros.get(
                "caracteristicas",
                [],
            )
        }

        for caracteristica in caracteristicas:
            clave = normalizar_texto(
                caracteristica
            )

            if clave:
                existentes.setdefault(
                    clave,
                    caracteristica,
                )

        lista_nueva = list(
            existentes.values()
        )

        if lista_nueva != filtros.get(
            "caracteristicas",
            [],
        ):
            filtros["caracteristicas"] = (
                lista_nueva
            )
            hubo_cambio = True

    # IMPORTANTE:
    # Esta línea está fuera del if de características.
    # Debe ejecutarse para todos los mensajes.
    geografia = detectar_zona_ciudad(
        mensaje
    )

    # Si el usuario escribió explícitamente una ciudad o municipio,
    # ese valor tiene prioridad sobre la interpretación de la IA.
    ciudad_explicita = detectar_ciudad_canonica(
        mensaje
    )

    if ciudad_explicita:
        geografia["ciudad"] = ciudad_explicita

        # Evita guardar Naguanagua, San Diego u otro municipio
        # simultáneamente como ciudad y como zona.
        if (
            geografia.get("zona")
            and normalizar_texto(
                geografia["zona"]
            )
            == normalizar_texto(
                ciudad_explicita
            )
        ):
            geografia.pop("zona", None)

        geografia.pop(
            "ambiguedad",
            None,
        )
        geografia.pop(
            "ciudades_posibles",
            None,
        )

    ciudad_actual = filtros.get("ciudad")
    ciudad_actual_norm = normalizar_texto(
        ciudad_actual
    )

    # Solo actualiza la zona si el mensaje no es una queja sobre
    # propiedades enviadas anteriormente.
    if (
        geografia.get("zona")
        and not es_reclamo_resultados
    ):
        zona_detectada = geografia["zona"]

        if filtros.get("zona") != zona_detectada:
            filtros["zona"] = zona_detectada
            hubo_cambio = True

    # Actualización de ciudad o municipio.
    if geografia.get("ciudad"):
        ciudad_detectada = geografia["ciudad"]

        if filtros.get("ciudad") != ciudad_detectada:
            filtros["ciudad"] = ciudad_detectada
            hubo_cambio = True

        # Si el municipio estaba repetido en el campo zona,
        # se elimina para poder preguntar por una zona interna.
        if (
            filtros.get("zona")
            and normalizar_texto(
                filtros["zona"]
            )
            == normalizar_texto(
                ciudad_detectada
            )
        ):
            filtros["zona"] = None
            hubo_cambio = True

        estado.pop(
            "requiere_confirmar_ciudad",
            None,
        )

    # Manejo de una zona presente en varias ciudades.
    elif geografia.get("ambiguedad"):
        opciones = geografia.get(
            "ciudades_posibles",
            [],
        )

        opciones_norm = {
            normalizar_texto(opcion)
            for opcion in opciones
        }

        # Si ya se había confirmado una ciudad compatible, se
        # conserva y no se vuelve a preguntar.
        if (
            ciudad_actual_norm
            and ciudad_actual_norm in opciones_norm
        ):
            estado.pop(
                "requiere_confirmar_ciudad",
                None,
            )

        else:
            estado["requiere_confirmar_ciudad"] = {
                "zona": (
                    geografia.get("zona")
                    or filtros.get("zona")
                ),
                "opciones": opciones,
            }

            if filtros.get("ciudad") is not None:
                filtros["ciudad"] = None
                hubo_cambio = True

    return hubo_cambio



def aplicar_sin_preferencia_desde_texto(
    estado: dict,
    mensaje: str,
) -> bool:
    normalizado = normalizar_texto(mensaje)
    filtros = estado["filtros"]
    sin_preferencia = estado.setdefault(
        "sin_preferencia",
        [],
    )
    pregunta_pendiente = estado.get(
        "pregunta_pendiente"
    )
    hubo_cambio = False

    respuestas_abiertas = {
        "cualquiera",
        "cualquier",
        "no importa",
        "me da igual",
        "sin preferencia",
        "abierto",
        "abierta",
    }

    frases_zona_abierta = [
        "cualquier zona",
        "cualquiera zona",
        "no importa la zona",
        "me da igual la zona",
        "toda valencia",
        "todo naguanagua",
        "todo san diego",
        "zona abierta",
        "sin preferencia de zona",
    ]

    frases_presupuesto_abierto = [
        "presupuesto abierto",
        "sin limite de presupuesto",
        "sin limite",
        "no importa el presupuesto",
        "cualquier presupuesto",
        "no tengo presupuesto definido",
        "aun no tiene presupuesto",
        "todavia no tiene presupuesto",
    ]

    respuesta_abierta_breve = (
        normalizado in respuestas_abiertas
    )

    if (
        any(
            frase in normalizado
            for frase in frases_zona_abierta
        )
        or (
            pregunta_pendiente == "zona"
            and respuesta_abierta_breve
        )
    ):
        if "zona" not in sin_preferencia:
            sin_preferencia.append("zona")

        if filtros.get("zona") is not None:
            filtros["zona"] = None

        estado["pregunta_pendiente"] = None
        hubo_cambio = True

    if (
        any(
            frase in normalizado
            for frase in frases_presupuesto_abierto
        )
        or (
            pregunta_pendiente == "presupuesto_max"
            and respuesta_abierta_breve
        )
    ):
        if "presupuesto_max" not in sin_preferencia:
            sin_preferencia.append(
                "presupuesto_max"
            )

        if filtros.get("presupuesto_max") is not None:
            filtros["presupuesto_max"] = None

        estado["pregunta_pendiente"] = None
        hubo_cambio = True

    return hubo_cambio

# ============================================================
# BUSCADOR DE PROPIEDADES
# ============================================================


CIUDADES_CANONICAS = {
    "puerto cabello": "Puerto Cabello",
    "los guayos": "Los Guayos",
    "san diego": "San Diego",
    "naguanagua": "Naguanagua",
    "tocuyito": "Tocuyito",
    "barquisimeto": "Barquisimeto",
    "cabudare": "Cabudare",
    "guacara": "Guacara",
    "valencia": "Valencia",
}


SECTORES_POR_CIUDAD = {
    "Naguanagua": {
        "manongo",
        "mañongo",
        "la granja",
        "las quintas de naguanagua",
        "la campina",
        "la campiña",
        "tazajal",
        "el rincon",
        "el rincón",
        "carialinda",
        "el saman",
        "el samán",
        "manantial",
        "tarapio",
        "barbula",
        "bárbula",
    },
    "San Diego": {
        "los jarales",
        "la esmeralda",
        "el remanso",
        "valle de oro",
        "parque comercio",
        "pueblo de san diego",
        "casco san diego",
        "campo solo",
        "el morro",
        "la cumaca",
        "yuma",
    },
    "Valencia": {
        "el trigal",
        "trigal norte",
        "el parral",
        "prebo",
        "la vina",
        "la viña",
        "el vinedo",
        "el viñedo",
        "los colorados",
        "agua blanca",
        "los mangos",
        "guataparo",
        "el bosque",
        "la trigaleña",
        "las chimeneas",
        "la alegria",
        "la alegría",
        "los caobos",
        "la isabelica",
        "flor amarillo",
    },
    "Tocuyito": {
        "tocuyito",
        "libertador",
        "el libertador",
        "la pocaterra",
    },
    "Guacara": {
        "guacara",
        "yagua",
        "ciudad alianza",
    },
    "Los Guayos": {
        "los guayos",
        "paraparal",
    },
    "Puerto Cabello": {
        "puerto cabello",
        "patanemo",
        "borburata",
    },
    "Barquisimeto": {
        "barquisimeto",
        "este de barquisimeto",
        "oeste de barquisimeto",
    },
    "Cabudare": {
        "cabudare",
        "la piedad",
        "agua viva",
    },
}


def detectar_ciudad_canonica(
    texto: Any,
) -> Optional[str]:
    normalizado = normalizar_texto(texto)

    if not normalizado:
        return None

    for alias, ciudad in sorted(
        CIUDADES_CANONICAS.items(),
        key=lambda elemento: len(elemento[0]),
        reverse=True,
    ):
        if contiene_termino(normalizado, alias):
            return ciudad

    for ciudad, sectores in SECTORES_POR_CIUDAD.items():
        for sector in sorted(
            sectores,
            key=len,
            reverse=True,
        ):
            if contiene_termino(
                normalizado,
                normalizar_texto(sector),
            ):
                return ciudad

    return None


def inferir_ciudad_propiedad(
    propiedad: dict,
) -> Optional[str]:
    detalle_raw = propiedad.get("detalle_raw") or {}

    texto_ubicacion_especifica = " ".join(
        [
            str(propiedad.get("zona") or ""),
            str(propiedad.get("titulo") or ""),
            str(propiedad.get("direccion_publica") or ""),
            str(detalle_raw.get("municipality_label") or ""),
            str(detalle_raw.get("location_label") or ""),
            str(detalle_raw.get("zone_label") or ""),
        ]
    )

    ciudad_inferida = detectar_ciudad_canonica(
        texto_ubicacion_especifica
    )

    if ciudad_inferida:
        return ciudad_inferida

    ciudad_wasi = detectar_ciudad_canonica(
        propiedad.get("ciudad")
    )

    if ciudad_wasi:
        return ciudad_wasi

    ciudad_original = str(
        propiedad.get("ciudad") or ""
    ).strip()

    return ciudad_original or None


def ciudad_coincide(
    propiedad: dict,
    ciudad_buscada: Optional[str],
) -> bool:
    if not ciudad_buscada:
        return True

    ciudad_objetivo = (
        detectar_ciudad_canonica(ciudad_buscada)
        or str(ciudad_buscada).strip()
    )

    ciudad_propiedad = inferir_ciudad_propiedad(
        propiedad
    )

    if not ciudad_propiedad:
        return False

    return (
        normalizar_texto(ciudad_objetivo)
        == normalizar_texto(ciudad_propiedad)
    )

def obtener_precio(
    propiedad: dict,
    operacion: Optional[str],
) -> float:
    if operacion == "alquiler":
        return convertir_float(
            propiedad.get("precio_renta_float")
            or propiedad.get("precio_alquiler")
        )

    return convertir_float(
        propiedad.get("precio_venta_float")
        or propiedad.get("precio_venta")
    )


def coincide_tipo(
    propiedad: dict,
    tipo_buscado: Optional[str],
) -> bool:
    if not tipo_buscado:
        return True

    buscado = normalizar_tipo_propiedad(tipo_buscado)
    tipo_wasi = normalizar_tipo_propiedad(
        propiedad.get("tipo_propiedad_wasi", "")
    )
    titulo = normalizar_texto(propiedad.get("titulo", ""))

    if buscado == "casa":
        aceptados = {
            "casa",
            "quinta",
            "townhouse",
            "apartoquinta",
        }
        return any(
            tipo in tipo_wasi or tipo in titulo
            for tipo in aceptados
        )

    if buscado == "apartamento":
        return any(
            tipo in tipo_wasi or tipo in titulo
            for tipo in ["apartamento", "penthouse"]
        )

    return buscado in tipo_wasi or buscado in titulo


def zona_coincide(
    zona_buscada: Optional[str],
    zona_propiedad: Optional[str],
    ciudad_propiedad: Optional[str],
) -> bool:
    if not zona_buscada:
        return True

    buscada = normalizar_texto(zona_buscada)
    zona_prop = normalizar_texto(zona_propiedad)
    ciudad_prop = normalizar_texto(ciudad_propiedad)

    if buscada in f"{zona_prop} {ciudad_prop}":
        return True

    tokens_buscada = tokens_zona(buscada)
    tokens_propiedad = tokens_zona(zona_prop)
    tokens_ciudad = tokens_zona(ciudad_prop)

    tokens_propiedad -= tokens_ciudad

    if not tokens_buscada or not tokens_propiedad:
        return False

    if tokens_buscada.issubset(tokens_propiedad):
        return True

    if tokens_propiedad.issubset(tokens_buscada):
        return True

    interseccion = tokens_buscada.intersection(tokens_propiedad)

    return bool(interseccion) and len(interseccion) >= max(
        1,
        len(tokens_buscada) - 1,
    )


def criterios_suficientes(estado: dict) -> bool:
    if not rol_esta_confirmado(estado):
        return False

    filtros = estado.get("filtros", {})
    sin_preferencia = set(
        estado.get("sin_preferencia", [])
    )
    rol = estado.get("rol")

    criterios_base = bool(
        filtros.get("tipo_operacion")
        and filtros.get("tipo_propiedad")
        and filtros.get("ciudad")
    )

    if not criterios_base:
        return False

    zona_resuelta = bool(
        filtros.get("zona")
        or "zona" in sin_preferencia
    )

    if rol == "colega_inmobiliario":
        presupuesto_resuelto = bool(
            filtros.get("presupuesto_max")
            or "presupuesto_max" in sin_preferencia
        )

        return bool(
            zona_resuelta
            and presupuesto_resuelto
        )

    return zona_resuelta

def evaluar_propiedad(
    original: dict,
    filtros: dict,
) -> Optional[dict]:
    propiedad = deepcopy(original)
    operacion = filtros.get("tipo_operacion")
    precio = obtener_precio(propiedad, operacion)

    if precio <= 0:
        return None

    tipo = filtros.get("tipo_propiedad")
    zona = filtros.get("zona")
    presupuesto = convertir_float(
        filtros.get("presupuesto_max")
    )
    habitaciones_min = convertir_entero(
        filtros.get("habitaciones_min")
    )
    banos_min = convertir_entero(filtros.get("banos_min"))
    garajes_min = convertir_entero(filtros.get("garajes_min"))
    caracteristicas = filtros.get("caracteristicas", [])

    score = 0.0
    diferencias: List[str] = []
    exacta = True

    if tipo:
        if not coincide_tipo(propiedad, tipo):
            return None
        score += 35

    if zona:
        if not zona_coincide(
            zona,
            propiedad.get("zona"),
            propiedad.get("ciudad"),
        ):
            return None
        score += 30

    if presupuesto > 0:
        if precio <= presupuesto:
            score += 20
        elif precio <= presupuesto * (
            1 + MAX_EXCESO_PRESUPUESTO
        ):
            score += 5
            exacta = False
            diferencias.append(
                f"precio {formato_moneda(precio)}"
            )
        else:
            return None

    habitaciones = convertir_entero(
        propiedad.get("habitaciones")
    )
    if habitaciones_min > 0:
        if habitaciones >= habitaciones_min:
            score += 10
        elif habitaciones == habitaciones_min - 1:
            score += 4
            exacta = False
            diferencias.append(
                f"tiene {habitaciones} habitaciones"
            )
        else:
            return None

    banos = convertir_entero(propiedad.get("banos"))
    if banos_min > 0:
        if banos >= banos_min:
            score += 5
        elif banos == banos_min - 1:
            score += 2
            exacta = False
            diferencias.append(f"tiene {banos} baños")
        else:
            return None

    garajes = convertir_entero(propiedad.get("garajes"))
    if garajes_min > 0:
        if garajes >= garajes_min:
            score += 5
        elif garajes == garajes_min - 1:
            score += 2
            exacta = False
            diferencias.append(f"tiene {garajes} puestos")
        else:
            return None

    texto_propiedad = normalizar_texto(
        " ".join(
            [
                str(propiedad.get("titulo", "")),
                str(propiedad.get("descripcion", "")),
                str(propiedad.get("observaciones", "")),
                str(propiedad.get("caracteristicas_texto", "")),
            ]
        )
    )

    no_confirmadas = []

    for caracteristica in caracteristicas:
        caracteristica_norm = normalizar_texto(caracteristica)

        if caracteristica_norm in texto_propiedad:
            score += 5
        else:
            no_confirmadas.append(caracteristica)

    if no_confirmadas:
        exacta = False
        diferencias.append(
            "no especifica: " + ", ".join(no_confirmadas)
        )

    propiedad["precio_venta_float"] = convertir_float(
        propiedad.get("precio_venta")
    )
    propiedad["precio_renta_float"] = convertir_float(
        propiedad.get("precio_alquiler")
    )
    propiedad["operacion_buscada"] = operacion
    propiedad["_score"] = round(score, 2)
    propiedad["_diferencias"] = diferencias
    propiedad["_coincidencia"] = (
        "exacta"
        if exacta and not diferencias
        else "aproximada"
    )

    return propiedad


def buscar_mejores_propiedades(
    estado: dict,
    cantidad: int,
) -> Tuple[List[dict], str]:
    filtros = estado.get("filtros", {})

    enviados = {
        str(property_id)
        for property_id in estado.get(
            "propiedades_enviadas",
            [],
        )
    }

    evaluadas: List[dict] = []
    pasaron_ubicacion_tipo = 0

    for original in inventory_cache.get(
        "inventario",
        [],
    ):
        property_id = str(
            original.get("id") or ""
        )

        if not property_id:
            continue

        if property_id in enviados:
            continue

        if not original.get("activa", True):
            continue

        if not coincide_tipo(
            original,
            filtros.get("tipo_propiedad"),
        ):
            continue

        # Validación estricta de ciudad o municipio.
        if not ciudad_coincide(
            original,
            filtros.get("ciudad"),
        ):
            continue

        zona_buscada = filtros.get("zona")
        ciudad_buscada = filtros.get("ciudad")

        # Una ciudad o municipio no debe aplicarse simultáneamente
        # como una zona interna.
        zona_equivale_a_ciudad = bool(
            zona_buscada
            and ciudad_buscada
            and normalizar_texto(zona_buscada)
            == normalizar_texto(ciudad_buscada)
        )

        if (
            zona_buscada
            and not zona_equivale_a_ciudad
            and not zona_coincide(
                zona_buscada,
                original.get("zona"),
                original.get("ciudad"),
            )
        ):
            continue

        pasaron_ubicacion_tipo += 1

        propiedad = evaluar_propiedad(
            original,
            filtros,
        )

        if propiedad:
            evaluadas.append(propiedad)

    evaluadas.sort(
        key=lambda propiedad: (
            propiedad.get("_coincidencia")
            == "exacta",
            propiedad.get("_score", 0),
        ),
        reverse=True,
    )

    resultado = evaluadas[:cantidad]

    if resultado:
        return resultado, ""

    if pasaron_ubicacion_tipo > 0:
        return [], "precio_o_caracteristicas"

    return [], "zona_o_tipo"


def complementar_propiedades(
    estado: dict,
    seleccion: List[dict],
    cantidad: int,
) -> List[dict]:
    if len(seleccion) >= cantidad:
        return seleccion

    filtros = estado.get("filtros", {})

    usados = {
        str(propiedad.get("id"))
        for propiedad in seleccion
        if propiedad.get("id")
    }

    usados.update(
        str(property_id)
        for property_id in estado.get(
            "propiedades_enviadas",
            [],
        )
    )

    tipo_buscado = filtros.get(
        "tipo_propiedad"
    )
    operacion = (
        filtros.get("tipo_operacion")
        or "venta"
    )
    presupuesto = convertir_float(
        filtros.get("presupuesto_max")
    )

    for original in inventory_cache.get(
        "inventario",
        [],
    ):
        property_id = str(
            original.get("id") or ""
        )

        if not property_id:
            continue

        if property_id in usados:
            continue

        if not original.get("activa", True):
            continue

        if (
            tipo_buscado
            and not coincide_tipo(
                original,
                tipo_buscado,
            )
        ):
            continue

        # La propiedad complementaria también debe pertenecer
        # a la ciudad o municipio solicitado.
        if not ciudad_coincide(
            original,
            filtros.get("ciudad"),
        ):
            continue

        precio = obtener_precio(
            original,
            operacion,
        )

        if precio <= 0:
            continue

        if presupuesto > 0:
            margen_relajado = presupuesto * (
                1 + MAX_EXCESO_PRESUPUESTO * 2
            )

            if precio > margen_relajado:
                continue

        propiedad = deepcopy(original)

        propiedad["precio_venta_float"] = (
            convertir_float(
                propiedad.get("precio_venta")
            )
        )
        propiedad["precio_renta_float"] = (
            convertir_float(
                propiedad.get("precio_alquiler")
            )
        )
        propiedad["operacion_buscada"] = operacion
        propiedad["_score"] = 1.0
        propiedad["_coincidencia"] = "relajada"
        propiedad["_diferencias"] = [
            "Sugerida para ampliar las opciones disponibles"
        ]

        seleccion.append(propiedad)
        usados.add(property_id)

        if len(seleccion) >= cantidad:
            break

    return seleccion

# ============================================================
# FICHAS Y CONSULTAS SOBRE PROPIEDADES
# ============================================================

async def formatear_ficha(
    propiedad: dict,
    es_colega: bool,
    posicion: Optional[int] = None,
) -> str:
    operacion = propiedad.get("operacion_buscada")

    if not operacion:
        operacion = (
            "venta"
            if convertir_float(propiedad.get("precio_venta")) > 0
            else "alquiler"
        )

    precio = obtener_precio(propiedad, operacion)
    titulo = propiedad.get("titulo") or "Propiedad Mettryc"

    if posicion is not None:
        titulo = f"Opción {posicion}: {titulo}"

    area = convertir_float(propiedad.get("area"))
    area_texto = (
        f"{area:,.0f} m²".replace(",", ".")
        if area > 0
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

    diferencias = propiedad.get("_diferencias", [])
    if diferencias:
        lineas.append(
            "ℹ️ *Consideraciones:* "
            + "; ".join(diferencias[:2])
        )

    if es_colega:
        await sincronizar_google_sheet()
        captador_wasi = propiedad.get(
            "captador_wasi",
            "Asesor Mettryc",
        )
        cruce = cruzar_captador_con_sheet(captador_wasi)

        lineas.append(
            f"👤 *Captador:* "
            f"{cruce.get('nombre') or captador_wasi}"
        )

        if cruce.get("telefono"):
            lineas.append(
                "📲 *WhatsApp captador:* "
                f"https://wa.me/{cruce['telefono']}"
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
        estado.get("rol") == "colega_inmobiliario"
    )

    if especifica:
        introduccion = (
            "Encontré la propiedad. Figura activa en nuestro "
            "inventario:"
        )
    else:
        introduccion = (
            "Encontré estas opciones que pueden encajar con "
            "lo que buscas:"
        )

    fichas = []

    for indice, propiedad in enumerate(propiedades, start=1):
        fichas.append(
            await formatear_ficha(
                propiedad,
                es_colega,
                None if especifica else indice,
            )
        )

    if es_colega:
        cierre = (
            "Puedes contactar al captador indicado en la ficha. "
            "También puedes pedirme más opciones o preguntarme "
            "algo sobre una propiedad."
        )
    else:
        cierre = (
            "¿Quieres agendar una visita o prefieres preguntarme "
            "algo sobre alguna de estas propiedades?"
        )

    return "\n\n".join(
        [introduccion, *fichas, cierre]
    )


def resolver_propiedad_contexto(
    estado: dict,
    posicion: Optional[int] = None,
    codigo: Optional[str] = None,
) -> Optional[dict]:
    if codigo:
        propiedad = buscar_por_codigo(codigo)

        if propiedad:
            return propiedad

    if posicion is not None:
        lote = estado.get("ultimo_lote", [])
        indice = posicion - 1

        if 0 <= indice < len(lote):
            propiedad = buscar_por_codigo(lote[indice])

            if propiedad:
                return propiedad

    propiedad_interes = estado.get("propiedad_interes")

    if propiedad_interes:
        return deepcopy(propiedad_interes)

    property_id = estado.get("propiedad_activa_id")

    if property_id:
        propiedad = buscar_por_codigo(property_id)

        if propiedad:
            return propiedad

        propiedad = property_detail_cache.get(
            str(property_id)
        )

        if propiedad:
            return deepcopy(propiedad)

    lote = estado.get("ultimo_lote", [])

    if len(lote) == 1:
        return buscar_por_codigo(lote[0])

    return None


def detalle_propiedad_para_ia(propiedad: dict) -> dict:
    return {
        "id": propiedad.get("id"),
        "titulo": propiedad.get("titulo"),
        "descripcion": propiedad.get("descripcion"),
        "observaciones": propiedad.get("observaciones"),
        "ciudad": propiedad.get("ciudad"),
        "zona": propiedad.get("zona"),
        "tipo": propiedad.get("tipo_propiedad_wasi"),
        "activa": propiedad.get("activa"),
        "precio_venta": propiedad.get("precio_venta"),
        "precio_alquiler": propiedad.get("precio_alquiler"),
        "area": propiedad.get("area"),
        "area_construida": propiedad.get("area_construida"),
        "area_terreno": propiedad.get("area_terreno"),
        "habitaciones": propiedad.get("habitaciones"),
        "banos": propiedad.get("banos"),
        "garajes": propiedad.get("garajes"),
        "caracteristicas_generales": propiedad.get(
            "caracteristicas_generales",
            [],
        ),
        "caracteristicas_internas": propiedad.get(
            "caracteristicas_internas",
            [],
        ),
        "caracteristicas_externas": propiedad.get(
            "caracteristicas_externas",
            [],
        ),
        "video": propiedad.get("video"),
        "enlace": propiedad.get("enlace"),
    }


def construir_texto_documental_propiedad(
    propiedad: dict,
) -> str:
    return "\n".join(
        [
            f"Título: {propiedad.get('titulo') or ''}",
            f"Descripción: {propiedad.get('descripcion') or ''}",
            f"Observaciones: {propiedad.get('observaciones') or ''}",
            (
                "Características generales: "
                + ", ".join(
                    propiedad.get(
                        "caracteristicas_generales",
                        [],
                    )
                )
            ),
            (
                "Características internas: "
                + ", ".join(
                    propiedad.get(
                        "caracteristicas_internas",
                        [],
                    )
                )
            ),
            (
                "Características externas: "
                + ", ".join(
                    propiedad.get(
                        "caracteristicas_externas",
                        [],
                    )
                )
            ),
            f"Habitaciones: {propiedad.get('habitaciones')}",
            f"Baños: {propiedad.get('banos')}",
            f"Garajes o puestos: {propiedad.get('garajes')}",
            f"Área: {propiedad.get('area')}",
            f"Precio de venta: {propiedad.get('precio_venta')}",
            f"Precio de alquiler: {propiedad.get('precio_alquiler')}",
        ]
    ).strip()

async def atender_solicitud_captador(
    estado: dict,
    posicion: Optional[int] = None,
    codigo: Optional[str] = None,
) -> str:
    propiedad = resolver_propiedad_contexto(
        estado,
        posicion=posicion,
        codigo=codigo,
    )

    if not propiedad:
        estado["esperando_codigo"] = True
        estado["pregunta_pendiente"] = (
            "codigo_para_captador"
        )

        return (
            "Para identificar al captador necesito ubicar primero "
            "la propiedad. Envíame el código que aparece al final "
            "del título o en la descripción del anuncio."
        )

    property_id = str(propiedad.get("id") or "")

    estado["propiedad_interes"] = propiedad
    estado["propiedad_activa_id"] = property_id
    estado["ultima_propiedad_consultada_id"] = property_id

    # El rol debe estar explícitamente confirmado antes de decidir
    # si se puede revelar el captador.
    if not rol_esta_confirmado(estado):
        return solicitar_rol_para_accion(
            estado,
            "solicitar_captador",
            propiedad_id=property_id,
            posicion=posicion,
        )

    # --------------------------------------------------------
    # CLIENTE: NO REVELAR DATOS DEL CAPTADOR
    # --------------------------------------------------------

    if estado.get("rol") == "cliente":
        estado["pregunta_pendiente"] = (
            "ofrecer_agente_cliente"
        )
        estado["detalle_pregunta_pendiente"] = {
            "tipo": "ofrecer_atencion_agente",
            "propiedad_id": property_id,
        }

        return (
            "Por privacidad y organización del servicio no "
            "compartimos directamente los datos del captador con "
            "clientes. Sin embargo, puedo asignarte uno de nuestros "
            "agentes para ayudarte con esta propiedad. "
            "¿Quieres que te pongamos en contacto con un agente?"
        )

    # --------------------------------------------------------
    # COLEGA: CONSULTAR WASI Y GOOGLE SHEETS
    # --------------------------------------------------------

    detalle = await consultar_detalle_propiedad_wasi(
        property_id
    )

    if not detalle:
        detalle = propiedad

    estado["propiedad_interes"] = detalle
    estado["propiedad_activa_id"] = property_id

    await sincronizar_google_sheet()

    captador_wasi = str(
        detalle.get("captador_wasi")
        or propiedad.get("captador_wasi")
        or ""
    ).strip()

    telefono_wasi = normalizar_telefono(
        detalle.get("telefono_captador_wasi")
        or propiedad.get("telefono_captador_wasi")
    )

    cruce = cruzar_captador_con_sheet(
        captador_wasi
    )

    nombre_captador = (
        cruce.get("nombre")
        or captador_wasi
        or "Captador no identificado"
    )

    # Google Sheets tiene prioridad. Si no está allí, se utiliza
    # el teléfono del usuario/captador registrado en Wasi.
    telefono_captador = (
        normalizar_telefono(cruce.get("telefono"))
        or telefono_wasi
    )

    estado["pregunta_pendiente"] = None
    estado.pop(
        "detalle_pregunta_pendiente",
        None,
    )

    if telefono_captador:
        return (
            "Claro, colega. El captador de esta propiedad es "
            f"{nombre_captador}.\n"
            f"📲 WhatsApp: "
            f"https://wa.me/{telefono_captador}"
        )

    if captador_wasi:
        return (
            "El captador registrado en Wasi es "
            f"{nombre_captador}, pero no pude localizar su "
            "WhatsApp en el directorio de Google Sheets. "
            "Si quieres, puedo notificar al equipo administrativo."
        )

    return (
        "No pude identificar al captador de esta propiedad en "
        "Wasi ni en el directorio de Google Sheets. "
        "Si quieres, puedo notificar al equipo administrativo."
    )

async def responder_pregunta_propiedad(
    estado: dict,
    propiedad: dict,
    pregunta: str,
) -> str:
    detalle = await consultar_detalle_propiedad_wasi(
        str(propiedad.get("id"))
    )

    if not detalle:
        return (
            "No pude recuperar los detalles de esa propiedad en "
            "este momento. ¿Quieres que un asesor lo confirme?"
        )

    estado["propiedad_interes"] = detalle
    estado["propiedad_activa_id"] = detalle.get("id")
    estado["estado_conversacion"] = "consulta_propiedad"
    estado["pregunta_pendiente"] = "confirmar_visita"

    pregunta_norm = normalizar_texto(pregunta)
    fuente = construir_texto_documental_propiedad(detalle)
    fuente_norm = normalizar_texto(fuente)

    # Validaciones especiales para características que no deben
    # deducirse únicamente por la cantidad de estacionamientos.
    if any(
        termino in pregunta_norm
        for termino in [
            "techado",
            "techados",
            "cubierto",
            "cubiertos",
        ]
    ):
        if not any(
            termino in fuente_norm
            for termino in [
                "techad",
                "cubiert",
                "estacionamiento bajo techo",
            ]
        ):
            return (
                "La ficha confirma que tiene "
                f"{detalle.get('garajes', 'N/D')} puestos de "
                "estacionamiento, pero no especifica si son "
                "techados. Si quieres, puedo pedirle a un asesor "
                "que lo confirme. ¿Deseas agendar una visita?"
            )

    mensajes = [
        {
            "role": "system",
            "content": (
                "Eres Paty de Mettryc Realty. Responde únicamente "
                "con la información textual y estructurada de la "
                "propiedad suministrada. No mezcles esta propiedad "
                "con otras opciones. No inventes cantidades ni "
                "características. Si el dato no está documentado, "
                "di claramente que no está especificado. "
                "Termina preguntando si desea agendar una visita."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "id_propiedad_consultada": detalle.get("id"),
                    "titulo_propiedad_consultada": detalle.get(
                        "titulo"
                    ),
                    "pregunta": pregunta,
                    "fuente_documental": fuente,
                },
                ensure_ascii=False,
            ),
        },
    ]

    resultado = await llamar_openrouter_json(
        RespuestaPropiedadIA,
        mensajes,
        temperatura=0.05,
        max_tokens=700,
    )

    if isinstance(resultado, RespuestaPropiedadIA):
        return resultado.respuesta.strip()

    return (
        "No encontré ese dato especificado en la ficha de Wasi. "
        "Si quieres, puedo pedirle a un asesor que lo confirme. "
        "¿Deseas agendar una visita?"
    )
# ============================================================
# LEADS Y NOTIFICACIONES
# ============================================================

RESPUESTAS_AFIRMATIVAS = {
    "si", "sii", "sip", "claro", "correcto", "exacto",
    "afirmativo", "por supuesto", "ok", "okay", "listo",
    "dale", "perfecto",
}

RESPUESTAS_NEGATIVAS = {
    "no", "no gracias", "negativo", "incorrecto",
    "no es correcto", "para nada",
}


def es_respuesta_afirmativa(texto: str) -> bool:
    normalizado = normalizar_texto(texto)

    if not normalizado:
        return False

    if normalizado in RESPUESTAS_AFIRMATIVAS:
        return True

    frases_afirmativas = [
        "te dije que si",
        "ya te dije que si",
        "claro que si",
        "por supuesto que si",
        "si quiero",
        "si deseo",
        "si por favor",
        "si agendala",
        "si agendalo",
        "si quiero visitarla",
        "si quiero visitarlo",
        "correcto",
        "eso es correcto",
    ]

    if any(frase in normalizado for frase in frases_afirmativas):
        return True

    if normalizado.startswith("si "):
        return True

    return False


def es_respuesta_negativa(texto: str) -> bool:
    normalizado = normalizar_texto(texto)
    return (
        normalizado in RESPUESTAS_NEGATIVAS
        or normalizado.startswith("no ")
    )


def lead_completo(estado: dict) -> bool:
    lead = estado.get("lead", {})

    return bool(
        nombre_valido(lead.get("nombre"))
        and correo_valido(lead.get("correo"))
        and normalizar_telefono(lead.get("whatsapp"))
        and lead.get("whatsapp_confirmado")
    )


def datos_lead_faltantes(estado: dict) -> List[str]:
    lead = estado.get("lead", {})
    faltantes = []

    if not nombre_valido(lead.get("nombre")):
        faltantes.append("nombre completo")

    if not correo_valido(lead.get("correo")):
        faltantes.append("correo electrónico")

    if not (
        normalizar_telefono(lead.get("whatsapp"))
        and lead.get("whatsapp_confirmado")
    ):
        faltantes.append("número de WhatsApp")

    return faltantes


def actualizar_lead_desde_mensaje(
    estado: dict,
    mensaje: str,
) -> List[str]:
    lead = estado["lead"]
    actualizados: List[str] = []

    correo = extraer_correo(mensaje)
    if correo and correo_valido(correo):
        if lead.get("correo") != correo:
            lead["correo"] = correo
            actualizados.append("correo electrónico")

    telefono_original = re.search(
        r"(\+?\d[\d\s\-()]{7,}\d)",
        mensaje or "",
    )

    if telefono_original:
        telefono = normalizar_telefono(
            telefono_original.group(1)
        )

        if telefono:
            if lead.get("whatsapp") != telefono:
                actualizados.append("número de WhatsApp")

            lead["whatsapp"] = telefono
            lead["whatsapp_confirmado"] = True

    normalizado = normalizar_texto(mensaje)

    if (
        estado.get("numero_canal")
        and any(
            frase in normalizado
            for frase in [
                "mismo numero",
                "numero del chat",
                "este numero",
                "numero actual",
            ]
        )
    ):
        lead["whatsapp"] = estado["numero_canal"]
        lead["whatsapp_confirmado"] = True
        actualizados.append("número de WhatsApp")

    texto_nombre = mensaje

    if correo:
        texto_nombre = texto_nombre.replace(correo, " ")

    if telefono_original:
        texto_nombre = texto_nombre.replace(
            telefono_original.group(1),
            " ",
        )

    coincidencia_nombre = re.search(
        r"(?:mi\s+nombre\s+es|me\s+llamo|soy)\s+"
        r"([A-Za-zÀ-ÖØ-öø-ÿ' ]{3,})",
        texto_nombre,
        flags=re.IGNORECASE,
    )

    nombre_candidato = (
        coincidencia_nombre.group(1).strip()
        if coincidencia_nombre
        else None
    )

    if not nombre_candidato:
        texto_filtrado = re.sub(
            r"[^A-Za-zÀ-ÖØ-öø-ÿ' ]",
            " ",
            texto_nombre,
        )
        texto_filtrado = re.sub(
            r"\s+",
            " ",
            texto_filtrado,
        ).strip()

        if 2 <= len(texto_filtrado.split()) <= 6:
            nombre_candidato = texto_filtrado

    if nombre_candidato and nombre_valido(nombre_candidato):
        nombre = normalizar_nombre(nombre_candidato)

        if lead.get("nombre") != nombre:
            lead["nombre"] = nombre
            actualizados.append("nombre completo")

    return list(dict.fromkeys(actualizados))


def mensaje_solicitud_datos_lead(
    estado: dict,
    saludo: bool = False,
) -> str:
    faltantes = datos_lead_faltantes(estado)

    if not faltantes:
        estado["lead_confirmacion_pendiente"] = True
        return mensaje_confirmacion_lead(estado)

    introduccion = (
        "¡Con gusto! Para asignarte un asesor, necesito:"
        if saludo
        else "Para continuar, me faltan estos datos:"
    )

    lineas = [
        f"{indice}. {campo.capitalize()}"
        for indice, campo in enumerate(faltantes, start=1)
    ]

    return (
        introduccion
        + "\n"
        + "\n".join(lineas)
        + "\n\nPuedes enviarlos juntos en un solo mensaje."
    )


def mensaje_confirmacion_lead(estado: dict) -> str:
    lead = estado["lead"]
    whatsapp = lead.get("whatsapp") or "N/D"

    if whatsapp != "N/D" and not str(whatsapp).startswith("+"):
        whatsapp = f"+{whatsapp}"

    return (
        "✔️ Estos son los datos que registré:\n"
        f"- Nombre: {lead.get('nombre') or 'N/D'}\n"
        f"- Correo: {lead.get('correo') or 'N/D'}\n"
        f"- WhatsApp: {whatsapp}\n\n"
        "¿Está todo correcto? Responde Sí o No."
    )


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
        return bool(respuesta.json().get("ok"))

    except Exception as exc:
        logger.error(
            "Error Telegram chat=%s tipo=%s",
            str(chat_id)[-4:],
            type(exc).__name__,
        )
        return False


def resumen_filtros(estado: dict) -> str:
    filtros = estado.get("filtros", {})
    lineas = []

    etiquetas = {
        "tipo_operacion": "Operación",
        "tipo_propiedad": "Tipo",
        "ciudad": "Ciudad",
        "zona": "Zona",
        "habitaciones_min": "Habitaciones mínimas",
        "banos_min": "Baños mínimos",
        "garajes_min": "Puestos mínimos",
    }

    for campo, etiqueta in etiquetas.items():
        valor = filtros.get(campo)
        if valor not in (None, "", []):
            lineas.append(f"- {etiqueta}: {valor}")

    if filtros.get("presupuesto_max"):
        lineas.append(
            "- Presupuesto: "
            + formato_moneda(filtros["presupuesto_max"])
        )

    if filtros.get("caracteristicas"):
        lineas.append(
            "- Características: "
            + ", ".join(filtros["caracteristicas"])
        )

    return "\n".join(lineas) or "- Sin filtros específicos"
async def notificar_lead_cliente(estado: dict) -> bool:
    lead = estado.get("lead", {})
    propiedad = estado.get("propiedad_interes")
    agente = estado.get("agente_asignado")
    whatsapp = normalizar_telefono(lead.get("whatsapp"))

    propiedad_texto = "No especificada"

    if propiedad:
        propiedad_texto = (
            f"{propiedad.get('titulo')}\n"
            f"ID: {propiedad.get('id')}\n"
            f"{propiedad.get('enlace')}"
        )

    mensaje = (
        "🏠 NUEVO LEAD METTRYC\n\n"
        f"ID: {estado.get('lead_id')}\n"
        f"Motivo: {estado.get('motivo_contacto') or 'Contacto'}\n"
        f"Nombre: {lead.get('nombre')}\n"
        f"Correo: {lead.get('correo')}\n"
        f"WhatsApp: {whatsapp or 'N/D'}\n"
        f"Contacto: "
        f"{f'https://wa.me/{whatsapp}' if whatsapp else 'N/D'}\n\n"
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
            destinos.add(str(telegram_id).strip())

    resultados = [
        await enviar_telegram(destino, mensaje)
        for destino in destinos
    ]

    return any(resultados)


def preparar_contacto_colega_desde_estado(
    estado: dict,
) -> dict:
    contacto = estado.setdefault(
        "contacto_colega",
        {
            "nombre": None,
            "whatsapp": None,
        },
    )

    lead = estado.get("lead", {})

    # Si la IA ya había extraído el nombre durante la conversación,
    # se reutiliza como nombre del colega.
    if (
        not contacto.get("nombre")
        and nombre_valido(lead.get("nombre"))
    ):
        contacto["nombre"] = normalizar_nombre(
            lead["nombre"]
        )

    # Se intenta utilizar primero el número del canal.
    numero_canal = normalizar_telefono(
        estado.get("numero_canal")
    )

    if numero_canal and not contacto.get("whatsapp"):
        contacto["whatsapp"] = numero_canal

    # Si durante la conversación ya se había extraído un WhatsApp,
    # también puede reutilizarse.
    whatsapp_lead = normalizar_telefono(
        lead.get("whatsapp")
    )

    if (
        whatsapp_lead
        and not contacto.get("whatsapp")
    ):
        contacto["whatsapp"] = whatsapp_lead

    return contacto


def actualizar_contacto_colega_desde_mensaje(
    estado: dict,
    mensaje: str,
) -> List[str]:
    contacto = preparar_contacto_colega_desde_estado(
        estado
    )
    actualizados: List[str] = []

    coincidencia_telefono = re.search(
        r"(\+?\d[\d\s\-()]{7,}\d)",
        mensaje or "",
    )

    telefono_original = None

    if coincidencia_telefono:
        telefono_original = coincidencia_telefono.group(1)
        telefono = normalizar_telefono(telefono_original)

        if telefono:
            if contacto.get("whatsapp") != telefono:
                actualizados.append("WhatsApp")

            contacto["whatsapp"] = telefono

    texto_para_nombre = str(mensaje or "")

    if telefono_original:
        texto_para_nombre = texto_para_nombre.replace(
            telefono_original,
            " ",
        )

    correo = extraer_correo(texto_para_nombre)

    if correo:
        texto_para_nombre = texto_para_nombre.replace(
            correo,
            " ",
        )

    coincidencia_nombre = re.search(
        r"(?:mi\s+nombre\s+es|me\s+llamo|soy)\s+"
        r"([A-Za-zÀ-ÖØ-öø-ÿ'’\- ]{3,})",
        texto_para_nombre,
        flags=re.IGNORECASE,
    )

    nombre_candidato = None

    if coincidencia_nombre:
        nombre_candidato = coincidencia_nombre.group(1).strip()

        # Elimina expresiones que podrían venir después del nombre.
        nombre_candidato = re.split(
            r"\b(?:y|mi\s+numero|mi\s+telefono|mi\s+whatsapp)\b",
            nombre_candidato,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()

    if not nombre_candidato:
        texto_filtrado = re.sub(
            r"[^A-Za-zÀ-ÖØ-öø-ÿ'’\- ]",
            " ",
            texto_para_nombre,
        )
        texto_filtrado = re.sub(
            r"\s+",
            " ",
            texto_filtrado,
        ).strip()

        palabras_bloqueadas = {
            "quiero",
            "hablar",
            "humano",
            "asesor",
            "agente",
            "atencion",
            "atención",
            "necesito",
            "ayuda",
            "colega",
            "cliente",
            "contacto",
            "whatsapp",
            "telefono",
            "teléfono",
        }

        palabras = texto_filtrado.split()

        if (
            2 <= len(palabras) <= 6
            and not any(
                normalizar_texto(palabra)
                in {
                    normalizar_texto(valor)
                    for valor in palabras_bloqueadas
                }
                for palabra in palabras
            )
        ):
            nombre_candidato = texto_filtrado

    if (
        nombre_candidato
        and nombre_valido(nombre_candidato)
    ):
        nombre_normalizado = normalizar_nombre(
            nombre_candidato
        )

        if contacto.get("nombre") != nombre_normalizado:
            contacto["nombre"] = nombre_normalizado
            actualizados.append("nombre")

    return list(dict.fromkeys(actualizados))


def datos_contacto_colega_faltantes(
    estado: dict,
) -> List[str]:
    contacto = preparar_contacto_colega_desde_estado(
        estado
    )
    faltantes: List[str] = []

    if not nombre_valido(contacto.get("nombre")):
        faltantes.append("nombre completo")

    if not normalizar_telefono(
        contacto.get("whatsapp")
    ):
        faltantes.append(
            "número de WhatsApp con código de país"
        )

    return faltantes


def mensaje_solicitud_contacto_colega(
    estado: dict,
) -> str:
    faltantes = datos_contacto_colega_faltantes(
        estado
    )

    if not faltantes:
        return ""

    if len(faltantes) == 1:
        campos = faltantes[0]
    else:
        campos = (
            ", ".join(faltantes[:-1])
            + " y "
            + faltantes[-1]
        )

    return (
        "Claro, colega. Para enviar tu solicitud al equipo "
        f"administrativo necesito tu {campos}. "
        "Puedes enviarlo en un solo mensaje."
    )

async def notificar_colega_administradores(
    estado: dict,
    mensaje_original: str,
) -> bool:
    contacto = preparar_contacto_colega_desde_estado(
        estado
    )

    asunto_fue_escrito = estado.get(
        "asunto_contacto_colega_escrito",
        False,
    )

    asunto = str(
        estado.get("asunto_contacto_colega")
        or ""
    ).strip()

    if not (
        asunto_fue_escrito
        and asunto_colega_valido(asunto)
    ):
        logger.warning(
            "Notificación de colega cancelada: "
            "el asunto no fue escrito por el colega."
        )
        return False

    nombre_colega = (
        contacto.get("nombre")
        or "No identificado"
    )
    whatsapp = normalizar_telefono(
        contacto.get("whatsapp")
    )


    mensaje_guardado = (
        estado.get("mensaje_contacto_colega")
        or mensaje_original
        or "Solicita atención del equipo administrativo"
    )

    propiedad = estado.get("propiedad_interes")
    propiedad_texto = "No especificada"

    if propiedad:
        propiedad_texto = (
            f"{propiedad.get('titulo')}\n"
            f"ID: {propiedad.get('id')}\n"
            f"{propiedad.get('enlace')}"
        )

    whatsapp_formateado = (
        f"+{whatsapp}"
        if whatsapp
        else "N/D"
    )

    enlace_whatsapp = (
        f"https://wa.me/{whatsapp}"
        if whatsapp
        else "N/D"
    )

    mensaje_telegram = (
        "🤝 SOLICITUD DE COLEGA INMOBILIARIO\n\n"
        f"📌 ASUNTO\n"
        f"{asunto}\n\n"
        f"👤 DATOS DEL COLEGA\n"
        f"Nombre: {nombre_colega}\n"
        f"WhatsApp: {whatsapp_formateado}\n"
        f"Contacto directo: {enlace_whatsapp}\n\n"
        f"💬 MENSAJE\n"
        f"{mensaje_guardado}\n\n"
        f"📋 NECESIDAD\n"
        f"{resumen_filtros(estado)}\n\n"
        f"⭐ PROPIEDAD RELACIONADA\n"
        f"{propiedad_texto}"
    )

    destinos = set(TELEGRAM_ADMIN_IDS)

    if not destinos:
        logger.warning(
            "No hay TELEGRAM_ADMIN_IDS configurados para "
            "notificar la solicitud del colega."
        )
        return False

    resultados = [
        await enviar_telegram(
            destino,
            mensaje_telegram,
        )
        for destino in destinos
    ]

    return any(resultados)


async def completar_y_asignar_lead(
    estado: dict,
) -> str:
    estado["lead_confirmacion_pendiente"] = False
    estado["lead_confirmado"] = True

    if not estado.get("lead_id"):
        estado["lead_id"] = str(uuid.uuid4())

    if not estado.get("agente_asignado"):
        estado["agente_asignado"] = (
            await asignar_agente_round_robin()
        )

    if not estado.get("notificacion_enviada"):
        estado["notificacion_enviada"] = (
            await notificar_lead_cliente(estado)
        )

    estado["objetivo"] = "lead_asignado"
    estado["estado_conversacion"] = "lead_asignado"

    agente = estado.get("agente_asignado")
    nombre = estado["lead"].get("nombre") or ""

    if agente:
        return (
            f"¡Listo, {nombre}! "
            f"{agente.get('nombre')} recibió tu solicitud y "
            "te contactará por WhatsApp. "
            "¡Gracias por confiar en Mettryc Realty!"
        )

    return (
        f"¡Listo, {nombre}! Registré tu solicitud. "
        "El equipo de Mettryc Realty te contactará por WhatsApp."
    )


# ============================================================
# ACCIONES CONVERSACIONALES
# ============================================================

def obtener_pregunta_faltante(estado: dict) -> str:
    filtros = estado.get("filtros", {})
    sin_preferencia = set(
        estado.get("sin_preferencia", [])
    )
    rol = estado.get("rol")

    if not rol_esta_confirmado(estado):
        estado["pregunta_pendiente"] = "confirmar_rol"
        return mensaje_confirmacion_rol()

    if not filtros.get("tipo_operacion"):
        estado["pregunta_pendiente"] = "tipo_operacion"
        return "¿La propiedad sería para comprar o alquilar?"

    if not filtros.get("tipo_propiedad"):
        estado["pregunta_pendiente"] = "tipo_propiedad"
        return (
            "¿Qué tipo de inmueble buscas, por ejemplo apartamento, "
            "casa, townhouse, local u oficina?"
        )

    if not filtros.get("ciudad"):
        confirmacion = estado.get("requiere_confirmar_ciudad")

        if confirmacion:
            opciones = confirmacion.get("opciones", [])
            zona = (
                confirmacion.get("zona")
                or filtros.get("zona")
            )

            if opciones:
                estado["pregunta_pendiente"] = (
                    "confirmar_ciudad"
                )
                return (
                    f"La zona {zona} aparece en "
                    f"{', '.join(opciones)}. "
                    "¿En cuál ciudad deseas buscar?"
                )

        estado["pregunta_pendiente"] = "ciudad"
        return "¿En qué ciudad deseas buscar?"

    if (
        not filtros.get("zona")
        and "zona" not in sin_preferencia
    ):
        estado["pregunta_pendiente"] = "zona"

        if rol == "colega_inmobiliario":
            return (
                f"Perfecto, colega. ¿Qué zona de "
                f"{filtros.get('ciudad')} prefiere tu cliente? "
                "Si está abierto a cualquier zona, también puedes "
                "indicármelo."
            )

        return (
            "¿En qué zona o urbanización de esa ciudad "
            "te gustaría encontrar la propiedad?"
        )

    if (
        rol == "colega_inmobiliario"
        and not filtros.get("presupuesto_max")
        and "presupuesto_max" not in sin_preferencia
    ):
        estado["pregunta_pendiente"] = "presupuesto_max"

        return (
            "¿Qué presupuesto aproximado maneja tu cliente? "
            "También puedes indicarme si el presupuesto está abierto. "
            "Si necesita habitaciones o alguna característica "
            "especial, puedes decírmelo en el mismo mensaje."
        )

    estado["pregunta_pendiente"] = None
    return ""


async def mostrar_propiedades(estado: dict) -> str:
    rol = estado.get("rol")
    filtros = estado.get("filtros", {})

    cantidad = (
        5
        if rol == "colega_inmobiliario"
        else MAX_PROPIEDADES_POR_LOTE
    )

    propiedades, motivo = buscar_mejores_propiedades(
        estado,
        cantidad,
    )

    # Solo se amplían resultados si el usuario no indicó una zona.
    # Nunca se sustituyen resultados exactos por otras zonas.
    if (
        rol != "colega_inmobiliario"
        and not filtros.get("zona")
        and len(propiedades) < cantidad
    ):
        propiedades = complementar_propiedades(
            estado,
            propiedades,
            cantidad,
        )

    if not propiedades:
        estado["ultimo_lote"] = []
        estado["propiedad_activa_id"] = None
        estado["pregunta_pendiente"] = "sin_resultados"
        estado["detalle_pregunta_pendiente"] = {
            "tipo": "eleccion",
            "opciones": [
                "ampliar_zona",
                "ajustar_busqueda",
            ],
            "descripcion": (
                "El usuario debe elegir si desea ampliar la zona "
                "o ajustar alguna condición de la búsqueda."
            ),
        }

        tipo = (
            filtros.get("tipo_propiedad")
            or "propiedades"
        )
        zona = filtros.get("zona")
        ciudad = filtros.get("ciudad")
        presupuesto = filtros.get(
            "presupuesto_max"
        )

        if zona:
            ubicacion = (
                f"{zona}, {ciudad}"
                if ciudad
                else zona
            )
        else:
            ubicacion = (
                ciudad
                or "la ubicación indicada"
            )

        if motivo == "precio_o_caracteristicas":
            return (
                f"No encontré {tipo} activas en {ubicacion} "
                f"con el presupuesto de "
                f"{formato_moneda(presupuesto) if presupuesto else 'ese rango'} "
                "y las características actuales. "
                "¿Quieres ampliar la zona o ajustar la búsqueda?"
            )

        return (
            f"No encontré {tipo} activas en {ubicacion} "
            "con los criterios actuales. "
            "¿Quieres ampliar la zona o ajustar la búsqueda?"
        )

    ids = [
        str(propiedad["id"])
        for propiedad in propiedades
    ]

    for property_id in ids:
        if property_id not in estado["propiedades_enviadas"]:
            estado["propiedades_enviadas"].append(property_id)

    estado["ultimo_lote"] = ids
    estado["propiedad_activa_id"] = (
        ids[0]
        if len(ids) == 1
        else None
    )
    estado["objetivo"] = "evaluar_resultados"
    estado["estado_conversacion"] = "propiedades_mostradas"
    estado["pregunta_pendiente"] = (
        "visita_o_pregunta_propiedad"
    )

    return await construir_respuesta_fichas(
        estado,
        propiedades,
    )


async def mostrar_inmueble_especifico(
    estado: dict,
    codigo: str,
) -> str:
    propiedad = await consultar_detalle_propiedad_wasi(codigo)

    if not propiedad or not propiedad.get("activa", True):
        estado["esperando_codigo"] = True
        return (
            f"No encontré un inmueble activo con el código {codigo}. "
            "Revisa el código o envíame el enlace del anuncio."
        )

    precio_venta = convertir_float(
        propiedad.get("precio_venta")
    )
    precio_alquiler = convertir_float(
        propiedad.get("precio_alquiler")
    )

    propiedad["precio_venta_float"] = precio_venta
    propiedad["precio_renta_float"] = precio_alquiler

    operacion = estado["filtros"].get("tipo_operacion")

    if not operacion:
        operacion = (
            "venta"
            if precio_venta > 0
            else "alquiler"
        )

    propiedad["operacion_buscada"] = operacion
    property_id = str(propiedad["id"])

    estado["ultimo_lote"] = [property_id]
    estado["propiedad_activa_id"] = property_id
    estado["ultima_propiedad_consultada_id"] = property_id
    estado["propiedad_interes"] = propiedad
    estado["contexto_propiedad_bloqueado"] = True
    estado["esperando_codigo"] = False
    estado["objetivo"] = "evaluar_resultados"
    estado["estado_conversacion"] = "propiedades_mostradas"
    estado["pregunta_pendiente"] = (
        "visita_o_pregunta_propiedad"
    )

    if property_id not in estado["propiedades_enviadas"]:
        estado["propiedades_enviadas"].append(property_id)

    return await construir_respuesta_fichas(
        estado,
        [propiedad],
        especifica=True,
    )
async def iniciar_visita(
    estado: dict,
    posicion: Optional[int],
    codigo: Optional[str] = None,
) -> str:
    propiedad = resolver_propiedad_contexto(
        estado,
        posicion=posicion,
        codigo=codigo,
    )

    if not propiedad:
        if len(estado.get("ultimo_lote", [])) > 1:
            return (
                "¡Con gusto! ¿Cuál propiedad deseas visitar: "
                "la primera, segunda, tercera, cuarta o quinta?"
            )

        return (
            "¡Con gusto! Primero indícame cuál propiedad deseas "
            "visitar o envíame su código."
        )

    property_id = str(propiedad.get("id") or "")

    estado["propiedad_interes"] = propiedad
    estado["propiedad_activa_id"] = property_id
    estado["ultima_propiedad_consultada_id"] = property_id

    # El rol es obligatorio antes de capturar el lead o entregar
    # los datos del captador.
    if not rol_esta_confirmado(estado):
        return solicitar_rol_para_accion(
            estado,
            "agendar_visita",
            propiedad_id=property_id,
            posicion=posicion,
        )

    if estado.get("rol") == "colega_inmobiliario":
        await sincronizar_google_sheet()

        cruce = cruzar_captador_con_sheet(
            propiedad.get(
                "captador_wasi",
                "",
            )
        )

        estado["accion_pendiente_rol"] = None
        estado["pregunta_pendiente"] = None
        estado["estado_conversacion"] = (
            "visita_colega"
        )

        if cruce.get("telefono"):
            return (
                "Perfecto, colega. El captador de esta propiedad es "
                f"{cruce.get('nombre')}. Puedes coordinar la visita "
                "directamente por WhatsApp aquí: "
                f"https://wa.me/{cruce['telefono']}"
            )

        return (
            "Identifiqué la propiedad, pero el teléfono del captador "
            "no aparece actualmente en el directorio. Si quieres, "
            "puedo notificar al equipo administrativo para que "
            "te ayude a coordinar la visita."
        )

    # El rol fue confirmado como cliente.
    estado["accion_pendiente_rol"] = None
    estado["pregunta_pendiente"] = None
    estado["objetivo"] = "captura_lead"
    estado["estado_conversacion"] = "captura_lead"
    estado["motivo_contacto"] = "Agendar visita"

    return mensaje_solicitud_datos_lead(
        estado,
        saludo=True,
    )

def asunto_colega_valido(valor: Any) -> bool:
    asunto = str(valor or "").strip()

    if len(asunto) < 4 or len(asunto) > 160:
        return False

    asunto_norm = normalizar_texto(asunto)

    respuestas_invalidas = {
        "si",
        "no",
        "ok",
        "gracias",
        "hola",
        "listo",
        "perfecto",
        "ninguno",
        "no se",
    }

    return asunto_norm not in respuestas_invalidas


def extraer_asunto_colega(
    mensaje: str,
) -> Optional[str]:
    texto = str(mensaje or "").strip()

    coincidencia = re.search(
        r"(?:asunto|motivo|tema)\s*[:\-]\s*(.+)",
        texto,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not coincidencia:
        return None

    asunto = coincidencia.group(1).strip()

    # Si después escribió teléfono u otros datos, se eliminan
    # del asunto.
    asunto = re.split(
        r"\b(?:whatsapp|telefono|teléfono|numero|número)\s*[:\-]",
        asunto,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()

    return asunto if asunto_colega_valido(asunto) else None


def mensaje_solicitud_asunto_colega() -> str:
    return (
        "Perfecto, colega. Ahora escribe brevemente el asunto "
        "de tu solicitud para enviarlo al equipo administrativo. "
        "Por ejemplo: “Apoyo para coordinar visita en El Trigal” "
        "o “Consulta sobre comisión compartida”."
    )

async def iniciar_atencion_humana(
    estado: dict,
    mensaje: str,
) -> str:
    if not rol_esta_confirmado(estado):
        return solicitar_rol_para_accion(
            estado,
            "hablar_con_humano",
            mensaje_original=mensaje,
        )
    if estado.get("rol") == "colega_inmobiliario":
        # Cada nueva solicitud humana de un colega debe tener
        # un asunto nuevo escrito expresamente por él.
        estado["asunto_contacto_colega"] = None
        estado["asunto_contacto_colega_escrito"] = False
        estado["mensaje_contacto_colega"] = mensaje

        preparar_contacto_colega_desde_estado(estado)

        # Permite capturar nombre y WhatsApp si el colega los
        # incluyó en el mismo mensaje inicial.
        actualizar_contacto_colega_desde_mensaje(
            estado,
            mensaje,
        )

        # Solo se acepta asunto en el primer mensaje si viene
        # escrito explícitamente como "Asunto:", "Motivo:" o "Tema:".
        asunto_extraido = extraer_asunto_colega(
            mensaje
        )

        if asunto_extraido:
            estado["asunto_contacto_colega"] = (
                asunto_extraido
            )
            estado["asunto_contacto_colega_escrito"] = True

        estado["objetivo"] = (
            "captura_contacto_colega"
        )
        estado["estado_conversacion"] = (
            "captura_contacto_colega"
        )

        faltantes_contacto = (
            datos_contacto_colega_faltantes(estado)
        )

        if faltantes_contacto:
            estado["pregunta_pendiente"] = (
                "datos_contacto_colega"
            )

            return mensaje_solicitud_contacto_colega(
                estado
            )

        if not (
            estado.get(
                "asunto_contacto_colega_escrito",
                False,
            )
            and asunto_colega_valido(
                estado.get("asunto_contacto_colega")
            )
        ):
            estado["pregunta_pendiente"] = (
                "asunto_contacto_colega"
            )
            return mensaje_solicitud_asunto_colega()

        enviado = await notificar_colega_administradores(
            estado,
            mensaje,
        )

        if enviado:
            estado["objetivo"] = "colega_notificado"
            estado["estado_conversacion"] = (
                "colega_notificado"
            )
            estado["pregunta_pendiente"] = None

            return (
                "Claro, colega. Ya envié tu solicitud al equipo "
                "administrativo con el asunto y tus datos de "
                "contacto. Te atenderán directamente por WhatsApp."
            )

        return (
            "Registré tu solicitud y tus datos, pero no pude "
            "confirmar el envío por Telegram. Puedes intentarlo "
            "nuevamente en unos minutos."
        )

    # Flujo de clientes: conserva el round robin.
    estado["objetivo"] = "captura_lead"
    estado["estado_conversacion"] = "captura_lead"
    estado["motivo_contacto"] = (
        "Solicita atención humana"
    )

    return mensaje_solicitud_datos_lead(
        estado,
        saludo=True,
    )

async def procesar_captura_contacto_colega(
    estado: dict,
    mensaje: str,
) -> str:
    pregunta_pendiente = estado.get(
        "pregunta_pendiente"
    )

    # Garantiza compatibilidad con sesiones creadas antes de
    # agregar esta nueva bandera.
    estado.setdefault(
        "asunto_contacto_colega_escrito",
        False,
    )

    # --------------------------------------------------------
    # EL BOT HABÍA SOLICITADO EL ASUNTO
    # --------------------------------------------------------

    if pregunta_pendiente == "asunto_contacto_colega":
        asunto = str(mensaje or "").strip()

        asunto_extraido = extraer_asunto_colega(
            mensaje
        )

        if asunto_extraido:
            asunto = asunto_extraido

        if not asunto_colega_valido(asunto):
            estado["asunto_contacto_colega"] = None
            estado["asunto_contacto_colega_escrito"] = False

            return (
                "No pude identificar un asunto válido. "
                "Escribe brevemente el motivo de tu solicitud. "
                "Por ejemplo: “Apoyo para coordinar una visita "
                "en El Trigal”."
            )

        estado["asunto_contacto_colega"] = asunto
        estado["asunto_contacto_colega_escrito"] = True
        estado["pregunta_pendiente"] = None

    else:
        # En cualquier otra etapa, primero se intentan capturar
        # nombre y WhatsApp.
        actualizar_contacto_colega_desde_mensaje(
            estado,
            mensaje,
        )

        # Solo se toma como asunto si fue identificado con una
        # etiqueta explícita: "Asunto:", "Motivo:" o "Tema:".
        asunto_extraido = extraer_asunto_colega(
            mensaje
        )

        if asunto_extraido:
            estado["asunto_contacto_colega"] = (
                asunto_extraido
            )
            estado["asunto_contacto_colega_escrito"] = True

    # --------------------------------------------------------
    # VALIDAR DATOS DEL COLEGA
    # --------------------------------------------------------

    faltantes_contacto = (
        datos_contacto_colega_faltantes(estado)
    )

    if faltantes_contacto:
        estado["pregunta_pendiente"] = (
            "datos_contacto_colega"
        )

        return mensaje_solicitud_contacto_colega(
            estado
        )

    # --------------------------------------------------------
    # EXIGIR ASUNTO ESCRITO POR EL COLEGA
    # --------------------------------------------------------

    asunto_fue_escrito = estado.get(
        "asunto_contacto_colega_escrito",
        False,
    )
    asunto = estado.get(
        "asunto_contacto_colega"
    )

    if not (
        asunto_fue_escrito
        and asunto_colega_valido(asunto)
    ):
        estado["asunto_contacto_colega"] = None
        estado["asunto_contacto_colega_escrito"] = False
        estado["pregunta_pendiente"] = (
            "asunto_contacto_colega"
        )

        return mensaje_solicitud_asunto_colega()

    # --------------------------------------------------------
    # ENVIAR SOLICITUD
    # --------------------------------------------------------

    enviado = await notificar_colega_administradores(
        estado,
        estado.get("mensaje_contacto_colega")
        or mensaje,
    )

    if enviado:
        estado["objetivo"] = "colega_notificado"
        estado["estado_conversacion"] = (
            "colega_notificado"
        )
        estado["pregunta_pendiente"] = None

        return (
            "¡Perfecto, colega! Ya envié tu solicitud al equipo "
            "administrativo con el asunto, tu nombre y el enlace "
            "directo de WhatsApp. Te atenderán directamente."
        )

    return (
        "Registré tus datos y el asunto, pero no pude confirmar "
        "el envío por Telegram. Puedes intentarlo nuevamente "
        "en unos minutos."
    )

async def procesar_captura_lead(
    estado: dict,
    mensaje: str,
) -> str:
    actualizar_lead_desde_mensaje(estado, mensaje)

    if lead_completo(estado):
        estado["lead_confirmacion_pendiente"] = True
        return mensaje_confirmacion_lead(estado)

    return mensaje_solicitud_datos_lead(estado)


async def responder_consulta_mettryc(
    mensaje: str,
    estado: dict,
    decision: DecisionAgente,
) -> str:
    mensajes = [
        {
            "role": "system",
            "content": (
                "Eres Paty de Mettryc Realty. Responde de forma "
                "natural, breve y profesional usando únicamente la "
                "base de conocimiento suministrada. No inventes "
                "horarios, teléfonos, servicios ni condiciones."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "pregunta": mensaje,
                    "base_conocimiento": BASE_CONOCIMIENTO_METTRYC,
                },
                ensure_ascii=False,
            ),
        },
    ]

    class RespuestaConocimiento(BaseModel):
        respuesta: str

    resultado = await llamar_openrouter_json(
        RespuestaConocimiento,
        mensajes,
        temperatura=0.2,
        max_tokens=600,
    )

    if isinstance(resultado, RespuestaConocimiento):
        return resultado.respuesta.strip()

    return decision.mensaje or (
        "Puedo ayudarte con nuestras oficinas, honorarios, "
        "servicios inmobiliarios o el programa de reclutamiento. "
        "¿Qué información necesitas?"
    )

RESPUESTAS_CORTAS_AFIRMATIVAS = {
    "si",
    "sii",
    "sip",
    "claro",
    "claro que si",
    "por supuesto",
    "dale",
    "de una",
    "correcto",
    "afirmativo",
    "si quiero",
    "bueno",
}

RESPUESTAS_CORTAS_NEGATIVAS = {
    "no",
    "nop",
    "negativo",
    "no gracias",
    "para nada",
    "mejor no",
    "ahorita no",
}

RESPUESTAS_CORTAS_ACEPTACION = {
    "ok",
    "okay",
    "esta bien",
    "está bien",
    "bien",
    "fino",
    "perfecto",
    "excelente",
    "listo",
    "vale",
    "entiendo",
    "entendido",
}


def ultima_respuesta_asistente(
    estado: dict,
) -> Optional[str]:
    for mensaje in reversed(
        estado.get("historial", [])
    ):
        if mensaje.get("role") == "assistant":
            contenido = str(
                mensaje.get("content") or ""
            ).strip()

            if contenido:
                return contenido

    return None


def es_respuesta_corta_contextual(
    texto: str,
) -> bool:
    normalizado = normalizar_texto(texto)

    if not normalizado:
        return False

    respuestas_conocidas = {
        normalizar_texto(respuesta)
        for respuesta in (
            RESPUESTAS_CORTAS_AFIRMATIVAS
            | RESPUESTAS_CORTAS_NEGATIVAS
            | RESPUESTAS_CORTAS_ACEPTACION
        )
    }

    if normalizado in respuestas_conocidas:
        return True

    frases_adicionales = [
        "te dije que si",
        "ya te dije que si",
        "si por favor",
        "si esta bien",
        "si perfecto",
        "no esta bien",
        "no quiero",
    ]

    return any(
        frase in normalizado
        for frase in frases_adicionales
    )


def clasificar_respuesta_corta(
    texto: str,
) -> str:
    normalizado = normalizar_texto(texto)

    afirmativas = {
        normalizar_texto(valor)
        for valor in RESPUESTAS_CORTAS_AFIRMATIVAS
    }
    negativas = {
        normalizar_texto(valor)
        for valor in RESPUESTAS_CORTAS_NEGATIVAS
    }
    aceptaciones = {
        normalizar_texto(valor)
        for valor in RESPUESTAS_CORTAS_ACEPTACION
    }

    if (
        normalizado in afirmativas
        or "te dije que si" in normalizado
        or "ya te dije que si" in normalizado
    ):
        return "afirmativa"

    if normalizado in negativas:
        return "negativa"

    if normalizado in aceptaciones:
        return "aceptacion"

    return "ambigua"


def respuesta_corta_fallback(
    estado: dict,
    mensaje_usuario: str,
    ultimo_mensaje_bot: str,
) -> str:
    significado = clasificar_respuesta_corta(
        mensaje_usuario
    )
    ultimo_norm = normalizar_texto(
        ultimo_mensaje_bot
    )
    pregunta_pendiente = estado.get(
        "pregunta_pendiente"
    )

    if pregunta_pendiente == "sin_resultados":
        if significado in {
            "afirmativa",
            "aceptacion",
        }:
            return (
                "¡Perfecto! ¿Prefieres que ampliemos la zona "
                "o que ajustemos alguna condición de la búsqueda, "
                "como el presupuesto o las características?"
            )

        if significado == "negativa":
            estado["pregunta_pendiente"] = None
            estado.pop(
                "detalle_pregunta_pendiente",
                None,
            )

            return (
                "Entendido. Si luego quieres intentar otra búsqueda "
                "o consultar algo más, con gusto te ayudo."
            )

    if pregunta_pendiente == "ajustar_busqueda":
        return (
            "Claro. ¿Qué deseas ajustar: la ubicación, el "
            "presupuesto, el tipo de propiedad o alguna "
            "característica?"
        )

    if (
        "ampliar la zona" in ultimo_norm
        and "ajustar" in ultimo_norm
    ):
        if significado in {
            "afirmativa",
            "aceptacion",
        }:
            return (
                "¡Excelente! Para continuar, dime cuál prefieres: "
                "¿ampliamos la zona o ajustamos la búsqueda?"
            )

        if significado == "negativa":
            return (
                "Entendido. ¿Hay algo más en lo que pueda ayudarte?"
            )

    if "o" in ultimo_norm and "?" in ultimo_mensaje_bot:
        if significado in {
            "afirmativa",
            "aceptacion",
        }:
            return (
                "Perfecto. Para continuar necesito que me indiques "
                "cuál de las opciones prefieres."
            )

    if significado == "negativa":
        return (
            "Entendido. ¿Hay algo más en lo que pueda ayudarte?"
        )

    return (
        "Perfecto. Para asegurarme de entenderte bien, "
        "¿puedes indicarme brevemente cómo deseas continuar?"
    )

async def interpretar_respuesta_corta_con_ia(
    estado: dict,
    mensaje_usuario: str,
) -> Optional[str]:
    if not es_respuesta_corta_contextual(
        mensaje_usuario
    ):
        return None

    ultimo_mensaje_bot = ultima_respuesta_asistente(
        estado
    )

    if not ultimo_mensaje_bot:
        return None

    pregunta_pendiente = estado.get(
        "pregunta_pendiente"
    )
    detalle_pendiente = estado.get(
        "detalle_pregunta_pendiente"
    )

    contexto = {
        "ultimo_mensaje_del_chatbot": ultimo_mensaje_bot,
        "respuesta_corta_del_usuario": mensaje_usuario,
        "pregunta_pendiente": pregunta_pendiente,
        "detalle_pregunta_pendiente": detalle_pendiente,
        "estado_comercial": construir_estado_para_ia(
            estado
        ),
    }

    mensajes = [
        {
            "role": "system",
            "content": (
                "Eres el intérprete contextual de Paty, la asesora "
                "virtual de Mettryc Realty. Debes comprender una "
                "respuesta corta del usuario usando principalmente "
                "la última pregunta que hizo el chatbot.\n\n"
                "REGLAS:\n"
                "1. Un 'sí', 'ok', 'está bien', 'fino', 'perfecto' "
                "o equivalente confirma disposición a continuar, "
                "pero no necesariamente selecciona entre dos "
                "alternativas.\n"
                "2. Si el chatbot ofreció dos opciones y la persona "
                "solo respondió 'sí', pregunta cuál de las dos "
                "prefiere.\n"
                "3. Reformula la pregunta de manera diferente. "
                "Nunca repitas literalmente el último mensaje.\n"
                "4. Si la respuesta es negativa, reconoce la "
                "decisión y ofrece ayuda con otra cosa.\n"
                "5. No inventes filtros ni elijas una alternativa "
                "por la persona.\n"
                "6. La respuesta debe ser natural, cálida y breve.\n"
                "7. Devuelve exclusivamente el JSON solicitado."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                contexto,
                ensure_ascii=False,
            ),
        },
    ]

    resultado = await llamar_openrouter_json(
        InterpretacionRespuestaCortaIA,
        mensajes,
        temperatura=0.1,
        max_tokens=400,
    )

    if isinstance(
        resultado,
        InterpretacionRespuestaCortaIA,
    ):
        respuesta = resultado.respuesta.strip()

        # Evita que la IA repita exactamente la misma pregunta.
        if (
            respuesta
            and normalizar_texto(respuesta)
            != normalizar_texto(
                ultimo_mensaje_bot
            )
        ):
            if not resultado.mantener_pregunta_pendiente:
                estado["pregunta_pendiente"] = None
                estado.pop(
                    "detalle_pregunta_pendiente",
                    None,
                )

            return respuesta

    return respuesta_corta_fallback(
        estado,
        mensaje_usuario,
        ultimo_mensaje_bot,
    )

async def procesar_eleccion_sin_resultados(
    estado: dict,
    mensaje: str,
) -> Optional[str]:
    if estado.get("pregunta_pendiente") not in {
        "sin_resultados",
        "sin_resultados_eleccion",
    }:
        return None

    normalizado = normalizar_texto(mensaje)

    frases_ampliar = [
        "ampliar",
        "ampliar zona",
        "la zona",
        "zona",
        "buscar en otra zona",
        "otra zona",
        "toda la ciudad",
        "sin zona",
    ]

    frases_ajustar = [
        "ajustar",
        "ajustar busqueda",
        "ajustar la busqueda",
        "cambiar criterios",
        "cambiar condiciones",
        "presupuesto",
        "caracteristicas",
    ]

    eligio_ampliar = any(
        normalizado == frase
        or frase in normalizado
        for frase in frases_ampliar
    )

    eligio_ajustar = any(
        normalizado == frase
        or frase in normalizado
        for frase in frases_ajustar
    )

    if eligio_ampliar and not eligio_ajustar:
        filtros = estado.get("filtros", {})

        filtros["zona"] = None

        sin_preferencia = estado.setdefault(
            "sin_preferencia",
            [],
        )

        if "zona" not in sin_preferencia:
            sin_preferencia.append("zona")

        estado["propiedades_enviadas"] = []
        estado["ultimo_lote"] = []
        estado["pregunta_pendiente"] = None
        estado.pop(
            "detalle_pregunta_pendiente",
            None,
        )

        if criterios_suficientes(estado):
            return await mostrar_propiedades(
                estado
            )

        return obtener_pregunta_faltante(
            estado
        )

    if eligio_ajustar:
        estado["pregunta_pendiente"] = (
            "ajustar_busqueda"
        )
        estado["detalle_pregunta_pendiente"] = {
            "tipo": "seleccion_de_campo",
            "opciones": [
                "ubicacion",
                "presupuesto",
                "tipo_propiedad",
                "caracteristicas",
            ],
        }

        return (
            "Claro. ¿Qué prefieres ajustar: la ubicación, "
            "el presupuesto, el tipo de propiedad o alguna "
            "característica?"
        )

    return None

async def continuar_accion_pendiente_rol(
    estado: dict,
) -> Optional[str]:
    if not rol_esta_confirmado(estado):
        return None

    pendiente = estado.get(
        "accion_pendiente_rol"
    )

    if not isinstance(pendiente, dict):
        return None

    estado["accion_pendiente_rol"] = None
    estado["pregunta_pendiente"] = None

    tipo = pendiente.get("tipo")
    property_id = pendiente.get("propiedad_id")
    posicion = pendiente.get("posicion")

    if tipo == "agendar_visita":
        return await iniciar_visita(
            estado,
            posicion=posicion,
            codigo=property_id,
        )

    if tipo == "solicitar_captador":
        return await atender_solicitud_captador(
            estado,
            posicion=posicion,
            codigo=property_id,
        )

    if tipo == "hablar_con_humano":
        return await iniciar_atencion_humana(
            estado,
            pendiente.get("mensaje_original")
            or "Solicita atención humana",
        )

    return None

async def procesar_mensaje(
    sender: str,
    mensaje: str,
) -> str:
    estado = obtener_sesion(sender)
    texto = str(mensaje or "").strip()
    texto_norm = normalizar_texto(texto)

    async def finalizar(respuesta: str) -> str:
        agregar_historial(estado, "user", texto)

        if respuesta:
            agregar_historial(
                estado,
                "assistant",
                respuesta,
            )

        guardar_sesion(sender, estado)
        return respuesta

    # --------------------------------------------------------
    # REINICIO DE BÚSQUEDA
    # --------------------------------------------------------

    if texto_norm == "/reiniciar":
        estado = reiniciar_busqueda(estado)

        respuesta = (
            "🧹 Reinicié la búsqueda. "
            "¿Cómo puedo ayudarte ahora?"
        )

        agregar_historial(estado, "user", texto)
        agregar_historial(
            estado,
            "assistant",
            respuesta,
        )
        guardar_sesion(sender, estado)
        return respuesta

    # --------------------------------------------------------
    # AGRADECIMIENTOS Y CIERRE NATURAL
    # --------------------------------------------------------

    if (
        es_agradecimiento_simple(texto)
        and estado.get("objetivo") != "captura_lead"
        and not estado.get("lead_confirmacion_pendiente")
    ):
        return await finalizar(
            respuesta_agradecimiento()
        )

    # --------------------------------------------------------
    # RESPUESTAS DETERMINISTAS A LA PREGUNTA DE ROL
    # --------------------------------------------------------

    rol_detectado = interpretar_respuesta_rol(
        texto,
        estado,
    )

    if rol_detectado:
        estado["pregunta_pendiente"] = None
        estado["rol_confirmado"] = True
        estado["confianza_rol"] = 1.0

        respuesta_pendiente = (
            await continuar_accion_pendiente_rol(
                estado
            )
        )

        if respuesta_pendiente:
            return await finalizar(
                respuesta_pendiente
            )

    rol_confirmado_en_este_mensaje = rol_detectado

    # --------------------------------------------------------
    # CONFIRMACIÓN FINAL DEL LEAD
    # --------------------------------------------------------

    if estado.get("lead_confirmacion_pendiente"):
        if es_respuesta_afirmativa(texto):
            respuesta = await completar_y_asignar_lead(
                estado
            )
            return await finalizar(respuesta)

        if es_respuesta_negativa(texto):
            estado["lead_confirmacion_pendiente"] = False
            estado["lead_confirmado"] = False

            return await finalizar(
                "Entendido. Indícame qué dato quieres corregir "
                "y lo actualizaré."
            )

        # Si no respondió claramente Sí o No, se permite que
        # indique directamente el dato que desea corregir.
        estado["lead_confirmacion_pendiente"] = False
        estado["lead_confirmado"] = False

    # --------------------------------------------------------
    # CLIENTE ACEPTA ATENCIÓN DE UN AGENTE
    # --------------------------------------------------------

    if (
        estado.get("pregunta_pendiente")
        == "ofrecer_agente_cliente"
    ):
        if es_respuesta_afirmativa(texto):
            estado["pregunta_pendiente"] = None
            estado.pop(
                "detalle_pregunta_pendiente",
                None,
            )
            estado["objetivo"] = "captura_lead"
            estado["estado_conversacion"] = (
                "captura_lead"
            )
            estado["motivo_contacto"] = (
                "Solicita información de una propiedad"
            )

            respuesta = mensaje_solicitud_datos_lead(
                estado,
                saludo=True,
            )
            return await finalizar(respuesta)

        if es_respuesta_negativa(texto):
            estado["pregunta_pendiente"] = None
            estado.pop(
                "detalle_pregunta_pendiente",
                None,
            )

            return await finalizar(
                "Entendido. Si quieres hacer otra pregunta sobre "
                "la propiedad o consultar otras opciones, con "
                "gusto te ayudo."
            )

        return await finalizar(
            "Para confirmar, ¿quieres que te asignemos un agente "
            "para ayudarte con esta propiedad?"
        )

    # --------------------------------------------------------
    # CAPTURA DE DATOS DEL COLEGA
    # --------------------------------------------------------

    if (
        estado.get("objetivo")
        == "captura_contacto_colega"
    ):
        respuesta = (
            await procesar_captura_contacto_colega(
                estado,
                texto,
            )
        )
        return await finalizar(respuesta)

    # --------------------------------------------------------
    # CAPTURA DE LEAD PRIORITARIA
    # --------------------------------------------------------
    # Debe ejecutarse antes de detectar códigos. Así los números
    # dentro de un correo nunca se interpretan como propiedad.

    if estado.get("objetivo") == "captura_lead":
        if not rol_esta_confirmado(estado):
            propiedad = resolver_propiedad_contexto(
                estado
            )

            estado["objetivo"] = "evaluar_resultados"

            return await finalizar(
                solicitar_rol_para_accion(
                    estado,
                    "agendar_visita",
                    propiedad_id=(
                        str(propiedad.get("id"))
                        if propiedad
                        else None
                    ),
                )
            )

        if estado.get("rol") == "colega_inmobiliario":
            propiedad = resolver_propiedad_contexto(
                estado
            )

            if propiedad:
                respuesta = await iniciar_visita(
                    estado,
                    posicion=None,
                    codigo=str(propiedad.get("id")),
                )
                return await finalizar(respuesta)

            estado["objetivo"] = "conversar"

            return await finalizar(
                "Como colega, no necesito los datos personales "
                "de tu cliente. Indícame la propiedad que deseas "
                "consultar y te compartiré los datos del captador."
            )

        respuesta = await procesar_captura_lead(
            estado,
            texto,
        )
        return await finalizar(respuesta)

    # --------------------------------------------------------
    # SOLICITUD EXPLÍCITA DE ATENCIÓN HUMANA
    # --------------------------------------------------------

    if solicita_humano(texto):
        respuesta = await iniciar_atencion_humana(
            estado,
            texto,
        )
        return await finalizar(respuesta)

    # --------------------------------------------------------
    # SOLICITUD EXPLÍCITA DE VISITA
    # --------------------------------------------------------

    frases_visita_adicionales = [
        "ir a verla",
        "ir a verlo",
        "quiero conocerla",
        "quiero conocerlo",
        "quiero visitar",
        "deseo visitarla",
        "deseo visitarlo",
        "podemos verla",
        "podemos verlo",
        "cuando puedo verla",
        "cuando puedo verlo",
    ]

    visita_evidente = (
        solicita_visita(texto)
        or any(
            frase in texto_norm
            for frase in frases_visita_adicionales
        )
    )

    if visita_evidente:
        posicion_visita = detectar_posicion(texto)

        estado["pregunta_pendiente"] = None

        respuesta = await iniciar_visita(
            estado,
            posicion=posicion_visita,
            codigo=None,
        )
        return await finalizar(respuesta)

    # --------------------------------------------------------
    # RESPUESTA A UNA PREGUNTA PENDIENTE DE VISITA
    # --------------------------------------------------------

    pregunta_pendiente = estado.get("pregunta_pendiente")

    if (
        pregunta_pendiente == "confirmar_visita"
        and es_respuesta_afirmativa(texto)
    ):
        estado["pregunta_pendiente"] = None

        respuesta = await iniciar_visita(
            estado,
            posicion=None,
            codigo=None,
        )
        return await finalizar(respuesta)

    if (
        pregunta_pendiente
        == "visita_o_pregunta_propiedad"
        and es_respuesta_afirmativa(texto)
    ):
        estado["pregunta_pendiente"] = (
            "visita_o_pregunta_propiedad"
        )

        return await finalizar(
            "Perfecto. ¿Quieres agendar una visita o prefieres "
            "hacer una pregunta sobre la propiedad?"
        )
        estado["pregunta_pendiente"] = None

        respuesta = await iniciar_visita(
            estado,
            posicion=None,
            codigo=None,
        )
        return await finalizar(respuesta)

    # Protección adicional si el usuario reclama que ya confirmó.
    if (
        "te dije que si" in texto_norm
        or "ya te dije que si" in texto_norm
    ):
        estado["pregunta_pendiente"] = None

        respuesta = await iniciar_visita(
            estado,
            posicion=None,
            codigo=None,
        )
        return await finalizar(respuesta)

    # --------------------------------------------------------
    # ELECCIONES PENDIENTES DESPUÉS DE NO ENCONTRAR RESULTADOS
    # --------------------------------------------------------

    respuesta_eleccion = (
        await procesar_eleccion_sin_resultados(
            estado,
            texto,
        )
    )

    if respuesta_eleccion:
        return await finalizar(
            respuesta_eleccion
        )

    # --------------------------------------------------------
    # TRADUCTOR CONTEXTUAL DE RESPUESTAS CORTAS
    # --------------------------------------------------------

    if es_respuesta_corta_contextual(texto):
        respuesta_contextual = (
            await interpretar_respuesta_corta_con_ia(
                estado,
                texto,
            )
        )

        if respuesta_contextual:
            # Si todavía falta escoger entre ampliar o ajustar,
            # se mantiene el contexto para el próximo mensaje.
            if estado.get("pregunta_pendiente") == "sin_resultados":
                estado["pregunta_pendiente"] = (
                    "sin_resultados_eleccion"
                )

            return await finalizar(
                respuesta_contextual
            )

    # --------------------------------------------------------
    # PREGUNTAS SOBRE UNA PROPIEDAD ACTIVA
    # --------------------------------------------------------
    # Se ejecuta antes de la IA para conservar automáticamente
    # el inmueble recibido por Mercado Libre o por código.

    if es_pregunta_sobre_propiedad_activa(
        texto,
        estado,
    ):
        propiedad = resolver_propiedad_contexto(
            estado
        )

        if propiedad:
            respuesta = await responder_pregunta_propiedad(
                estado,
                propiedad,
                texto,
            )
            return await finalizar(respuesta)

    # --------------------------------------------------------
    # RESPUESTA AL TIPO DE INFORMACIÓN
    # --------------------------------------------------------

    if (
        estado.get("pregunta_pendiente")
        == "tipo_informacion"
    ):
        normalizado_info = normalizar_texto(texto)

        palabras_propiedad = [
            "propiedad",
            "inmueble",
            "casa",
            "apartamento",
            "local",
            "oficina",
            "anuncio",
            "publicacion",
            "precio",
            "disponible",
            "alquiler",
            "venta",
        ]

        palabras_empresa = [
            "mettryc",
            "inmobiliaria",
            "empresa",
            "oficina",
            "oficinas",
            "honorarios",
            "servicios",
            "ubicacion",
            "direccion",
        ]

        palabras_trabajo = [
            "trabajo",
            "trabajar",
            "empleo",
            "ingresar",
            "reclutamiento",
            "asesor",
            "curso",
            "credenciales",
        ]

        if any(
            palabra in normalizado_info
            for palabra in palabras_propiedad
        ):
            estado["pregunta_pendiente"] = None
            estado.pop(
                "detalle_pregunta_pendiente",
                None,
            )

            codigo = (
                extraer_codigo_mercadolibre(texto)
                or extraer_codigo_inmueble(
                    texto,
                    permitir_solo_digitos=False,
                )
            )

            if codigo:
                respuesta = await mostrar_inmueble_especifico(
                    estado,
                    codigo,
                )
            else:
                estado["esperando_codigo"] = True
                respuesta = mensaje_solicitud_codigo_inmueble()

            return await finalizar(respuesta)

        if any(
            palabra in normalizado_info
            for palabra in (
                palabras_empresa
                + palabras_trabajo
            )
        ):
            estado["pregunta_pendiente"] = None
            estado.pop(
                "detalle_pregunta_pendiente",
                None,
            )

            # Se continúa hasta la IA para que responda usando
            # la base de conocimiento.
        else:
            return await finalizar(
                "Para orientarte mejor, dime si la información "
                "es sobre una propiedad, sobre Mettryc Realty "
                "o sobre cómo trabajar con nosotros."
            )

    # --------------------------------------------------------
    # SOLICITUD AMBIGUA DE INFORMACIÓN
    # --------------------------------------------------------

    if (
        es_solicitud_informacion_ambigua(texto)
        and not tiene_propiedad_en_contexto(estado)
    ):
        estado["pregunta_pendiente"] = (
            "tipo_informacion"
        )
        estado["detalle_pregunta_pendiente"] = {
            "opciones": [
                "propiedad",
                "empresa",
                "trabajo",
            ]
        }

        return await finalizar(
            "¡Claro! ¿Qué información deseas conocer? "
            "¿Es sobre una propiedad, sobre Mettryc Realty "
            "o sobre cómo trabajar con nosotros?"
        )

    # --------------------------------------------------------
    # CONSULTA DIRECTA DE INVENTARIO
    # --------------------------------------------------------

    if es_consulta_inventario_directa(texto):
        codigo_directo = (
            extraer_codigo_mercadolibre(texto)
            or extraer_codigo_inmueble(
                texto,
                permitir_solo_digitos=False,
            )
        )

        if codigo_directo:
            respuesta = await mostrar_inmueble_especifico(
                estado,
                codigo_directo,
            )
            return await finalizar(respuesta)

        if tiene_propiedad_en_contexto(estado):
            propiedad = resolver_propiedad_contexto(
                estado
            )

            if propiedad:
                respuesta = await responder_pregunta_propiedad(
                    estado,
                    propiedad,
                    texto,
                )
                return await finalizar(respuesta)

        estado["esperando_codigo"] = True
        estado["pregunta_pendiente"] = (
            "codigo_consulta_inventario"
        )

        return await finalizar(
            mensaje_solicitud_codigo_inmueble()
        )

    # --------------------------------------------------------
    # MERCADO LIBRE
    # --------------------------------------------------------

    codigo_mercadolibre = extraer_codigo_mercadolibre(
        texto
    )

    if codigo_mercadolibre:
        respuesta = await mostrar_inmueble_especifico(
            estado,
            codigo_mercadolibre,
        )
        return await finalizar(respuesta)

    # --------------------------------------------------------
    # ESPERANDO CÓDIGO DE UN ANUNCIO
    # --------------------------------------------------------

    if estado.get("esperando_codigo"):
        codigo = (
            extraer_codigo_mercadolibre(texto)
            or extraer_codigo_inmueble(
                texto,
                permitir_solo_digitos=True,
            )
        )

        if codigo:
            estado["esperando_codigo"] = False

            if (
                estado.get("pregunta_pendiente")
                == "codigo_para_captador"
            ):
                estado["pregunta_pendiente"] = None

                propiedad = (
                    await consultar_detalle_propiedad_wasi(
                        codigo
                    )
                )

                if propiedad:
                    property_id = str(
                        propiedad.get("id")
                    )

                    estado["propiedad_interes"] = (
                        propiedad
                    )
                    estado["propiedad_activa_id"] = (
                        property_id
                    )
                    estado[
                        "ultima_propiedad_consultada_id"
                    ] = property_id
                    estado["ultimo_lote"] = [
                        property_id
                    ]

                    respuesta = (
                        await atender_solicitud_captador(
                            estado,
                            codigo=property_id,
                        )
                    )

                else:
                    estado["esperando_codigo"] = True
                    estado["pregunta_pendiente"] = (
                        "codigo_para_captador"
                    )

                    respuesta = (
                        "No encontré una propiedad activa con "
                        "ese código. Revisa el número o envíame "
                        "el enlace del anuncio."
                    )

            else:
                respuesta = (
                    await mostrar_inmueble_especifico(
                        estado,
                        codigo,
                    )
                )

        else:
            respuesta = (
                "No logré identificar el código. Suele aparecer "
                "al final del título o en la descripción del "
                "anuncio. Por ejemplo: AM-9935990 o 9935990."
            )

        return await finalizar(respuesta)

    # --------------------------------------------------------
    # ANÁLISIS PRINCIPAL CON IA
    # --------------------------------------------------------

    try:
        decision = await decidir_con_ia(
            texto,
            estado,
        )
    except Exception as exc:
        logger.exception(
            "Fallo analizando mensaje con IA: %s",
            type(exc).__name__,
        )
        decision = decision_fallback(
            texto,
            estado,
        )

    cambio_ia = aplicar_decision(
        estado,
        decision,
        texto,
    )

    # La IA no puede reemplazar el rol que fue confirmado
    # directamente por el usuario en este mismo mensaje.
    if rol_confirmado_en_este_mensaje:
        estado["rol"] = rol_confirmado_en_este_mensaje
        estado["confianza_rol"] = 1.0

    cambio_tecnico = aplicar_extracciones_tecnicas(
        estado,
        texto,
    )

    cambio_sin_preferencia = (
        aplicar_sin_preferencia_desde_texto(
            estado,
            texto,
        )
    )

    # --------------------------------------------------------
    # PROTECCIÓN PERMANENTE DEL ROL CONFIRMADO
    # Punto 20
    # --------------------------------------------------------

    if estado.get("rol") in {
        "cliente",
        "colega_inmobiliario",
    }:
        estado["confianza_rol"] = 1.0

    # --------------------------------------------------------
    # RESOLVER ACCIÓN, POSICIÓN Y CÓDIGO
    # --------------------------------------------------------

    accion = decision.accion.tipo

    posicion = (
        decision.accion.posicion
        or decision.referencia_propiedad.posicion
        or detectar_posicion(texto)
    )

    # Fuera de esperando_codigo, un número aislado no puede
    # convertirse automáticamente en código de propiedad.
    codigo = (
        decision.accion.codigo
        or decision.referencia_propiedad.codigo
        or extraer_codigo_inmueble(
            texto,
            permitir_solo_digitos=False,
        )
    )

    # Las intenciones técnicas evidentes tienen prioridad sobre
    # la acción propuesta por la IA.

    if solicita_humano(texto):
        accion = "hablar_con_humano"

    elif visita_evidente:
        accion = "agendar_visita"

    elif pide_mas_opciones(texto):
        accion = "mostrar_mas_propiedades"

    elif codigo:
        accion = "buscar_por_codigo"

    cambio_busqueda = bool(
        cambio_ia
        or cambio_tecnico
        or cambio_sin_preferencia
    )

    # Solo se limpia la paginación cuando realmente se ejecutará
    # una nueva búsqueda con criterios modificados.
    if (
        accion == "buscar_propiedades"
        and cambio_busqueda
        and estado.get("estado_conversacion")
        not in {
            "consulta_propiedad",
            "propiedades_mostradas",
        }
    ):
        estado["propiedades_enviadas"] = []
        estado["ultimo_lote"] = []

    # --------------------------------------------------------
    # EJECUCIÓN DE ACCIONES
    # --------------------------------------------------------

    if accion == "reiniciar_busqueda":
        estado = reiniciar_busqueda(estado)

        respuesta = (
            "Perfecto, iniciemos una nueva búsqueda. "
            "¿Qué tipo de propiedad necesitas?"
        )

    elif accion == "hablar_con_humano":
        respuesta = await iniciar_atencion_humana(
            estado,
            texto,
        )

    elif accion == "agendar_visita":
        estado["pregunta_pendiente"] = None

        respuesta = await iniciar_visita(
            estado,
            posicion,
            codigo,
        )

    elif accion == "buscar_por_codigo":
        if codigo:
            respuesta = await mostrar_inmueble_especifico(
                estado,
                codigo,
            )
        else:
            estado["esperando_codigo"] = True
            estado["estado_conversacion"] = (
                "esperando_codigo"
            )

            respuesta = (
                "Envíame el código que aparece al final del título "
                "o en la descripción del anuncio."
            )

    elif accion == "pedir_codigo_inmueble":
        estado["esperando_codigo"] = True
        estado["estado_conversacion"] = (
            "esperando_codigo"
        )

        respuesta = (
            "Para ubicar la propiedad exacta, envíame el "
            "código que aparece al final del título o en la "
            "descripción del anuncio. Por ejemplo: "
            "AM-9935990 o 9935990."
        )

    elif accion == "mostrar_mas_propiedades":
        if estado.get("propiedades_enviadas"):
            respuesta = await mostrar_propiedades(
                estado
            )
        else:
            respuesta = (
                "Primero cuéntame qué tipo de propiedad buscas, "
                "si es para comprar o alquilar y la ubicación."
            )

    elif accion == "seleccionar_propiedad":
        propiedad = resolver_propiedad_contexto(
            estado,
            posicion=posicion,
            codigo=codigo,
        )

        if propiedad:
            estado["propiedad_interes"] = propiedad
            estado["propiedad_activa_id"] = (
                propiedad.get("id")
            )
            estado["ultima_propiedad_consultada_id"] = (
                propiedad.get("id")
            )
            estado["pregunta_pendiente"] = (
                "visita_o_pregunta_propiedad"
            )

            if (
                estado.get("rol")
                == "colega_inmobiliario"
            ):
                respuesta = await iniciar_visita(
                    estado,
                    posicion,
                    codigo,
                )
            else:
                respuesta = (
                    "Perfecto, ya identifiqué esa propiedad. "
                    "¿Quieres agendar una visita o preguntarme "
                    "algo específico sobre ella?"
                )
        else:
            respuesta = (
                "No pude identificar la propiedad. "
                "Indícame el número de la opción o su código."
            )


    elif accion == "consultar_propiedad":
        propiedad = resolver_propiedad_contexto(
            estado,
            posicion=posicion,
            codigo=codigo,
        )

        if not propiedad:
            if len(estado.get("ultimo_lote", [])) > 1:
                respuesta = (
                    "¿Sobre cuál propiedad quieres consultar: "
                    "la primera, segunda, tercera, cuarta o quinta?"
                )
            else:
                respuesta = (
                    "Envíame el código de la propiedad para "
                    "consultar sus detalles."
                )
        else:
            pregunta = (
                decision.solicitudes.pregunta_sobre_propiedad
                or texto
            )

            respuesta = await responder_pregunta_propiedad(
                estado,
                propiedad,
                pregunta,
            )


    elif accion == "solicitar_captador":
        respuesta = await atender_solicitud_captador(
            estado,
            posicion=posicion,
            codigo=codigo,
        )


    elif accion == "consultar_mettryc":
        respuesta = await responder_consulta_mettryc(
            texto,
            estado,
            decision,
        )

    elif accion == "buscar_propiedades":
        pregunta_faltante = obtener_pregunta_faltante(
            estado
        )

        if pregunta_faltante:
            respuesta = pregunta_faltante

        elif criterios_suficientes(estado):
            respuesta = await mostrar_propiedades(
                estado
            )

        else:
            respuesta = (
                "Necesito confirmar algunos detalles antes de "
                "buscar las propiedades."
            )

    elif accion == "pedir_aclaracion":
        respuesta = decision.mensaje or (
            "Quiero ayudarte correctamente. "
            "¿Puedes darme un poco más de detalle?"
        )

    # --------------------------------------------------------
    # CONVERSACIÓN GENERAL O ACCIÓN RESPONDER
    # Punto 19
    # --------------------------------------------------------

    else:
        tiene_filtros = any(
            valor not in (None, "", [])
            for valor in estado.get(
                "filtros",
                {},
            ).values()
        )

        intenciones_busqueda = {
            "buscar_propiedades",
            "busqueda_inmueble",
            "buscar_inmueble",
            "consulta_inmobiliaria",
            "buscar_casa",
            "buscar_apartamento",
            "buscar_alquiler",
            "buscar_compra",
        }

        intencion_busqueda = bool(
            decision.intencion_principal
            in intenciones_busqueda
            or tiene_filtros
        )

        if intencion_busqueda:
            pregunta_faltante = obtener_pregunta_faltante(
                estado
            )

            if (
                criterios_suficientes(estado)
                and not pregunta_faltante
            ):
                respuesta = await mostrar_propiedades(
                    estado
                )

            elif pregunta_faltante:
                respuesta = pregunta_faltante

            else:
                respuesta = decision.mensaje or (
                    "Cuéntame un poco más sobre la propiedad "
                    "que necesitas."
                )

        else:
            # No se activa el flujo de búsqueda para consultas
            # corporativas, reclutamiento, oficinas, honorarios,
            # saludos, agradecimientos u otros temas generales.
            respuesta = decision.mensaje or (
                "¡Con gusto! ¿Hay algo más en lo que pueda "
                "ayudarte?"
            )

    if not respuesta:
        respuesta = (
            "¿Puedes contarme un poco más sobre lo que necesitas?"
        )

    return await finalizar(respuesta)


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
                "Error de inicialización tipo=%s detalle=%s",
                type(resultado).__name__,
                str(resultado)[:200],
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client

    http_client = httpx.AsyncClient(
        follow_redirects=True,
        trust_env=False,
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
        ),
        headers={
            "User-Agent": "Mettryc-Chatbot/3.0",
        },
    )

    tarea_inicializacion = asyncio.create_task(
        inicializar_datos()
    )

    yield

    if not tarea_inicializacion.done():
        tarea_inicializacion.cancel()

        try:
            await tarea_inicializacion
        except asyncio.CancelledError:
            pass

    await http_client.aclose()


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Mettryc Realty Paty",
    version="3.0.0",
    lifespan=lifespan,
)


def validar_api_key(api_key: Optional[str]) -> None:
    if not API_KEYS_AGENTES:
        raise HTTPException(
            status_code=503,
            detail="API_KEYS_AGENTES no está configurado.",
        )

    if not api_key or api_key not in API_KEYS_AGENTES:
        raise HTTPException(
            status_code=403,
            detail="Acceso denegado.",
        )


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {
        "service": "Mettryc Realty Paty",
        "version": "3.0.0",
        "status": "online",
    }


@app.get("/health")
async def health():
    ultima = inventory_cache.get("ultima_actualizacion")

    return {
        "status": "ok",
        "inventario": len(
            inventory_cache.get("inventario", [])
        ),
        "ultima_actualizacion_inventario": (
            ultima.isoformat()
            if ultima
            else None
        ),
        "agentes": len(sheets_cache.get("agentes", [])),
        "captadores": len(
            sheets_cache.get("captadores", {})
        ),
        "sesiones_memoria": len(sesiones),
        "modelo_principal": MODELO_AGENTE_PRINCIPAL,
        "persistencia": "memoria_del_proceso",
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
            inventory_cache.get("inventario", [])
        ),
        "agentes": len(sheets_cache.get("agentes", [])),
        "captadores": len(
            sheets_cache.get("captadores", {})
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
    locks_usuarios.pop(sender, None)

    return {
        "ok": True,
        "sender": sender,
    }


@app.get("/admin/status")
async def admin_status(
    x_api_key: Optional[str] = Header(
        default=None,
        alias="x-api-key",
    ),
):
    validar_api_key(x_api_key)

    return {
        "sesiones": len(sesiones),
        "propiedades": len(
            inventory_cache.get("inventario", [])
        ),
        "agentes": len(sheets_cache.get("agentes", [])),
        "captadores": len(
            sheets_cache.get("captadores", {})
        ),
        "round_robin_index": round_robin_index,
        "modelo_principal": MODELO_AGENTE_PRINCIPAL,
        "modelo_respaldo": MODELO_AGENTE_RESPALDO,
        "openrouter_configurado": bool(
            OPENROUTER_API_KEY
        ),
        "telegram_configurado": bool(
            TELEGRAM_BOT_TOKEN
        ),
        "wasi_configurado": bool(
            WASI_TOKEN and WASI_COMPANY_ID
        ),
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

    sender = str(payload.get("sender", "")).strip()
    mensaje = str(payload.get("message", "")).strip()
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

    if mensaje_es_duplicado(sender, message_id):
        return {"replies": []}

    if sender not in locks_usuarios:
        locks_usuarios[sender] = asyncio.Lock()

    try:
        if not inventory_cache.get("inventario"):
            await actualizar_inventario(force=True)

        elif inventario_necesita_actualizacion():
            asyncio.create_task(actualizar_inventario())

        if sheets_necesita_actualizacion():
            asyncio.create_task(
                sincronizar_google_sheet()
            )

    except Exception as exc:
        logger.error(
            "Error preparando datos tipo=%s detalle=%s",
            type(exc).__name__,
            str(exc)[:160],
        )

    try:
        async with locks_usuarios[sender]:
            respuesta = await procesar_mensaje(
                sender,
                mensaje,
            )

        if not respuesta:
            return {"replies": []}

        return {
            "replies": [
                {
                    "message": str(respuesta).replace(
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
                        "Disculpa, tuve un inconveniente procesando "
                        "tu mensaje. ¿Puedes intentarlo nuevamente?"
                    )
                }
            ]
        }
