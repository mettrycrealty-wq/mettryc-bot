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

OPENROUTER_API_KEY = os.getenv(
    "OPENROUTER_API_KEY",
    "",
)

WASI_TOKEN = os.getenv(
    "WASI_TOKEN",
    "",
)

WASI_COMPANY_ID = os.getenv(
    "WASI_COMPANY_ID",
    "",
)

GOOGLE_SHEET_TURNOS_URL = os.getenv(
    "GOOGLE_SHEET_TURNOS_URL",
    "",
)

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
)

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
    os.getenv(
        "OPENROUTER_TIMEOUT",
        "30",
    )
)

WASI_TIMEOUT = float(
    os.getenv(
        "WASI_TIMEOUT",
        "40",
    )
)

SHEETS_TIMEOUT = float(
    os.getenv(
        "SHEETS_TIMEOUT",
        "20",
    )
)

TELEGRAM_TIMEOUT = float(
    os.getenv(
        "TELEGRAM_TIMEOUT",
        "15",
    )
)

MAX_PROPIEDADES_POR_LOTE = int(
    os.getenv(
        "MAX_PROPIEDADES_POR_LOTE",
        "3",
    )
)

MAX_EXCESO_PRESUPUESTO = float(
    os.getenv(
        "MAX_EXCESO_PRESUPUESTO",
        "0.20",
    )
)

MAX_HISTORIAL = int(
    os.getenv(
        "MAX_HISTORIAL",
        "16",
    )
)

DUPLICATE_TTL_SECONDS = int(
    os.getenv(
        "DUPLICATE_TTL_SECONDS",
        "180",
    )
)

SENDER_ES_WHATSAPP = (
    os.getenv(
        "SENDER_ES_WHATSAPP",
        "true",
    ).lower()
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
        os.getenv(
            "TELEGRAM_ADMIN_ID",
            "",
        ),
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
# PALABRAS CLAVE RECONOCIDAS
# ============================================================

CARACTERISTICAS_RECONOCIDAS = {
    "aire acondicionado",
    "amoblado",
    "ascensor",
    "balcon",
    "caney",
    "cocina empotrada",
    "estacionamiento techado",
    "gimnasio",
    "jardin",
    "maletero",
    "parrillera",
    "piscina",
    "piso bajo",
    "planta baja",
    "planta electrica",
    "pozo de agua",
    "salon de fiestas",
    "semi amoblado",
    "terraza",
    "vigilancia",
}


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

    # Estos campos se conservan con el sufijo "_min" por
    # compatibilidad, pero representan la cantidad deseada.
    # El buscador aceptará exactamente esa cantidad o +/- 1.
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
# UTILIDADES GENERALES
# ============================================================

def normalizar_texto(valor: Any) -> str:
    if valor is None:
        return ""

    texto = str(valor).strip().lower()
    texto = unicodedata.normalize(
        "NFD",
        texto,
    )

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

    return re.sub(
        r"\s+",
        " ",
        texto,
    ).strip()


def normalizar_nombre(valor: Any) -> str:
    palabras = re.findall(
        r"[A-Za-zÀ-ÖØ-ÿ'’-]+",
        str(valor or ""),
    )

    return " ".join(
        palabra[:1].upper()
        + palabra[1:].lower()
        for palabra in palabras
    )


def convertir_float(valor: Any) -> float:
    try:
        if valor in [
            None,
            "",
            "N/D",
        ]:
            return 0.0

        return float(valor)

    except (
        TypeError,
        ValueError,
    ):
        return 0.0


def convertir_entero(valor: Any) -> int:
    try:
        if valor in [
            None,
            "",
            "N/D",
        ]:
            return 0

        return int(float(valor))

    except (
        TypeError,
        ValueError,
    ):
        return 0


def formato_moneda(valor: Any) -> str:
    numero = convertir_float(valor)

    if numero <= 0:
        return "N/D"

    return f"${numero:,.0f}".replace(
        ",",
        ".",
    )


def limpiar_telefono(valor: Any) -> str:
    return re.sub(
        r"\D",
        "",
        str(valor or ""),
    )


def normalizar_telefono(
    valor: Any,
) -> Optional[str]:
    telefono = limpiar_telefono(valor)

    if telefono.startswith("00"):
        telefono = telefono[2:]

    if (
        telefono.startswith("0")
        and len(telefono) == 11
    ):
        telefono = "58" + telefono[1:]

    if (
        len(telefono) == 10
        and telefono.startswith("4")
    ):
        telefono = "58" + telefono

    if 10 <= len(telefono) <= 15:
        return telefono

    return None


def extraer_telefono(
    texto: str,
) -> Optional[str]:
    coincidencia = re.search(
        r"(\+?\d[\d\s\-()]{7,}\d)",
        texto or "",
    )

    if not coincidencia:
        return None

    return normalizar_telefono(
        coincidencia.group(1)
    )


def extraer_correo(
    texto: str,
) -> Optional[str]:
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

    if any(
        palabra in bloqueadas
        for palabra in palabras
    ):
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
            texto = texto.replace(
                ".",
                "",
            )
            texto = texto.replace(
                ",",
                ".",
            )
        else:
            texto = texto.replace(
                ",",
                "",
            )

    elif "." in texto:
        if len(texto.split(".")[-1]) == 3:
            texto = texto.replace(
                ".",
                "",
            )

    elif "," in texto:
        if len(texto.split(",")[-1]) == 3:
            texto = texto.replace(
                ",",
                "",
            )
        else:
            texto = texto.replace(
                ",",
                ".",
            )

    return convertir_float(texto)


def normalizar_tipo_propiedad(
    valor: Any,
) -> str:
    texto = normalizar_texto(valor)

    equivalencias = {
        "apto": "apartamento",
        "aptos": "apartamento",
        "apart": "apartamento",
        "apartamento": "apartamento",
        "apartamentos": "apartamento",

        "casa": "casa",
        "casas": "casa",

        "town house": "townhouse",
        "town houses": "townhouse",
        "townhouse": "townhouse",
        "townhouses": "townhouse",
        "towhouse": "townhouse",
        "tohouse": "townhouse",

        "aptoquinta": "apartoquinta",
        "aptoquintas": "apartoquinta",
        "aparto quinta": "apartoquinta",
        "aparto quintas": "apartoquinta",
        "apartoquinta": "apartoquinta",
        "apartoquintas": "apartoquinta",

        "penthouse": "penthouse",
        "penthouses": "penthouse",

        "quinta": "quinta",
        "quintas": "quinta",

        "oficina": "oficina",
        "oficinas": "oficina",

        "local": "local",
        "locales": "local",

        "galpon": "galpon",
        "galpones": "galpon",

        "terreno": "terreno",
        "terrenos": "terreno",
    }

    if texto in equivalencias:
        return equivalencias[texto]

    return texto


def pluralizar_tipo_propiedad(
    tipo: Any,
) -> str:
    tipo_normalizado = normalizar_tipo_propiedad(
        tipo
    )

    plurales = {
        "apartamento": "apartamentos",
        "casa": "casas",
        "townhouse": "townhouses",
        "apartoquinta": "apartoquintas",
        "penthouse": "penthouses",
        "quinta": "quintas",
        "oficina": "oficinas",
        "local": "locales",
        "galpon": "galpones",
        "terreno": "terrenos",
    }

    return plurales.get(
        tipo_normalizado,
        tipo_normalizado or "propiedades",
    )


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
        for token in normalizar_texto(
            valor
        ).split()
        if (
            len(token) >= 2
            and token not in bloqueadas
        )
    }


def convertir_caracteristicas(
    valor: Any,
) -> str:
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


# ============================================================
# ZONAS MÚLTIPLES (NUEVO)
# ============================================================

SEPARADOR_ZONAS_REGEX = re.compile(
    r"\s*(?:,|/|;|\bo\b|\bu\b|\by\b|\|)\s*",
    flags=re.IGNORECASE,
)

ALIAS_ZONAS = {
    "manongo": "Mañongo",
    "manonngo": "Mañongo",
    "trigalena": "Trigaleña",
    "trigaleña": "Trigaleña",
    "prebo": "Prebo",
}

def parsear_zonas(valor: Any) -> List[str]:
    if valor is None:
        return []

    if isinstance(valor, list):
        crudo = " , ".join(str(v) for v in valor if v)
    else:
        crudo = str(valor)

    partes = [
        p.strip()
        for p in SEPARADOR_ZONAS_REGEX.split(crudo)
        if p and p.strip()
    ]

    zonas: List[str] = []
    vistos: Set[str] = set()

    for parte in partes:
        norm = normalizar_texto(parte)
        if not norm:
            continue

        bonito = ALIAS_ZONAS.get(norm, normalizar_nombre(parte))

        if norm not in vistos:
            vistos.add(norm)
            zonas.append(bonito)

    return zonas


def detectar_zonas_en_texto(texto: str) -> List[str]:
    texto_norm = normalizar_texto(texto or "")
    detectadas: List[str] = []
    vistos: Set[str] = set()

    # 1) Alias directos
    for norm, bonito in ALIAS_ZONAS.items():
        if re.search(rf"\b{re.escape(norm)}\b", texto_norm):
            if norm not in vistos:
                vistos.add(norm)
                detectadas.append(bonito)

    # 2) Diccionario geográfico (si existe)
    try:
        from geografia import DICCIONARIO_GEOGRAFICO

        for _estado_geo, ciudades in DICCIONARIO_GEOGRAFICO.items():
            for ciudad, zonas in ciudades.items():
                ciudad_norm = normalizar_texto(ciudad)
                if ciudad_norm and re.search(rf"\b{re.escape(ciudad_norm)}\b", texto_norm):
                    if ciudad_norm not in vistos:
                        vistos.add(ciudad_norm)
                        detectadas.append(normalizar_nombre(ciudad))

                for zona in zonas:
                    zona_norm = normalizar_texto(zona)
                    if not zona_norm or len(zona_norm) < 3:
                        continue
                    if re.search(rf"\b{re.escape(zona_norm)}\b", texto_norm):
                        if zona_norm not in vistos:
                            vistos.add(zona_norm)
                            detectadas.append(normalizar_nombre(zona))
    except ImportError:
        pass

    return detectadas


def fusionar_zonas(
    zona_actual: Any,
    zona_nueva: Any,
    mensaje: str = "",
) -> str:
    lista_actual = parsear_zonas(zona_actual)
    lista_nueva = parsear_zonas(zona_nueva)
    lista_mensaje = detectar_zonas_en_texto(mensaje)

    final: List[str] = []
    vistos: Set[str] = set()

    for z in [*lista_actual, *lista_nueva, *lista_mensaje]:
        norm = normalizar_texto(z)
        if norm and norm not in vistos:
            vistos.add(norm)
            final.append(ALIAS_ZONAS.get(norm, z))

    return ", ".join(final)


# ============================================================
# DETECCIÓN DETERMINISTA DE CARACTERÍSTICAS
# ============================================================

def detectar_caracteristicas_mensaje(
    mensaje: str,
) -> List[str]:
    texto = normalizar_texto(mensaje)

    encontradas: List[str] = []

    for caracteristica in sorted(
        CARACTERISTICAS_RECONOCIDAS,
        key=len,
        reverse=True,
    ):
        caracteristica_normalizada = (
            normalizar_texto(caracteristica)
        )

        if caracteristica_normalizada in texto:
            encontradas.append(
                caracteristica_normalizada
            )

    return list(
        dict.fromkeys(encontradas)
    )


def detectar_espacios_mensaje(
    mensaje: str,
) -> Dict[str, int]:
    texto = normalizar_texto(mensaje)

    patrones = {
        "habitaciones_min": (
            r"\b(\d+)\s*"
            r"(?:habitaciones?|habitacion|cuartos?|hab)\b"
        ),
        "banos_min": (
            r"\b(\d+)\s*"
            r"(?:banos?|bano|bathrooms?)\b"
        ),
        "garajes_min": (
            r"\b(\d+)\s*"
            r"(?:garajes?|puestos?|estacionamientos?)\b"
        ),
    }

    resultado: Dict[str, int] = {}

    for campo, patron in patrones.items():
        coincidencia = re.search(
            patron,
            texto,
        )

        if coincidencia:
            resultado[campo] = int(
                coincidencia.group(1)
            )

    return resultado


# ============================================================
# DETECCIÓN DE CÓDIGOS, INTENCIONES Y ROLES
# ============================================================

def extraer_codigo_inmueble(
    mensaje: str,
) -> Optional[str]:
    patrones = [
        r"mettryc\.com/inmueble/(\d+)",

        r"\b(?:codigo|código|cod|inmueble)"
        r"\s*[:#-]?\s*(\d{4,})\b",

        r"\b(?:ALM|EJL|LR|JM|MFR|TH)-?"
        r"(\d{4,})\b",

        # Enlaces de Mercado Libre con el código interno
        # inmediatamente antes de "_JM".
        r"[-_/](\d{4,})-?_JM\b",
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

    if re.fullmatch(
        r"\d{6,10}",
        texto,
    ):
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
        if any(
            frase in texto
            for frase in frases
        ):
            return posicion

    return None


def pide_mas_opciones(
    mensaje: str,
) -> bool:
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

    return any(
        frase in texto
        for frase in frases
    )


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
        and not extraer_codigo_inmueble(
            mensaje
        )
    )


def detectar_rol_explicito(
    mensaje: str,
) -> Optional[str]:
    texto = normalizar_texto(mensaje)

    patrones_colega = [
        (
            r"\bsoy\s+"
            r"(asesor|asesora|agente|corredor|corredora|"
            r"broker|realtor)\b"
        ),
        r"\bsoy\s+colega\b",
        r"\btengo\s+un\s+cliente\b",
        r"\bbusco\s+para\s+un\s+cliente\b",
        r"\btrabajo\s+en\s+una\s+inmobiliaria\b",
        r"\bcomparto\s+comision\b",
        r"\bcliente\s+evaluando\b",
        r"\bperfil\s+juridico\b",
        r"\b(asesor|asesora)\s+inmobiliari[oa]\b",
        r"\breal\s+estate\b",
        r"\brealty\b",
        r"\brealtor\b",
        r"\bbroker\b",
        (
            r"@[a-z0-9_.-]+"
            r"(realty|realtor|inmuebles|inmobiliaria)"
        ),
    ]

    patrones_cliente = [
        (
            r"\bno\s+soy\s+"
            r"(asesor|agente|corredor|broker|realtor)\b"
        ),
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

    if any(
        palabra in texto
        for palabra in [
            "solicito",
            "solicitud",
            "requiero",
        ]
    ):
        jerga = [
            "canon",
            "perfil",
            "cliente",
            "asesor",
            "inmobiliaria",
            "realty",
            "realtor",
            "broker",
            "real estate",
            "negociacion",
            "hagamos negocios",
            "comision",
        ]

        if any(
            palabra in texto
            for palabra in jerga
        ):
            return "colega_inmobiliario"

    return None


# ============================================================
# SESIONES
# ============================================================

def crear_sesion(
    sender: str,
) -> dict:
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


def obtener_sesion(
    sender: str,
) -> dict:
    if sender not in sesiones:
        sesiones[sender] = crear_sesion(
            sender
        )

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
    contenido_limpio = str(
        contenido or ""
    ).strip()

    if len(contenido_limpio) > 5000:
        contenido_limpio = (
            contenido_limpio[:5000]
        )

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
    confianza = estado.get(
        "confianza_rol",
        0,
    )
    historial = estado.get(
        "historial",
        [],
    )
    numero_canal = estado.get(
        "numero_canal"
    )

    nuevo = crear_sesion(
        numero_canal or ""
    )

    nuevo["rol"] = rol
    nuevo["confianza_rol"] = confianza
    nuevo["historial"] = historial
    nuevo["numero_canal"] = numero_canal

    return nuevo


def verificar_caducidad_y_amnesia(
    estado: dict,
) -> dict:
    if not estado.get("actualizado_en"):
        return estado

    try:
        ultima_actualizacion = (
            datetime.fromisoformat(
                estado["actualizado_en"]
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        return estado

    ahora = datetime.utcnow()
    tiempo_inactivo = (
        ahora - ultima_actualizacion
    )

    rol = estado.get("rol") or "cliente"
    numero_canal = estado.get(
        "numero_canal",
        "",
    )

    if (
        rol == "colega_inmobiliario"
        and tiempo_inactivo > timedelta(hours=24)
    ):
        logger.info(
            "Sesión de colega caducada por inactividad "
            "numero=%s",
            numero_canal[-4:] if numero_canal else "N/D",
        )

        return crear_sesion(
            numero_canal
        )

    if (
        rol == "cliente"
        and tiempo_inactivo > timedelta(days=30)
    ):
        logger.info(
            "Sesión de cliente caducada por inactividad "
            "numero=%s",
            numero_canal[-4:] if numero_canal else "N/D",
        )

        return crear_sesion(
            numero_canal
        )

    if (
        rol == "cliente"
        and tiempo_inactivo > timedelta(hours=24)
        and estado.get("historial")
    ):
        logger.info(
            "Amnesia selectiva: historial eliminado, "
            "filtros conservados numero=%s",
            numero_canal[-4:] if numero_canal else "N/D",
        )

        estado["historial"] = []

    return estado


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
        mensajes_duplicados.pop(
            clave,
            None,
        )

    clave = f"{sender}:{message_id}"

    if clave in mensajes_duplicados:
        return True

    mensajes_duplicados[clave] = (
        ahora + DUPLICATE_TTL_SECONDS
    )

    return False


# ============================================================
# PREGUNTAS DINÁMICAS
# ============================================================

def obtener_pregunta_faltante(
    estado: dict,
) -> str:
    filtros = estado.get(
        "filtros",
        {},
    )

    if not filtros.get("tipo_operacion"):
        return "¿Es para compra o alquiler?"

    if not filtros.get("tipo_propiedad"):
        return (
            "¿Qué tipo de inmueble buscas? "
            "Por ejemplo: apartamento, casa o townhouse."
        )

    if not filtros.get("zona"):
        return (
            "¿En qué zona o zonas te gustaría buscar?"
        )

    if not filtros.get("presupuesto_max"):
        return (
            "¿Cuál es tu presupuesto estimado?"
        )

    return (
        "¿Hay alguna característica adicional "
        "que sea indispensable?"
    )


async def humanizar_texto_con_ia(
    estado: dict,
    instruccion_cruda: str,
    mensaje_usuario: str,
) -> str:
    api_key = os.getenv(
        "OPENROUTER_API_KEY",
        "",
    )

    if not api_key or http_client is None:
        return instruccion_cruda

    prompt_sistema = f"""
Eres Paty, la asistente VIP de Mettryc Realty.

El sistema determinó que necesitas comunicar o solicitar esto:
"{instruccion_cruda}"

TAREA:
1. Si el usuario acaba de dar un dato, valídalo brevemente.
2. Luego comunica exactamente la instrucción indicada.
3. No hagas más preguntas aparte de la indicada.
4. Sé cálida, profesional y muy breve.
5. Usa un máximo de 30 palabras.
"""

    mensajes = [
        {
            "role": "system",
            "content": prompt_sistema,
        }
    ]

    for mensaje_historial in estado.get(
        "historial",
        [],
    )[-4:]:
        mensajes.append(
            mensaje_historial
        )

    mensajes.append({
        "role": "user",
        "content": mensaje_usuario,
    })

    url = (
        "https://openrouter.ai/api/v1/"
        "chat/completions"
    )

    headers = {
        "Authorization": (
            f"Bearer {api_key}"
        ),
        "Content-Type": "application/json",
        "HTTP-Referer": "https://www.mettryc.com",
        "X-Title": "Mettryc Realty Paty",
    }

    payload = {
        "model": os.getenv(
            "MODELO_PRINCIPAL",
            "google/gemini-2.5-flash-lite",
        ),
        "messages": mensajes,
        "max_tokens": 150,
        "temperature": 0.4,
    }

    try:
        respuesta = await http_client.post(
            url,
            headers=headers,
            json=payload,
            timeout=15.0,
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

        contenido = str(
            contenido or ""
        ).strip()

        return (
            contenido
            if contenido
            else instruccion_cruda
        )

    except Exception as exc:
        logger.error(
            "Error humanizando texto tipo=%s detalle=%s",
            type(exc).__name__,
            str(exc)[:150],
        )

        return instruccion_cruda


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

    if http_client is None:
        logger.error(
            "El cliente HTTP todavía no está inicializado."
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
                    (
                        "https://api.wasi.co/v1/"
                        "property/search"
                    ),
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

                await asyncio.sleep(
                    2 ** intento
                )

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

            property_id = valor.get(
                "id_property"
            )

            if not property_id:
                continue

            usuario = (
                valor.get("user_data")
                or {}
            )

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
                "area": valor.get(
                    "area",
                    "N/D",
                ),
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
                    captador
                    or "Asesor Mettryc"
                ),
                "telefono_captador_wasi": (
                    usuario.get(
                        "phone",
                        "",
                    )
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

        inventory_cache["inventario"] = (
            propiedades
        )

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
    nombre_limpio = str(
        nombre or ""
    ).strip()

    telefono_limpio = normalizar_telefono(
        telefono
    )

    if nombre_limpio and telefono_limpio:
        resultado[nombre_limpio] = (
            telefono_limpio
        )


def procesar_captadores_sheet(
    payload: Any,
) -> Dict[str, str]:
    captadores: Dict[str, str] = {}

    if isinstance(payload, dict):
        for nombre, telefono in payload.items():
            if isinstance(telefono, dict):
                agregar_captador_sheet(
                    captadores,
                    telefono.get("nombre")
                    or nombre,
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
            if not isinstance(
                registro,
                dict,
            ):
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

    if http_client is None:
        logger.error(
            "El cliente HTTP todavía no está inicializado."
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

            agentes = payload.get(
                "agentes",
                [],
            )

            if not isinstance(agentes, list):
                agentes = []

            captadores = procesar_captadores_sheet(
                payload.get(
                    "captadores",
                    {},
                )
            )

            if not captadores:
                captadores = procesar_captadores_sheet(
                    payload.get(
                        "captadores_data",
                        [],
                    )
                    or payload.get(
                        "asesores",
                        [],
                    )
                )

            sheets_cache["agentes"] = agentes
            sheets_cache["captadores"] = (
                captadores
            )
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

    tokens_wasi = tokens_nombre(
        nombre_wasi
    )

    mejor = None
    mejor_score = 0.0

    for nombre_sheet, telefono in captadores.items():
        tokens_sheet = tokens_nombre(
            nombre_sheet
        )

        if not tokens_wasi or not tokens_sheet:
            continue

        interseccion = tokens_wasi.intersection(
            tokens_sheet
        )

        union = tokens_wasi.union(
            tokens_sheet
        )

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
                "tipo_coincidencia": (
                    "aproximada"
                ),
            }

    if mejor and mejor_score >= 0.65:
        return mejor

    return {
        "nombre": (
            nombre_wasi
            or "Asesor Mettryc"
        ),
        "telefono": None,
        "tipo_coincidencia": "no_encontrada",
    }


async def asignar_agente_round_robin() -> Optional[dict]:
    global round_robin_index

    await sincronizar_google_sheet()

    agentes = [
        deepcopy(agente)
        for agente in sheets_cache["agentes"]
        if (
            isinstance(agente, dict)
            and (
                agente.get("nombre")
                or agente.get("name")
            )
        )
    ]

    if not agentes:
        return None

    async with round_robin_lock:
        round_robin_index = (
            round_robin_index + 1
        ) % len(agentes)

        agente = agentes[
            round_robin_index
        ]

    if not agente.get("nombre"):
        agente["nombre"] = agente.get(
            "name"
        )

    return agente


# ============================================================
# OPENROUTER
# ============================================================

PROMPT_MAESTRO = """
Eres Paty, la asesora virtual de Mettryc Realty, la Primera
Tecnoinmobiliaria de Venezuela.

Hablas español venezolano de forma cálida, profesional, breve,
natural y humana. Nunca debes parecer un formulario.

TU FUNCIÓN

Debes comprender el mensaje usando toda la conversación y el estado
comercial. Extrae información, identifica la intención y decide qué
herramienta debe ejecutar el sistema.

No inventes propiedades, precios, enlaces, códigos, captadores,
agentes ni disponibilidad. El sistema mostrará las fichas.

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
   - tipo_operacion;
   - tipo_propiedad; y
   - zona o presupuesto_max.
7. Habitaciones, baños, garajes y características son opcionales, pero
   cuando el usuario los especifica debes registrarlos.
8. Si el usuario desea ver opciones antes de completar todo, puedes
   ejecutar buscar_propiedades.
9. Si cambia zona, presupuesto, tipo u otra condición, extrae el nuevo
   valor y usa buscar_propiedades.
10. Si dice no importa, cualquiera, ninguna, me da igual o no tengo,
    registra el campo correcto en campos_sin_preferencia.
11. Interpreta números según el contexto de la conversación.
12. No repitas saludos en todos los mensajes.
13. No repitas literalmente el mensaje del usuario.

REGLAS DE EXTRACCIÓN

- Extrae todas las zonas mencionadas.
- Si pide "Mañongo, Trigaleña o Prebo", guarda zona exactamente como:
  "Mañongo, Trigaleña, Prebo".
- No selecciones una sola zona cuando el usuario mencionó varias.
- habitaciones_min, banos_min y garajes_min representan la cantidad
  deseada, no un mínimo matemático.
- Extrae expresiones como piscina, piso bajo, planta baja, terraza,
  jardín, amoblado, ascensor, pozo de agua y planta eléctrica dentro
  de caracteristicas.
- No elimines una característica explícitamente solicitada.
- Tipo de propiedad y operación son filtros estrictos.
- Si dice "3 habitaciones, 2 baños y 2 garajes", registra exactamente
  3, 2 y 2 en los campos correspondientes.
- No agregues características que el usuario no haya solicitado.

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
  usa pedir_codigo_inmueble.
- No asumas que el identificador principal de Mercado Libre es el ID
  de Wasi.
- En un enlace de Mercado Libre, el código interno de la propiedad
  puede aparecer inmediatamente antes de "-_JM".
- Si recibes una solicitud tipo cadena y no especifica la operación,
  puedes inferirla por palabras explícitas o por el precio.
- Normalmente los alquileres tienen precios inferiores a $10.000,
  salvo inmuebles industriales de gran tamaño.

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


def limpiar_json_modelo(
    contenido: str,
) -> str:
    texto = str(
        contenido or ""
    ).strip()

    if texto.startswith("```"):
        texto = re.sub(
            r"^```(?:json)?\s*",
            "",
            texto,
            flags=re.IGNORECASE,
        )

        texto = re.sub(
            r"\s*```$",
            "",
            texto,
        )

    inicio = texto.find("{")
    fin = texto.rfind("}")

    if inicio >= 0 and fin > inicio:
        return texto[
            inicio:fin + 1
        ]

    return texto


async def llamar_openrouter_json(
    modelo_pydantic,
    mensajes: List[dict],
    temperatura: float = 0.2,
):
    if (
        not OPENROUTER_API_KEY
        or http_client is None
    ):
        return None

    headers = {
        "Authorization": (
            f"Bearer {OPENROUTER_API_KEY}"
        ),
        "Content-Type": "application/json",
        "HTTP-Referer": "https://www.mettryc.com",
        "X-Title": "Mettryc Realty Paty",
    }

    modelos = list(
        dict.fromkeys([
            MODELO_AGENTE_PRINCIPAL,
            MODELO_AGENTE_RESPALDO,
        ])
    )

    for modelo in modelos:
        formatos = [
            {
                "type": "json_schema",
                "json_schema": {
                    "name": (
                        modelo_pydantic.__name__
                    ),
                    "strict": True,
                    "schema": (
                        modelo_pydantic
                        .model_json_schema()
                    ),
                },
            },
            {
                "type": "json_object",
            },
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
                respuesta = await http_client.post(
                    (
                        "https://openrouter.ai/api/v1/"
                        "chat/completions"
                    ),
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
                        elemento.get(
                            "text",
                            "",
                        )
                        for elemento in contenido
                        if isinstance(
                            elemento,
                            dict,
                        )
                    )

                contenido = limpiar_json_modelo(
                    contenido
                )

                return (
                    modelo_pydantic
                    .model_validate_json(
                        contenido
                    )
                )

            except (
                ValidationError,
                ValueError,
                httpx.HTTPError,
            ) as exc:
                logger.warning(
                    "OpenRouter JSON modelo=%s formato=%s tipo=%s",
                    modelo,
                    response_format.get(
                        "type"
                    ),
                    type(exc).__name__,
                )

            except Exception as exc:
                logger.warning(
                    "OpenRouter modelo=%s tipo=%s detalle=%s",
                    modelo,
                    type(exc).__name__,
                    str(exc)[:150],
                )

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
        "objetivo": estado.get(
            "objetivo"
        ),
        "filtros": estado.get(
            "filtros"
        ),
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
                "id": propiedad_interes.get(
                    "id"
                ),
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
                if estado["lead"].get(
                    "whatsapp"
                )
                else None
            ),
            "numero_actual_disponible": bool(
                estado.get(
                    "numero_canal"
                )
            ),
        },
    }


async def decidir_con_ia(
    mensaje: str,
    estado: dict,
) -> DecisionAgente:
    contexto = {
        "estado_comercial": (
            construir_estado_para_ia(
                estado
            )
        ),
        "mensaje_actual": mensaje,
    }

    mensajes = [
        {
            "role": "system",
            "content": PROMPT_MAESTRO,
        },
        *estado.get(
            "historial",
            [],
        )[-12:],
        {
            "role": "user",
            "content": (
                "Analiza el mensaje actual usando el estado "
                "comercial. Devuelve la decisión estructurada."
                "\n\n"
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
            (
                "Redacta una introducción "
                "breve y natural."
            ),
            (
                "No inventes información "
                "de propiedades."
            ),
            (
                "No incluyas fichas, precios "
                "ni enlaces."
            ),
            (
                "El cierre debe invitar a "
                "seleccionar una opción o pedir más."
            ),
            (
                "Si es colega, menciona que la "
                "ficha incluye el captador."
            ),
            (
                "Si es cliente, no menciones "
                "datos de captadores."
            ),
        ],
    }

    mensajes = [
        {
            "role": "system",
            "content": (
                "Eres Paty de Mettryc Realty. "
                "Redacta el texto que acompaña "
                "fichas generadas por el sistema. "
                "Devuelve exclusivamente el JSON "
                "solicitado."
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
            "Claro, esta es la propiedad "
            "que consultaste:"
        )
    elif aproximadas:
        introduccion = (
            "Encontré estas opciones. Algunas "
            "tienen una diferencia permitida "
            "respecto a tus criterios:"
        )
    else:
        introduccion = (
            "Encontré estas opciones que "
            "encajan muy bien:"
        )

    if rol == "colega_inmobiliario":
        cierre = (
            "Puedes contactar al captador indicado "
            "en cada ficha o pedirme más opciones."
        )
    else:
        cierre = (
            "Dime cuál te interesa o escribe "
            "“más opciones”."
        )

    return TextoResultado(
        introduccion=introduccion,
        cierre=cierre,
    )


def decision_fallback(
    mensaje: str,
    estado: dict,
) -> DecisionAgente:
    codigo = extraer_codigo_inmueble(
        mensaje
    )
    posicion = detectar_posicion(
        mensaje
    )
    rol = detectar_rol_explicito(
        mensaje
    )

    if codigo:
        return DecisionAgente(
            mensaje="",
            rol=rol,
            confianza_rol=(
                1.0 if rol else 0
            ),
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
            confianza_rol=(
                1.0 if rol else 0
            ),
            intencion="mas_opciones",
            accion=AccionAgente(
                tipo=(
                    "mostrar_mas_propiedades"
                )
            ),
        )

    if posicion:
        return DecisionAgente(
            mensaje="",
            rol=rol,
            confianza_rol=(
                1.0 if rol else 0
            ),
            intencion="interes_propiedad",
            accion=AccionAgente(
                tipo="seleccionar_propiedad",
                posicion=posicion,
            ),
        )

    if menciona_anuncio_sin_codigo(
        mensaje
    ):
        return DecisionAgente(
            mensaje=(
                "¡Claro! Envíame el código que "
                "aparece en el anuncio o el enlace "
                "de la propiedad y te muestro la "
                "ficha exacta."
            ),
            rol=rol,
            confianza_rol=(
                1.0 if rol else 0
            ),
            intencion="anuncio_sin_codigo",
            accion=AccionAgente(
                tipo="pedir_codigo_inmueble"
            ),
        )

    return DecisionAgente(
        mensaje=(
            "¡Con gusto te ayudo! Cuéntame qué "
            "tipo de propiedad buscas, si es para "
            "comprar o alquilar y la zona que "
            "prefieres."
        ),
        rol=rol,
        confianza_rol=(
            1.0 if rol else 0
        ),
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
    texto = normalizar_texto(
        campo
    ).replace(
        " ",
        "_",
    )

    equivalencias = {
        "presupuesto": "presupuesto_max",
        "precio": "presupuesto_max",

        "habitaciones": "habitaciones_min",
        "habitacion": "habitaciones_min",
        "cuartos": "habitaciones_min",
        "cuarto": "habitaciones_min",

        "banos": "banos_min",
        "bano": "banos_min",

        "garaje": "garajes_min",
        "garajes": "garajes_min",
        "puestos": "garajes_min",
        "puesto": "garajes_min",
        "estacionamientos": "garajes_min",
        "estacionamiento": "garajes_min",
        "puestos_de_estacionamiento": (
            "garajes_min"
        ),

        "caracteristica": "caracteristicas",
        "caracteristicas_especiales": (
            "caracteristicas"
        ),

        "ubicacion": "zona",
        "tipo": "tipo_propiedad",
    }

    texto = equivalencias.get(
        texto,
        texto,
    )

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

    return (
        texto
        if texto in validos
        else None
    )


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
            >= estado.get(
                "confianza_rol",
                0,
            )
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
        valor = actualizaciones.get(
            campo
        )

        if valor in [
            None,
            "",
            [],
        ]:
            continue

        if campo == "tipo_propiedad":
            valor = normalizar_tipo_propiedad(
                valor
            )

        elif campo == "zona":
            zonas = dividir_zonas(
                valor
            )

            valor = ", ".join(
                zonas
            ) if zonas else str(valor).strip()

        elif campo == "presupuesto_max":
            valor = convertir_float(
                valor
            )

            if valor <= 0:
                continue

        elif campo in {
            "habitaciones_min",
            "banos_min",
            "garajes_min",
        }:
            valor = convertir_entero(
                valor
            )

            if valor < 0:
                continue

        elif campo == "caracteristicas":
            valor = list(
                dict.fromkeys(
                    normalizar_texto(
                        elemento
                    )
                    for elemento in valor
                    if normalizar_texto(
                        elemento
                    )
                )
            )

        anterior = estado[
            "filtros"
        ].get(campo)

        if anterior != valor:
            estado["filtros"][campo] = (
                valor
            )
            hubo_cambio_busqueda = True

            if campo in estado[
                "sin_preferencia"
            ]:
                estado[
                    "sin_preferencia"
                ].remove(campo)

    for campo_original in (
        decision.campos_sin_preferencia
    ):
        campo = normalizar_campo_sin_preferencia(
            campo_original
        )

        if not campo:
            continue

        if campo not in estado[
            "sin_preferencia"
        ]:
            estado[
                "sin_preferencia"
            ].append(campo)

        nuevo_valor = (
            []
            if campo == "caracteristicas"
            else None
        )

        if (
            estado["filtros"].get(campo)
            != nuevo_valor
        ):
            estado["filtros"][campo] = (
                nuevo_valor
            )
            hubo_cambio_busqueda = True

    lead = estado["lead"]

    nombre = actualizaciones.get(
        "nombre"
    )

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
        telefono = normalizar_telefono(
            telefono
        )

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
        and estado.get(
            "propiedades_enviadas"
        )
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
            or filtros.get(
                "presupuesto_max"
            )
        )
    )


def lead_completo(
    estado: dict,
) -> bool:
    lead = estado["lead"]

    return bool(
        nombre_valido(
            lead.get("nombre")
        )
        and correo_valido(
            lead.get("correo")
        )
        and normalizar_telefono(
            lead.get("whatsapp")
        )
        and lead.get(
            "whatsapp_confirmado"
        )
    )


def datos_lead_faltantes(
    estado: dict,
) -> List[str]:
    lead = estado["lead"]
    faltantes: List[str] = []

    if not nombre_valido(
        lead.get("nombre")
    ):
        faltantes.append(
            "nombre completo"
        )

    if not correo_valido(
        lead.get("correo")
    ):
        faltantes.append(
            "correo electrónico"
        )

    if not (
        normalizar_telefono(
            lead.get("whatsapp")
        )
        and lead.get(
            "whatsapp_confirmado"
        )
    ):
        faltantes.append(
            "confirmación del número "
            "de WhatsApp"
        )

    return faltantes


# ============================================================
# FILTRADO ESTRICTO Y RANKING
# ============================================================

def obtener_precio(
    propiedad: dict,
    operacion: str,
) -> float:
    if operacion == "alquiler":
        return convertir_float(
            propiedad.get(
                "precio_renta_float"
            )
        )

    if operacion == "venta":
        return convertir_float(
            propiedad.get(
                "precio_venta_float"
            )
        )

    return 0.0


def coincide_operacion(
    propiedad: dict,
    operacion: Optional[str],
) -> bool:
    """
    La operación es estricta.

    Una propiedad de venta no puede aparecer en una búsqueda de
    alquiler si no tiene un precio de alquiler válido, y viceversa.
    """
    if operacion == "alquiler":
        return (
            convertir_float(
                propiedad.get(
                    "precio_renta_float"
                )
            ) > 0
        )

    if operacion == "venta":
        return (
            convertir_float(
                propiedad.get(
                    "precio_venta_float"
                )
            ) > 0
        )

    return False


def coincide_tipo(
    propiedad: dict,
    tipo_buscado: str,
) -> bool:
    """
    Comparación estricta del tipo oficial de Wasi.

    No considera automáticamente:
    - penthouse como apartamento;
    - townhouse como casa;
    - apartoquinta como casa.
    """
    if not tipo_buscado:
        return True

    buscado = normalizar_tipo_propiedad(
        tipo_buscado
    )

    tipo_wasi = normalizar_tipo_propiedad(
        propiedad.get(
            "tipo_propiedad_wasi",
            "",
        )
    )

    if not buscado or not tipo_wasi:
        return False

    return buscado == tipo_wasi


def evaluar_espacio(
    valor_propiedad: Any,
    valor_solicitado: Optional[int],
) -> tuple[bool, bool, int]:
    """
    Retorna:
    - si cumple;
    - si es exacto;
    - valor real de la propiedad.

    Cuando el cliente indica una cantidad, se permite una diferencia
    máxima de uno.
    """
    if (
        valor_solicitado is None
        or valor_solicitado <= 0
    ):
        return (
            True,
            True,
            convertir_entero(
                valor_propiedad
            ),
        )

    if valor_propiedad in [
        None,
        "",
        "N/D",
        "None",
    ]:
        return False, False, 0

    valor_real = convertir_entero(
        valor_propiedad
    )

    diferencia = abs(
        valor_real - valor_solicitado
    )

    if diferencia == 0:
        return True, True, valor_real

    if diferencia <= 1:
        return True, False, valor_real

    return False, False, valor_real


def construir_texto_busqueda(
    propiedad: dict,
) -> str:
    """
    Construye el texto completo utilizado para comprobar palabras
    clave. Incluye título, descripción y características de Wasi.
    """
    return normalizar_texto(
        " ".join([
            str(
                propiedad.get(
                    "titulo",
                    "",
                )
            ),
            str(
                propiedad.get(
                    "descripcion",
                    "",
                )
            ),
            str(
                propiedad.get(
                    "caracteristicas_texto",
                    "",
                )
            ),
        ])
    )


def cumple_caracteristicas(
    propiedad: dict,
    caracteristicas: List[str],
) -> bool:
    """
    Las palabras clave solicitadas son estrictas.

    Si el cliente pide piscina y piso bajo, ambas expresiones deben
    estar presentes en el título, descripción o características Wasi.
    """
    if not caracteristicas:
        return True

    texto_propiedad = construir_texto_busqueda(
        propiedad
    )

    for caracteristica in caracteristicas:
        caracteristica_normalizada = (
            normalizar_texto(
                caracteristica
            )
        )

        if (
            caracteristica_normalizada
            and caracteristica_normalizada
            not in texto_propiedad
        ):
            return False

    return True


def evaluar_propiedad(
    original: dict,
    filtros: dict,
) -> Optional[dict]:
    propiedad = deepcopy(original)

    operacion = filtros.get(
        "tipo_operacion"
    )
    tipo = filtros.get(
        "tipo_propiedad"
    )
    zona = filtros.get(
        "zona"
    )

    presupuesto = convertir_float(
        filtros.get(
            "presupuesto_max"
        )
    )

    habitaciones_solicitadas = filtros.get(
        "habitaciones_min"
    )
    banos_solicitados = filtros.get(
        "banos_min"
    )
    garajes_solicitados = filtros.get(
        "garajes_min"
    )

    caracteristicas = [
        normalizar_texto(
            caracteristica
        )
        for caracteristica in filtros.get(
            "caracteristicas",
            [],
        )
        if normalizar_texto(
            caracteristica
        )
    ]

    # 1. Operación estricta.
    if not coincide_operacion(
        propiedad,
        operacion,
    ):
        return None

    precio = obtener_precio(
        propiedad,
        operacion,
    )

    if precio <= 0:
        return None

    # 2. Tipo estricto.
    if (
        tipo
        and not coincide_tipo(
            propiedad,
            tipo,
        )
    ):
        return None

    # 3. La propiedad debe pertenecer al menos a una de las
    # zonas solicitadas.
    if (
        zona
        and not zona_coincide(
            zona,
            propiedad.get(
                "zona",
                "",
            ),
            propiedad.get(
                "ciudad",
                "",
            ),
        )
    ):
        return None

    # 4. Límite absoluto de presupuesto.
    sobre_presupuesto = False

    if presupuesto > 0:
        limite_tolerancia = (
            presupuesto
            * (
                1
                + MAX_EXCESO_PRESUPUESTO
            )
        )

        if precio > limite_tolerancia:
            return None

        sobre_presupuesto = (
            precio > presupuesto
        )

    # 5. Habitaciones con tolerancia de +/- 1.
    (
        cumple,
        exacto_habitaciones,
        habitaciones,
    ) = evaluar_espacio(
        propiedad.get(
            "habitaciones"
        ),
        habitaciones_solicitadas,
    )

    if not cumple:
        return None

    # 6. Baños con tolerancia de +/- 1.
    (
        cumple,
        exacto_banos,
        banos,
    ) = evaluar_espacio(
        propiedad.get(
            "banos"
        ),
        banos_solicitados,
    )

    if not cumple:
        return None

    # 7. Garajes con tolerancia de +/- 1.
    (
        cumple,
        exacto_garajes,
        garajes,
    ) = evaluar_espacio(
        propiedad.get(
            "garajes"
        ),
        garajes_solicitados,
    )

    if not cumple:
        return None

    # 8. Todas las palabras clave son obligatorias.
    if not cumple_caracteristicas(
        propiedad,
        caracteristicas,
    ):
        return None

    score = 0.0
    diferencias: List[str] = []

    if tipo:
        score += 35

    if zona:
        score += 30

    if presupuesto > 0:
        if sobre_presupuesto:
            score += 5

            diferencias.append(
                "Inversión "
                + formato_moneda(
                    precio
                )
            )
        else:
            proporcion = min(
                precio / presupuesto,
                1,
            )

            score += (
                20 * proporcion
            )

    if habitaciones_solicitadas:
        if exacto_habitaciones:
            score += 10
        else:
            score += 5

            diferencias.append(
                f"tiene {habitaciones} "
                "habitaciones"
            )

    if banos_solicitados:
        if exacto_banos:
            score += 8
        else:
            score += 4

            diferencias.append(
                f"tiene {banos} baños"
            )

    if garajes_solicitados:
        if exacto_garajes:
            score += 8
        else:
            score += 4

            diferencias.append(
                f"tiene {garajes} garajes"
            )

    score += (
        len(caracteristicas) * 5
    )

    es_exacta = (
        not sobre_presupuesto
        and exacto_habitaciones
        and exacto_banos
        and exacto_garajes
    )

    propiedad["_score"] = round(
        score,
        2,
    )

    propiedad["_diferencias"] = (
        diferencias
    )

    propiedad["_sobre_presupuesto"] = (
        sobre_presupuesto
    )

    propiedad["_coincidencia"] = (
        "exacta"
        if es_exacta
        else "aproximada"
    )

    propiedad["operacion_buscada"] = (
        operacion
    )

    return propiedad


def propiedad_cumple_tipo_operacion(
    propiedad: dict,
    filtros: dict,
) -> bool:
    operacion = filtros.get(
        "tipo_operacion"
    )
    tipo = filtros.get(
        "tipo_propiedad"
    )

    return bool(
        coincide_operacion(
            propiedad,
            operacion,
        )
        and (
            not tipo
            or coincide_tipo(
                propiedad,
                tipo,
            )
        )
    )


def diagnosticar_motivo_falla(
    estado: dict,
) -> str:
    """
    Diagnóstico por etapas:

    1. Tipo y operación.
    2. Zona.
    3. Presupuesto con tolerancia.
    4. Espacios y palabras clave.
    """
    filtros = estado.get(
        "filtros",
        {},
    )

    zona = filtros.get(
        "zona"
    )

    presupuesto = convertir_float(
        filtros.get(
            "presupuesto_max"
        )
    )

    tipo_operacion = [
        propiedad
        for propiedad in inventory_cache[
            "inventario"
        ]
        if propiedad_cumple_tipo_operacion(
            propiedad,
            filtros,
        )
    ]

    if not tipo_operacion:
        return "zona_o_tipo"

    propiedades_en_zona = [
        propiedad
        for propiedad in tipo_operacion
        if (
            not zona
            or zona_coincide(
                zona,
                propiedad.get(
                    "zona",
                    "",
                ),
                propiedad.get(
                    "ciudad",
                    "",
                ),
            )
        )
    ]

    if not propiedades_en_zona:
        return "zona_o_tipo"

    if presupuesto > 0:
        limite = (
            presupuesto
            * (
                1
                + MAX_EXCESO_PRESUPUESTO
            )
        )

        propiedades_en_precio = [
            propiedad
            for propiedad in propiedades_en_zona
            if (
                0
                < obtener_precio(
                    propiedad,
                    filtros.get(
                        "tipo_operacion"
                    ),
                )
                <= limite
            )
        ]

        if not propiedades_en_precio:
            return "precio"

    return "espacios_o_caracteristicas"


def clave_orden_propiedad(
    propiedad: dict,
) -> tuple:
    """
    Prioriza coincidencias exactas y después el mayor puntaje.
    """
    return (
        propiedad.get(
            "_coincidencia"
        ) == "exacta",
        propiedad.get(
            "_score",
            0,
        ),
    )


def buscar_mejores_propiedades(
    estado: dict,
    cantidad: int = 3,
) -> tuple[List[dict], str]:
    excluir = {
        str(property_id)
        for property_id in estado.get(
            "propiedades_enviadas",
            [],
        )
    }

    evaluadas: List[dict] = []

    for original in inventory_cache[
        "inventario"
    ]:
        property_id = str(
            original.get(
                "id",
                "",
            )
        )

        if not property_id:
            continue

        if property_id in excluir:
            continue

        propiedad = evaluar_propiedad(
            original,
            estado["filtros"],
        )

        if propiedad:
            evaluadas.append(
                propiedad
            )

    # Primera etapa: propiedades dentro del presupuesto.
    dentro_presupuesto = sorted(
        [
            propiedad
            for propiedad in evaluadas
            if not propiedad.get(
                "_sobre_presupuesto",
                False,
            )
        ],
        key=clave_orden_propiedad,
        reverse=True,
    )

    # Segunda etapa: propiedades por encima del presupuesto,
    # pero sin superar la tolerancia configurada del 20%.
    sobre_presupuesto = sorted(
        [
            propiedad
            for propiedad in evaluadas
            if propiedad.get(
                "_sobre_presupuesto",
                False,
            )
        ],
        key=clave_orden_propiedad,
        reverse=True,
    )

    resultado = dentro_presupuesto[
        :cantidad
    ]

    # La tolerancia solamente se activa si no hay suficientes
    # propiedades dentro del presupuesto.
    if len(resultado) < cantidad:
        faltantes = (
            cantidad - len(resultado)
        )

        resultado.extend(
            sobre_presupuesto[
                :faltantes
            ]
        )

    if resultado:
        return resultado, ""

    # Comprueba si no hay resultados porque todos los elegibles
    # ya fueron enviados anteriormente.
    existen_sin_excluir = any(
        evaluar_propiedad(
            original,
            estado["filtros"],
        )
        is not None
        for original in inventory_cache[
            "inventario"
        ]
    )

    if existen_sin_excluir and excluir:
        return [], "agotadas"

    return (
        [],
        diagnosticar_motivo_falla(
            estado
        ),
    )


def buscar_por_codigo(
    codigo: str,
) -> Optional[dict]:
    codigo_limpio = re.sub(
        r"\D",
        "",
        str(codigo or ""),
    )

    if not codigo_limpio:
        return None

    for propiedad in inventory_cache[
        "inventario"
    ]:
        if (
            str(
                propiedad.get(
                    "id",
                    "",
                )
            )
            == codigo_limpio
        ):
            return deepcopy(
                propiedad
            )

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
        titulo = (
            f"Opción {posicion}: {titulo}"
        )

    area = propiedad.get(
        "area",
        "N/D",
    )

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
            + "; ".join(
                diferencias[:3]
            )
        )

    if es_colega:
        captador_wasi = propiedad.get(
            "captador_wasi",
            "Asesor Mettryc",
        )

        cruce = cruzar_captador_con_sheet(
            captador_wasi
        )

        telefono = cruce.get(
            "telefono"
        )

        lineas.append(
            "👤 *Captador:* "
            + str(
                cruce.get("nombre")
                or captador_wasi
            )
        )

        if telefono:
            lineas.append(
                "📲 *WhatsApp captador:* "
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
        if propiedad.get(
            "_coincidencia"
        ) == "aproximada"
    )

    textos = await redactar_resultado_ia(
        estado,
        cantidad=len(propiedades),
        aproximadas=aproximadas,
        especifica=especifica,
    )

    fichas: List[str] = []

    for indice, propiedad in enumerate(
        propiedades,
        start=1,
    ):
        ficha = await formatear_ficha(
            propiedad,
            es_colega,
            (
                indice
                if not especifica
                else None
            ),
        )

        fichas.append(ficha)

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
    if (
        not TELEGRAM_BOT_TOKEN
        or not chat_id
        or http_client is None
    ):
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

        return bool(
            payload.get("ok")
        )

    except Exception as exc:
        logger.error(
            "Error Telegram chat=%s tipo=%s",
            str(chat_id)[-4:],
            type(exc).__name__,
        )
        return False


def resumen_filtros(
    estado: dict,
) -> str:
    filtros = estado["filtros"]
    lineas: List[str] = []

    etiquetas = {
        "tipo_operacion": "Operación",
        "tipo_propiedad": "Tipo",
        "zona": "Zona",
        "habitaciones_min": "Habitaciones",
        "banos_min": "Baños",
        "garajes_min": "Garajes",
    }

    for campo, etiqueta in etiquetas.items():
        valor = filtros.get(
            campo
        )

        if valor not in [
            None,
            "",
            [],
        ]:
            lineas.append(
                f"- {etiqueta}: {valor}"
            )

    if filtros.get(
        "presupuesto_max"
    ):
        lineas.append(
            "- Presupuesto: "
            + formato_moneda(
                filtros[
                    "presupuesto_max"
                ]
            )
        )

    if filtros.get(
        "caracteristicas"
    ):
        lineas.append(
            "- Características: "
            + ", ".join(
                filtros[
                    "caracteristicas"
                ]
            )
        )

    return (
        "\n".join(lineas)
        or "- Sin filtros específicos"
    )


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

    propiedad_texto = (
        "No especificada"
    )

    if propiedad:
        propiedad_texto = (
            f"{propiedad.get('titulo')}\n"
            f"ID: {propiedad.get('id')}\n"
            f"{propiedad.get('enlace')}"
        )

    whatsapp = normalizar_telefono(
        lead.get("whatsapp")
    )

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

    destinos = set(
        TELEGRAM_ADMIN_IDS
    )

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

    resultados: List[bool] = []

    for destino in destinos:
        resultados.append(
            await enviar_telegram(
                destino,
                mensaje,
            )
        )

    return any(resultados)


# ============================================================
# DETECCIÓN DETERMINISTA DE PRESUPUESTO
# ============================================================

def convertir_monto_texto(
    numero_texto: str,
    multiplicador_mil: bool = False,
) -> float:
    texto = str(
        numero_texto or ""
    ).strip()

    texto = re.sub(
        r"\s+",
        "",
        texto,
    )

    if not texto:
        return 0.0

    if multiplicador_mil:
        # Casos como 1.5k o 1,5 mil.
        if re.fullmatch(
            r"\d+[.,]\d{1,2}",
            texto,
        ):
            texto = texto.replace(
                ",",
                ".",
            )

            return (
                convertir_float(texto)
                * 1000
            )

    if "." in texto and "," in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(
                ".",
                "",
            )
            texto = texto.replace(
                ",",
                ".",
            )
        else:
            texto = texto.replace(
                ",",
                "",
            )

    elif "." in texto:
        parte_final = texto.split(
            "."
        )[-1]

        if len(parte_final) == 3:
            texto = texto.replace(
                ".",
                "",
            )

    elif "," in texto:
        parte_final = texto.split(
            ","
        )[-1]

        if len(parte_final) == 3:
            texto = texto.replace(
                ",",
                "",
            )
        else:
            texto = texto.replace(
                ",",
                ".",
            )

    numero = convertir_float(
        texto
    )

    if multiplicador_mil:
        numero *= 1000

    return numero


def detectar_presupuesto_mensaje(
    mensaje: str,
) -> float:
    texto = str(
        mensaje or ""
    ).lower()

    texto = unicodedata.normalize(
        "NFD",
        texto,
    )

    texto = "".join(
        caracter
        for caracter in texto
        if unicodedata.category(
            caracter
        ) != "Mn"
    )

    numero = (
        r"\d+(?:[.,]\d+)*"
    )

    patrones = [
        # $400, USD 400, US$ 400
        (
            rf"(?:us\s*\$|usd|\$)\s*"
            rf"({numero})"
            rf"\s*(mil|k)?"
        ),

        # 400 dólares, 400 USD, 400$
        (
            rf"({numero})\s*"
            rf"(mil|k)?\s*"
            rf"(?:usd|us\s*\$|\$|dolares?)"
        ),

        # Presupuesto de 400 o hasta 400.
        (
            rf"(?:presupuesto|precio|maximo|hasta|inversion)"
            rf"\s*(?:de|es|aproximado|aproximadamente|:)?"
            rf"\s*(?:us\s*\$|usd|\$)?\s*"
            rf"({numero})\s*(mil|k)?"
        ),
    ]

    for patron in patrones:
        coincidencia = re.search(
            patron,
            texto,
            re.IGNORECASE,
        )

        if not coincidencia:
            continue

        numero_texto = coincidencia.group(
            1
        )

        grupos = [
            str(grupo or "").lower()
            for grupo in coincidencia.groups()[
                1:
            ]
        ]

        multiplicador_mil = any(
            grupo in {
                "mil",
                "k",
            }
            for grupo in grupos
        )

        monto = convertir_monto_texto(
            numero_texto,
            multiplicador_mil,
        )

        if monto > 0:
            return monto

    return 0.0


# ============================================================
# DETECCIÓN DETERMINISTA DE TIPO Y OPERACIÓN
# ============================================================

def detectar_tipo_propiedad_mensaje(
    mensaje: str,
) -> Optional[str]:
    texto = normalizar_texto(
        mensaje
    )

    equivalencias = [
        (
            [
                "apartoquinta",
                "aparto quinta",
                "aptoquinta",
            ],
            "apartoquinta",
        ),
        (
            [
                "townhouse",
                "town house",
            ],
            "townhouse",
        ),
        (
            [
                "penthouse",
            ],
            "penthouse",
        ),
        (
            [
                "apartamento",
                "apartamentos",
                "apto",
                "aptos",
            ],
            "apartamento",
        ),
        (
            [
                "oficina",
                "oficinas",
            ],
            "oficina",
        ),
        (
            [
                "galpon",
                "galpones",
            ],
            "galpon",
        ),
        (
            [
                "terreno",
                "terrenos",
            ],
            "terreno",
        ),
        (
            [
                "local",
                "locales",
            ],
            "local",
        ),
        (
            [
                "quinta",
                "quintas",
            ],
            "quinta",
        ),
        (
            [
                "casa",
                "casas",
            ],
            "casa",
        ),
    ]

    for expresiones, resultado in equivalencias:
        for expresion in expresiones:
            if re.search(
                rf"\b{re.escape(expresion)}\b",
                texto,
            ):
                return resultado

    return None


def detectar_operacion_mensaje(
    mensaje: str,
) -> Optional[str]:
    texto = normalizar_texto(
        mensaje
    )

    palabras_venta = [
        "venta",
        "comprar",
        "compra",
        "adquirir",
        "inversion",
    ]

    palabras_alquiler = [
        "alquiler",
        "alquilar",
        "arrendar",
        "arrendamiento",
        "canon",
        "renta",
    ]

    if any(
        palabra in texto
        for palabra in palabras_venta
    ):
        return "venta"

    if any(
        palabra in texto
        for palabra in palabras_alquiler
    ):
        return "alquiler"

    return None


def detectar_zonas_geografia(
    mensaje: str,
) -> List[str]:
    """
    Busca zonas en el diccionario geográfico.

    Si detecta zonas concretas, no agrega también la ciudad, porque
    hacerlo convertiría una búsqueda de Prebo en una búsqueda de toda
    Valencia.
    """
    texto = normalizar_texto(
        mensaje
    )

    zonas_encontradas: List[str] = []
    ciudades_encontradas: List[str] = []

    try:
        from geografia import (
            DICCIONARIO_GEOGRAFICO,
        )
    except ImportError:
        return []

    for _, ciudades in (
        DICCIONARIO_GEOGRAFICO.items()
    ):
        if not isinstance(
            ciudades,
            dict,
        ):
            continue

        for ciudad, zonas in ciudades.items():
            ciudad_normalizada = (
                normalizar_texto(
                    ciudad
                )
            )

            if (
                ciudad_normalizada
                and re.search(
                    rf"\b{re.escape(ciudad_normalizada)}\b",
                    texto,
                )
            ):
                ciudades_encontradas.append(
                    str(ciudad)
                )

            if not isinstance(
                zonas,
                (
                    list,
                    tuple,
                    set,
                ),
            ):
                continue

            for zona in zonas:
                zona_normalizada = (
                    normalizar_texto(
                        zona
                    )
                )

                if (
                    len(zona_normalizada) >= 3
                    and re.search(
                        rf"\b{re.escape(zona_normalizada)}\b",
                        texto,
                    )
                ):
                    zonas_encontradas.append(
                        str(zona)
                    )

    resultado = (
        zonas_encontradas
        if zonas_encontradas
        else ciudades_encontradas
    )

    return list(
        dict.fromkeys(resultado)
    )


def mensaje_niega_caracteristica(
    mensaje: str,
    caracteristica: str,
) -> bool:
    texto = normalizar_texto(
        mensaje
    )

    caracteristica = normalizar_texto(
        caracteristica
    )

    patrones = [
        rf"\bsin\s+{re.escape(caracteristica)}\b",
        rf"\bno\s+necesito\s+{re.escape(caracteristica)}\b",
        rf"\bno\s+quiero\s+{re.escape(caracteristica)}\b",
        rf"\bno\s+importa\s+{re.escape(caracteristica)}\b",
    ]

    return any(
        re.search(
            patron,
            texto,
        )
        for patron in patrones
    )


def aplicar_cazadores_deterministas(
    estado: dict,
    mensaje: str,
) -> bool:
    filtros = estado.setdefault(
        "filtros",
        {},
    )

    hubo_cambio = False

    # Presupuesto.
    presupuesto = detectar_presupuesto_mensaje(
        mensaje
    )

    if (
        presupuesto > 0
        and filtros.get(
            "presupuesto_max"
        ) != presupuesto
    ):
        filtros["presupuesto_max"] = (
            presupuesto
        )
        hubo_cambio = True

    # Operación explícita.
    operacion = detectar_operacion_mensaje(
        mensaje
    )

    if (
        operacion
        and filtros.get(
            "tipo_operacion"
        ) != operacion
    ):
        filtros["tipo_operacion"] = (
            operacion
        )
        hubo_cambio = True

    # Inferencia financiera únicamente cuando no se informó
    # explícitamente la operación.
    if (
        not filtros.get(
            "tipo_operacion"
        )
        and presupuesto > 0
    ):
        filtros["tipo_operacion"] = (
            "venta"
            if presupuesto > 4000
            else "alquiler"
        )
        hubo_cambio = True

    # Tipo.
    tipo = detectar_tipo_propiedad_mensaje(
        mensaje
    )

    if (
        tipo
        and filtros.get(
            "tipo_propiedad"
        ) != tipo
    ):
        filtros["tipo_propiedad"] = (
            tipo
        )
        hubo_cambio = True

    # Zonas múltiples.
    zonas = detectar_zonas_geografia(
        mensaje
    )

    if zonas:
        zona_nueva = ", ".join(
            zonas
        )

        if filtros.get("zona") != zona_nueva:
            filtros["zona"] = zona_nueva
            hubo_cambio = True

    # Espacios.
    espacios = detectar_espacios_mensaje(
        mensaje
    )

    for campo, valor in espacios.items():
        if filtros.get(campo) != valor:
            filtros[campo] = valor
            hubo_cambio = True

    # Características obligatorias.
    detectadas = detectar_caracteristicas_mensaje(
        mensaje
    )

    actuales = [
        normalizar_texto(
            caracteristica
        )
        for caracteristica in filtros.get(
            "caracteristicas",
            [],
        )
        if normalizar_texto(
            caracteristica
        )
    ]

    # Permite retirar una característica con frases como
    # "sin piscina" o "no necesito piscina".
    caracteristicas_negadas = {
        normalizar_texto(
            caracteristica
        )
        for caracteristica in (
            CARACTERISTICAS_RECONOCIDAS
        )
        if mensaje_niega_caracteristica(
            mensaje,
            caracteristica,
        )
    }

    if caracteristicas_negadas:
        nuevas_actuales = [
            caracteristica
            for caracteristica in actuales
            if caracteristica
            not in caracteristicas_negadas
        ]

        if nuevas_actuales != actuales:
            actuales = nuevas_actuales
            hubo_cambio = True

    positivas = [
        caracteristica
        for caracteristica in detectadas
        if (
            caracteristica
            not in caracteristicas_negadas
        )
    ]

    nuevas_caracteristicas = list(
        dict.fromkeys([
            *actuales,
            *positivas,
        ])
    )

    if (
        nuevas_caracteristicas
        != filtros.get(
            "caracteristicas",
            [],
        )
    ):
        filtros["caracteristicas"] = (
            nuevas_caracteristicas
        )
        hubo_cambio = True

    return hubo_cambio


# ============================================================
# MOTOR CONVERSACIONAL
# ============================================================

async def mostrar_propiedades(
    estado: dict,
) -> str:
    (
        propiedades,
        motivo_falla,
    ) = buscar_mejores_propiedades(
        estado,
        MAX_PROPIEDADES_POR_LOTE,
    )

    if not propiedades:
        estado["ultimo_lote"] = []

        filtros = estado.get(
            "filtros",
            {},
        )

        tipo = pluralizar_tipo_propiedad(
            filtros.get(
                "tipo_propiedad"
            )
        )

        zona = formatear_lista_zonas(
            filtros.get("zona")
        )

        presupuesto = filtros.get(
            "presupuesto_max"
        )

        if motivo_falla == "zona_o_tipo":
            return (
                f"No disponemos de {tipo} en {zona} "
                "en este momento ¿te puedo ofrecer en otra zona?"
            )

        if motivo_falla == "precio":
            return (
                f"No tenemos {tipo} en {zona} por "
                f"{formato_moneda(presupuesto)}. "
                "¿Busco en otro precio o en otra zona?"
            )

        if motivo_falla == "agotadas":
            return (
                "Ya te mostré todas las opciones disponibles "
                "con esos criterios. ¿Quieres cambiar la zona, "
                "el precio o alguna característica?"
            )

        return (
            f"Sí tenemos {tipo} en {zona}, pero ninguno cumple "
            "simultáneamente con los espacios o características "
            "solicitadas. ¿Quieres flexibilizar algún requisito?"
        )

    ids = [
        str(propiedad["id"])
        for propiedad in propiedades
    ]

    estado[
        "propiedades_enviadas"
    ].extend(ids)

    estado["ultimo_lote"] = ids
    estado["objetivo"] = (
        "evaluar_resultados"
    )

    return await construir_respuesta_fichas(
        estado,
        propiedades,
    )


async def mostrar_inmueble_especifico(
    estado: dict,
    codigo: str,
) -> str:
    propiedad = buscar_por_codigo(
        codigo
    )

    if not propiedad:
        estado["esperando_codigo"] = True

        return (
            "No encontré un inmueble activo con el código "
            f"{codigo}. Revisa si está escrito correctamente o "
            "envíame el enlace de la publicación."
        )

    operacion = estado[
        "filtros"
    ].get(
        "tipo_operacion"
    )

    if not coincide_operacion(
        propiedad,
        operacion,
    ):
        if convertir_float(
            propiedad.get(
                "precio_venta_float"
            )
        ) > 0:
            operacion = "venta"
        elif convertir_float(
            propiedad.get(
                "precio_renta_float"
            )
        ) > 0:
            operacion = "alquiler"
        else:
            operacion = "venta"

    propiedad[
        "operacion_buscada"
    ] = operacion

    property_id = str(
        propiedad["id"]
    )

    estado["ultimo_lote"] = [
        property_id
    ]

    if property_id not in estado[
        "propiedades_enviadas"
    ]:
        estado[
            "propiedades_enviadas"
        ].append(property_id)

    estado["esperando_codigo"] = False
    estado["objetivo"] = (
        "evaluar_resultados"
    )

    return await construir_respuesta_fichas(
        estado,
        [propiedad],
        especifica=True,
    )


async def seleccionar_propiedad(
    estado: dict,
    posicion: Optional[int],
) -> str:
    lote = estado.get(
        "ultimo_lote",
        [],
    )

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

    if (
        indice < 0
        or indice >= len(lote)
    ):
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

    estado["propiedad_interes"] = (
        propiedad
    )

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
            return (
                "Perfecto, colega. El captador de esa propiedad "
                f"es {cruce['nombre']}. Puedes comunicarte por "
                "WhatsApp aquí: "
                f"https://wa.me/{cruce['telefono']}. "
                "Si quieres, también puedo revisar otras opciones."
            )

        return (
            "Perfecto, colega. Identifiqué la propiedad, pero el "
            "captador no aparece actualmente en el directorio de "
            "Google Sheets. Puedes solicitar apoyo a la oficina o "
            "pedirme otras opciones."
        )

    estado["objetivo"] = (
        "captura_lead"
    )

    faltantes = datos_lead_faltantes(
        estado
    )

    if not faltantes:
        return (
            "¡Excelente elección! Ya tengo tus datos y voy a "
            "asignarte un asesor."
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
        estado["lead_id"] = str(
            uuid.uuid4()
        )

    if not estado.get(
        "agente_asignado"
    ):
        estado["agente_asignado"] = (
            await asignar_agente_round_robin()
        )

    if not estado.get(
        "notificacion_enviada"
    ):
        estado["notificacion_enviada"] = (
            await notificar_lead(
                estado
            )
        )

    estado["objetivo"] = (
        "lead_asignado"
    )

    agente = estado.get(
        "agente_asignado"
    )

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
    codigo = extraer_codigo_inmueble(
        mensaje
    )

    if codigo:
        decision.accion = AccionAgente(
            tipo="buscar_por_codigo",
            codigo=codigo,
        )
        return decision

    if (
        estado.get(
            "esperando_codigo"
        )
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

    if pide_mas_opciones(
        mensaje
    ):
        decision.accion = AccionAgente(
            tipo=(
                "mostrar_mas_propiedades"
            )
        )
        return decision

    posicion = detectar_posicion(
        mensaje
    )

    if (
        posicion
        and estado.get(
            "ultimo_lote"
        )
    ):
        decision.accion = AccionAgente(
            tipo="seleccionar_propiedad",
            posicion=posicion,
        )
        return decision

    if menciona_anuncio_sin_codigo(
        mensaje
    ):
        decision.accion = AccionAgente(
            tipo="pedir_codigo_inmueble"
        )

    return decision


async def procesar_mensaje(
    sender: str,
    mensaje: str,
) -> str:
    estado = obtener_sesion(
        sender
    )

    estado = verificar_caducidad_y_amnesia(
        estado
    )

    decision = await decidir_con_ia(
        mensaje,
        estado,
    )

    logger.info(
        "Decisión IA accion=%s actualizaciones=%s",
        decision.accion.tipo,
        decision.actualizaciones.model_dump(),
    )

    decision = forzar_accion_evidente(
        decision,
        mensaje,
        estado,
    )

    hubo_cambio_ia = aplicar_decision(
        estado,
        decision,
        mensaje,
    )

    hubo_cambio_python = (
        aplicar_cazadores_deterministas(
            estado,
            mensaje,
        )
    )

    hubo_cambio = (
        hubo_cambio_ia
        or hubo_cambio_python
    )

    # aplicar_decision ya reinicia el lote cuando la IA cambia
    # filtros. Este bloque cubre los cambios detectados directamente
    # por Python.
    if (
        hubo_cambio_python
        and estado.get(
            "propiedades_enviadas"
        )
    ):
        estado[
            "propiedades_enviadas"
        ] = []
        estado["ultimo_lote"] = []
        estado["propiedad_interes"] = None

    logger.info(
        "Filtros finales=%s rol=%s criterios_suficientes=%s",
        estado.get("filtros"),
        estado.get("rol"),
        criterios_suficientes(
            estado
        ),
    )

    if not estado.get("rol"):
        estado["rol"] = "cliente"
        estado["confianza_rol"] = 0.40

    accion = decision.accion.tipo

    if accion == "reiniciar_busqueda":
        estado = reiniciar_busqueda(
            estado
        )

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
            or extraer_codigo_inmueble(
                mensaje
            )
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
        if not estado[
            "propiedades_enviadas"
        ]:
            if criterios_suficientes(
                estado
            ):
                respuesta = (
                    await mostrar_propiedades(
                        estado
                    )
                )
            else:
                respuesta = (
                    "Todavía no tengo criterios suficientes. "
                    "Dime el tipo de inmueble, la operación y "
                    "la zona o presupuesto."
                )
        else:
            respuesta = (
                await mostrar_propiedades(
                    estado
                )
            )

    elif accion == "seleccionar_propiedad":
        respuesta = await seleccionar_propiedad(
            estado,
            decision.accion.posicion,
        )

    elif accion == "buscar_propiedades":
        if criterios_suficientes(
            estado
        ):
            respuesta = (
                await mostrar_propiedades(
                    estado
                )
            )
        else:
            pregunta = obtener_pregunta_faltante(
                estado
            )

            respuesta = (
                decision.mensaje.strip()
                or pregunta
            )

    else:
        if (
            hubo_cambio
            and criterios_suficientes(
                estado
            )
        ):
            respuesta = (
                await mostrar_propiedades(
                    estado
                )
            )
        else:
            pregunta_dinamica = (
                obtener_pregunta_faltante(
                    estado
                )
            )

            texto_crudo = (
                decision.mensaje.strip()
                or pregunta_dinamica
            )

            respuesta = (
                await humanizar_texto_con_ia(
                    estado=estado,
                    instruccion_cruda=texto_crudo,
                    mensaje_usuario=mensaje,
                )
            )

    if (
        estado.get("objetivo")
        == "captura_lead"
    ):
        if lead_completo(
            estado
        ):
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

    guardar_sesion(
        sender,
        estado,
    )

    return respuesta


# ============================================================
# INICIALIZACIÓN
# ============================================================

async def inicializar_datos() -> None:
    resultados = await asyncio.gather(
        actualizar_inventario(
            force=True
        ),
        sincronizar_google_sheet(
            force=True
        ),
        return_exceptions=True,
    )

    for resultado in resultados:
        if isinstance(
            resultado,
            Exception,
        ):
            logger.error(
                "Error inicialización tipo=%s detalle=%s",
                type(resultado).__name__,
                str(resultado)[:200],
            )


@asynccontextmanager
async def lifespan(
    app: FastAPI,
):
    global http_client

    http_client = httpx.AsyncClient(
        follow_redirects=True,
        trust_env=False,
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
        ),
        headers={
            "User-Agent": (
                "Mettryc-Chatbot/2.1"
            ),
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

    if http_client is not None:
        await http_client.aclose()


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Mettryc Realty Paty",
    version="2.1.0",
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

    if (
        not api_key
        or api_key not in API_KEYS_AGENTES
    ):
        raise HTTPException(
            status_code=403,
            detail="Acceso denegado.",
        )


@app.api_route(
    "/",
    methods=[
        "GET",
        "HEAD",
    ],
)
async def root():
    return {
        "service": "Mettryc Realty Paty",
        "version": "2.1.0",
        "status": "online",
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "inventario": len(
            inventory_cache[
                "inventario"
            ]
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
            sheets_cache[
                "agentes"
            ]
        ),
        "captadores": len(
            sheets_cache[
                "captadores"
            ]
        ),
        "sesiones_memoria": len(
            sesiones
        ),
        "persistencia": "memoria",
        "tolerancia_presupuesto": (
            MAX_EXCESO_PRESUPUESTO
        ),
    }


@app.post("/admin/refresh")
async def refresh(
    x_api_key: Optional[str] = Header(
        default=None,
        alias="x-api-key",
    ),
):
    validar_api_key(
        x_api_key
    )

    await asyncio.gather(
        actualizar_inventario(
            force=True
        ),
        sincronizar_google_sheet(
            force=True
        ),
    )

    return {
        "ok": True,
        "propiedades": len(
            inventory_cache[
                "inventario"
            ]
        ),
        "agentes": len(
            sheets_cache[
                "agentes"
            ]
        ),
        "captadores": len(
            sheets_cache[
                "captadores"
            ]
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
    validar_api_key(
        x_api_key
    )

    sesiones.pop(
        sender,
        None,
    )

    locks_usuarios.pop(
        sender,
        None,
    )

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
    validar_api_key(
        x_api_key
    )

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="JSON inválido.",
        )

    payload = (
        data.get("query")
        if isinstance(
            data.get("query"),
            dict,
        )
        else data
    )

    if not isinstance(
        payload,
        dict,
    ):
        raise HTTPException(
            status_code=400,
            detail="Payload inválido.",
        )

    sender = str(
        payload.get(
            "sender",
            "",
        )
    ).strip()

    mensaje = str(
        payload.get(
            "message",
            "",
        )
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
        return {
            "replies": [],
        }

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
        return {
            "replies": [],
        }

    if sender not in locks_usuarios:
        locks_usuarios[sender] = (
            asyncio.Lock()
        )

    if not inventory_cache[
        "inventario"
    ]:
        await actualizar_inventario(
            force=True
        )

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
