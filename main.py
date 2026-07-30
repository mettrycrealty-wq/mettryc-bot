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

MAX_PROPIEDADES_POR_LOTE = int(os.getenv("MAX_PROPIEDADES_POR_LOTE", "3"))
MAX_EXCESO_PRESUPUESTO = float(os.getenv("MAX_EXCESO_PRESUPUESTO", "0.20"))
MAX_HISTORIAL = int(os.getenv("MAX_HISTORIAL", "24"))
DUPLICATE_TTL_SECONDS = int(os.getenv("DUPLICATE_TTL_SECONDS", "180"))

SENDER_ES_WHATSAPP = os.getenv("SENDER_ES_WHATSAPP", "true").lower() == "true"

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
    minutes=int(os.getenv("INTERVALO_ACTUALIZACION_SHEETS_MINUTOS", "60"))
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
    tipos_propiedad: List[str] = Field(default_factory=list)
    ciudad: Optional[str] = None
    zona: Optional[str] = None
    zonas: List[str] = Field(default_factory=list)
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
        "terreno": "terreno",
        "parcela": "terreno",
        "lote": "terreno",
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
        "el", "la", "los", "las", "de", "del", "en", "urb", "urbanizacion",
        "zona", "sector", "ciudad", "venezuela", "estado",
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
        r"\btengo\s+un\s+hijo\b", # Añadido para el caso de reclutamiento
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

    if any(re.search(patron, normalizado) for patron in patrones_cliente):
        return "cliente"

    if any(re.search(patron, normalizado) for patron in patrones_colega):
        return "colega_inmobiliario"

    return None


def solicita_humano(texto: str) -> bool:
    normalizado = normalizar_texto(texto)
    return any(normalizar_texto(frase) in normalizado for frase in FRASES_HUMANO)


def solicita_visita(texto: str) -> bool:
    normalizado = normalizar_texto(texto)
    return any(normalizar_texto(frase) in normalizado for frase in FRASES_VISITA)


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
        "quien es el captador",
        "quien capto la propiedad",
        "quien capto ese inmueble",
        "contacto del captador",
        "dame el contacto del captador",
        "pasame el contacto del captador",
        "telefono del captador",
        "numero del captador",
        "whatsapp del captador",
        "contacto del asesor",
        "quien lleva la propiedad",
        "quien lleva ese inmueble",
        "con quien coordino",
        "con quien puedo coordinar",
        "dame el contacto",
        "pasame el contacto",
    ]

    return any(frase in normalizado for frase in frases)

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


def detectar_tipos_propiedad(texto: str) -> List[str]:
    normalizado = normalizar_texto(texto)

    patrones = [
        ("apartamento", [r"\bapartamentos?\b", r"\baptos?\b", r"\bpenthouses?\b", r"\blofts?\b", r"\banexos?\b"]),
        ("casa", [r"\bcasas?\b", r"\bquintas?\b", r"\bchalets?\b", r"\bvillas?\b"]),
        ("townhouse", [r"\btownhouses?\b", r"\btown\s+houses?\b"]),
        ("oficina", [r"\boficinas?\b"]),
        ("local", [r"\blocal(?:es)?\b", r"\blocal(?:es)?\s+comercial(?:es)?\b"]),
        ("galpon", [r"\bgalpones?\b", r"\bdepositos?\b"]),
        ("terreno", [r"\bterrenos?\b", r"\blotes?\b", r"\bparcelas?\b"]),
    ]

    encontrados = []
    for tipo, expresiones in patrones:
        if any(re.search(expresion, normalizado) for expresion in expresiones):
            encontrados.append(tipo)

    return encontrados


def detectar_tipo_propiedad(texto: str) -> Optional[str]:
    tipos = detectar_tipos_propiedad(texto)
    return tipos[0] if tipos else None


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

        if numero >= 50:  # Umbral mínimo para considerar un número como presupuesto
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
    "pozo de agua": "pozo de agua",
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
            "tipos_propiedad": [],
            "ciudad": None,
            "zona": None,
            "zonas": [],
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
        "propuesta_alternativa": None,

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
        "tipos_propiedad": [],
        "ciudad": None,
        "zona": None,
        "zonas": [],
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
    estado["propuesta_alternativa"] = None # Nuevo campo

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
        parte for parte en [localidad, zona] if parte
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
# CATÁLOGO GEOGRÁFICO Y CANÓNICO
# ============================================================

FALLBACK_ZONAS_AMBIGUAS = {
    "el trigal": ["Valencia", "Cabudare"],
    "trigal": ["Valencia", "Cabudare"],
    "trigal norte": ["Valencia", "Cabudare"],
    "los caobos": ["Valencia", "Caracas"],
    "centro": ["Valencia", "Barquisimeto", "Caracas"],
}

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
        "manongo", "mañongo", "la granja", "las quintas de naguanagua",
        "la campina", "la campiña", "tazajal", "el rincon", "el rincón",
        "carialinda", "el saman", "el samán", "manantial", "tarapio",
        "barbula", "bárbula",
    },
    "San Diego": {
        "los jarales", "la esmeralda", "el remanso", "valle de oro",
        "parque comercio", "pueblo de san diego", "casco san diego",
        "campo solo", "el morro", "la cumaca", "yuma",
    },
    "Valencia": {
        "el trigal", "trigal norte", "trigal centro", "trigal sur",
        "el parral", "prebo", "la vina", "la viña", "el vinedo",
        "el viñedo", "los colorados", "agua blanca", "los mangos",
        "guataparo", "el bosque", "la trigaleña", "las chimeneas",
        "la alegria", "la alegría", "los caobos", "la isabelica",
        "flor amarillo", "valles de camoruco", "kerdell", "las acacias",
    },
    "Tocuyito": {
        "tocuyito", "libertador", "el libertador", "la pocaterra",
    },
    "Guacara": {
        "guacara", "yagua", "ciudad alianza",
    },
    "Los Guayos": {
        "los guayos", "paraparal",
    },
    "Puerto Cabello": {
        "puerto cabello", "patanemo", "borburata",
    },
    "Barquisimeto": {
        "barquisimeto", "este de barquisimeto", "oeste de barquisimeto",
        "cabudare", "el parral barquisimeto",
    },
    "Cabudare": {
        "cabudare", "la piedad", "agua viva",
    },
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

        for zona_item in re.split(r"[\/|·–,]", zona_completa): # Renombrada 'zona' a 'zona_item' para evitar colisión
            zona = zona_item.strip() # Usa la variable 'zona' aquí
            zona_norm = normalizar_texto(zona)

            if len(zona_norm) < 3:
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


def detectar_ciudad_canonica(texto: Any) -> Optional[str]:
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


def detectar_zona_ciudad(texto: str) -> Dict[str, Any]:
    normalizado = normalizar_texto(texto)
    resultado: Dict[str, Any] = {}

    if not normalizado:
        return resultado

    ciudad_explicita = detectar_ciudad_canonica(normalizado)

    if ciudad_explicita:
        resultado["ciudad"] = ciudad_explicita

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

    zonas_detectadas = []

    for zona_norm, zona in (
        catalogo_geografico["frases_zonas"]
    ):
        if contiene_termino(
            normalizado,
            zona_norm,
        ):
            # Excluir estados o regiones amplias si se detecta una ciudad explícita
            if (
                ciudad_explicita
                and normalizar_texto(zona) == normalizar_texto(ciudad_explicita)
            ):
                continue
            zonas_detectadas.append(zona)

    if zonas_detectadas:
        resultado["zona"] = zonas_detectadas[0]
        resultado["zonas"] = zonas_detectadas

    if (
        ciudad_explicita
        and resultado.get("zona")
        and normalizar_texto(resultado["zona"])
        == normalizar_texto(ciudad_explicita)
    ):
        resultado.pop("zona", None)
        resultado.pop("zonas", None)
        resultado.pop("ambiguedad", None)
        resultado.pop("ciudades_posibles", None)
        return resultado

    if resultado.get("zona"):
        ciudades = sorted(
            obtener_ciudades_para_zona(
                resultado["zona"]
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

            if (
                ciudad_detectada_norm
                and ciudad_detectada_norm not in ciudades_norm
            ):
                resultado.pop("ciudad", None)
                resultado["ambiguedad"] = True
                resultado["ciudades_posibles"] = ciudades

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
    estado["ultima_propiedad_consultada_id"] = (
        detalle.get("id")
    )
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


RESPUESTAS_CORTAS_AFIRMATIVAS = {
    "si", "sii", "sip", "claro", "correcto", "exacto",
    "afirmativo", "por supuesto", "ok", "okay", "listo",
    "dale", "perfecto",
}

RESPUESTAS_CORTAS_NEGATIVAS = {
    "no", "no gracias", "negativo", "incorrecto",
    "no es correcto", "para nada",
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


def es_respuesta_afirmativa(texto: str) -> bool:
    normalizado = normalizar_texto(texto)

    if not normalizado:
        return False

    respuestas = {
        "si", "sii", "siii", "sip", "claro", "claro que si",
        "por supuesto", "por supuesto que si", "correcto",
        "es correcto", "si es correcto", "todo correcto",
        "los datos estan correctos", "los datos son correctos",
        "afirmativo", "ok", "okay", "listo", "dale", "perfecto",
        "esta bien", "de acuerdo", "confirmo", "enviame", "mandalas",
        "quiero verlas", "quiero verlos",
    }

    if normalizado in respuestas:
        return True

    frases = [
        "te dije que si",
        "ya te dije que si",
        "si por favor",
        "si quiero",
        "si deseo",
        "si confirmo",
        "si correcto",
        "si esta correcto",
        "si es correcto",
        "envialas", # Añadido contextualmente
        "mandalas", # Añadido contextualmente
    ]

    if any(frase in normalizado for frase in frases):
        return True

    return normalizado.startswith("si ")


def es_respuesta_negativa(texto: str) -> bool:
    normalizado = normalizar_texto(texto)

    return (
        normalizado in RESPUESTAS_CORTAS_NEGATIVAS
        or normalizado.startswith("no ")
        or "no quiero" in normalizado
        or "mejor no" in normalizado
        or "ahorita no" in normalizado
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

# ============================================================
# CLASES DE RESPUESTA Y FALLBACKS DE LA IA
# ============================================================

RESPUESTAS_CORTAS_AFIRMATIVAS = {
    "si", "sii", "sip", "claro", "correcto", "exacto",
    "afirmativo", "por supuesto", "ok", "okay", "listo",
    "dale", "perfecto",
}

RESPUESTAS_CORTAS_NEGATIVAS = {
    "no", "no gracias", "negativo", "incorrecto",
    "no es correcto", "para nada",
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


def es_respuesta_afirmativa(texto: str) -> bool:
    normalizado = normalizar_texto(texto)

    if not normalizado:
        return False

    respuestas = {
        "si", "sii", "siii", "sip", "claro", "claro que si",
        "por supuesto", "por supuesto que si", "correcto",
        "es correcto", "si es correcto", "todo correcto",
        "los datos estan correctos", "los datos son correctos",
        "afirmativo", "ok", "okay", "listo", "dale", "perfecto",
        "esta bien", "de acuerdo", "confirmo",
    }

    if normalizado in respuestas:
        return True

    frases = [
        "te dije que si",
        "ya te dije que si",
        "si por favor",
        "si quiero",
        "si deseo",
        "si confirmo",
        "si correcto",
        "si esta correcto",
        "si es correcto",
    ]

    if any(
        frase in normalizado
        for frase in frases
    ):
        return True

    return normalizado.startswith("si ")


def es_respuesta_negativa(texto: str) -> bool:
    normalizado = normalizar_texto(texto)

    return (
        normalizado in RESPUESTAS_CORTAS_NEGATIVAS
        or normalizado.startswith("no ")
        or "no quiero" in normalizado
        or "mejor no" in normalizado
        or "ahorita no" in normalizado
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

# ============================================================
# ORQUESTADOR PRINCIPAL
# ============================================================

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
                "¡Excelente! Para continuar, necesito que me indiques "
                "cuál de las dos opciones prefieres."
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
                "Perfecto. Para ser más preciso, ¿podrías indicarme "
                "cuál de las opciones prefieres?"
            )

    if significado == "negativa":
        return (
            "Entendido. ¿Hay algo más en lo que pueda ayudarte?"
        )

    return (
        "Perfecto. Para asegurarme de entenderte bien, ¿podrías "
        "reiterarme tu preferencia?"
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

# ============================================================
# APLICACIÓN FASTAPI Y ENDPOINTS
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
        or payload.get("messageId")
        or payload.get("message-id")
        or payload.get("wamid")
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

    if message_id:
        if mensaje_es_duplicado(
            sender,
            message_id,
        ):
            logger.info(
                "Mensaje duplicado ignorado sender=%s id=%s",
                sender[-4:],
                message_id[-12:],
            )
            return {"replies": []}
    else:
        logger.debug(
            "Mensaje sin message_id; se procesa sin deduplicación "
            "sender=%s",
            sender[-4:],
        )

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
