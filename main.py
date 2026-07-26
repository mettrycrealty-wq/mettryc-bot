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
from typing import (
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
)

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

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ============================================================
# CONFIGURACIÓN
# ============================================================

DEBUG_MODE = os.getenv("DEBUG_MODE", "true").lower() == "true"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
WASI_TOKEN = os.getenv("WASI_TOKEN", "")
WASI_COMPANY_ID = os.getenv("WASI_COMPANY_ID", "")
GOOGLE_SHEET_TURNOS_URL = os.getenv("GOOGLE_SHEET_TURNOS_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

MODELO_AGENTE_PRINCIPAL = os.getenv(
    "MODELO_AGENTE_PRINCIPAL",
    os.getenv(
        "MODELO_ANALISIS_PRINCIPAL",
        "google/gemini-2.5-flash-lite",
    ),
)

MODELO_AGENTE_RESPALDO = os.getenv(
    "MODELO_AGENTE_RESPALDO",
    os.getenv(
        "MODELO_ANALISIS_RESPALDO",
        "openai/gpt-4o-mini",
    ),
)

MODELO_HUMANIZACION = os.getenv(
    "MODELO_HUMANIZACION",
    "google/gemini-2.5-flash-lite",
)

OPENROUTER_TIMEOUT = float(os.getenv("OPENROUTER_TIMEOUT", "30"))
WASI_TIMEOUT = float(os.getenv("WASI_TIMEOUT", "40"))
SHEETS_TIMEOUT = float(os.getenv("SHEETS_TIMEOUT", "20"))
TELEGRAM_TIMEOUT = float(os.getenv("TELEGRAM_TIMEOUT", "15"))

MAX_PROPIEDADES_CLIENTE = int(
    os.getenv("MAX_PROPIEDADES_CLIENTE", "3")
)

MAX_PROPIEDADES_COLEGA = int(
    os.getenv("MAX_PROPIEDADES_COLEGA", "5")
)

MAX_EXCESO_PRESUPUESTO = float(
    os.getenv("MAX_EXCESO_PRESUPUESTO", "0.20")
)

MAX_EXCESO_COMPLEMENTARIO = float(
    os.getenv("MAX_EXCESO_COMPLEMENTARIO", "0.40")
)

MAX_HISTORIAL = int(os.getenv("MAX_HISTORIAL", "16"))

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

CHAT_ADMIN_SENDERS = {
    valor.strip()
    for valor in os.getenv("CHAT_ADMIN_SENDERS", "").split(",")
    if valor.strip()
}

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
# BASE DE CONOCIMIENTO METTRYC
# ============================================================

BASE_CONOCIMIENTO_METTRYC: Dict[str, Any] = {
    "perfil_empresa": {
        "nombre": "Mettryc Realty",
        "slogan": "Primera Tecnoinmobiliaria de Venezuela",
        "descripcion": (
            "Somos una inmobiliaria venezolana que combina asesoría "
            "humana con herramientas tecnológicas para compra, venta, "
            "alquiler y administración de inmuebles residenciales y "
            "comerciales."
        ),
        "valores": [
            "Transparencia en cada gestión",
            "Atención personalizada y cercana",
            "Uso de tecnología para acelerar resultados",
            "Trabajo colaborativo con colegas inmobiliarios",
        ],
    },
    "contacto": {
        "sitio_web": "https://www.mettryc.com",
        "correo_general": "mettryc.realty@gmail.com",
        "redes_sociales": "@mettryc",
    },
    "oficinas": [
        {
            "ciudad": "Valencia",
            "direccion": (
                "Centro Comercial Patio Trigal, local 300-6"
            ),
            "ubicacion_maps": (
                "https://maps.app.goo.gl/dSofuNmF89vNLv7X8"
            ),
        },
        {
            "ciudad": "San Diego",
            "direccion": "Centro Comercial Metroplaza",
            "ubicacion_maps": (
                "https://maps.app.goo.gl/sKA8ngRFNK9xpmtU8"
            ),
        },
        {
            "ciudad": "Barquisimeto",
            "direccion": (
                "Av. Los Leones, Torre Bel, Piso 4, Ofic. 4-6"
            ),
            "ubicacion_maps": (
                "https://maps.app.goo.gl/1w6Ru7XSHuhfgjA36"
            ),
        },
    ],
    "informacion_servicios": {
        "venta": (
            "Acompañamos todo el ciclo de venta: valoración, promoción, "
            "visitas, negociación y cierre con el propietario."
        ),
        "alquiler": (
            "Gestionamos promoción, captación de inquilinos, visitas "
            "y formalización del contrato."
        ),
        "asesoria_clientes": (
            "Escuchamos necesidades, proponemos opciones del inventario "
            "y coordinamos visitas con nuestros agentes."
        ),
        "colaboracion_colegas": (
            "Compartimos captaciones con colegas bajo acuerdos "
            "transparentes y respetando la información de los propietarios."
        ),
        "marketing_digital": (
            "Usamos portales inmobiliarios, redes sociales y herramientas "
            "de analítica para posicionar inmuebles."
        ),
    },
    "honorarios": {
        "venta": (
            "5% del precio de venta, alineado con la Cámara "
            "Inmobiliaria de Venezuela."
        ),
        "alquiler": (
            "Un mes de canon como comisión, según lineamientos "
            "de la Cámara Inmobiliaria de Venezuela."
        ),
        "nota_negociabilidad": (
            "Sería cuestión de que usted plantee una oferta y nosotros "
            "con gusto se la haremos saber al propietario para que "
            "la evalúe."
        ),
    },
    "politicas": {
        "confidencialidad_propietarios": (
            "No se suministra información directa de propietarios "
            "ni se facilitan contactos privados."
        ),
        "colegas": (
            "Cuando atiendas a colegas o asesores, evita pedir datos "
            "personales del cliente; comparte información útil dentro "
            "de lo permitido."
        ),
        "manejo_multimedia": (
            "Ante audios o imágenes responde que aún no puedes "
            "procesarlos y solicita información escrita."
        ),
        "seguimiento_formulario_capacitacion": (
            "El equipo de reclutamiento revisa las respuestas del "
            "formulario y contacta a quienes cumplen el perfil; "
            "confirma que consultarás su estatus."
        ),
    },
    "captacion_propietarios": {
        "requisitos_iniciales": [
            "Ubicación exacta o zona del inmueble",
            "Precio deseado o rango de oferta",
        ],
        "proceso": (
            "Luego de obtener datos básicos se colectan datos de "
            "contacto para asignar asesor especializado."
        ),
    },
    "reclutamiento": {
        "formulario": "https://forms.gle/kJu3ogn32WWNxByE7",
        "inversion_inicial": (
            "USD 50 que incluye curso introductorio, material "
            "de apoyo y credenciales."
        ),
        "mensaje_estatus": (
            "El departamento de reclutamiento revisa cada solicitud "
            "y contacta a quienes cumplen con el perfil buscado."
        ),
    },
    "mensajes_prefabricados": {
        "no_archivos": (
            "Como soy un Bot aún no he aprendido a escuchar audios "
            "ni ver imágenes, pero si me escribes podré ayudarte "
            "más rápido. 😊"
        ),
        "negociabilidad": (
            "Sería cuestión de que usted plantee una oferta y nosotros "
            "con gusto se la haremos saber al propietario para que "
            "la evalúe."
        ),
        "honorarios": (
            "El honorario es el 5% del precio en ventas o un mes "
            "de comisión en alquiler, tal como lo establece la "
            "Cámara Inmobiliaria de Venezuela."
        ),
    },
    "preguntas_frecuentes": [
        {
            "pregunta": "¿Qué hace Mettryc Realty?",
            "respuesta": (
                "Acompañamos compra, venta y alquiler de inmuebles "
                "con asesoría profesional y tecnología para agilizar "
                "cada proceso."
            ),
        },
        {
            "pregunta": "¿Dónde están ubicados?",
            "respuesta": (
                "Tenemos oficinas en Valencia, en el CC Patio Trigal; "
                "San Diego, en el CC Metroplaza; y Barquisimeto, en "
                "la Av. Los Leones, Torre Bel, Piso 4, oficina 4-6."
            ),
        },
        {
            "pregunta": "¿Cómo contacto a Mettryc?",
            "respuesta": (
                "Puedes escribir al correo mettryc.realty@gmail.com, "
                "visitar https://www.mettryc.com o seguirnos en redes "
                "como @mettryc."
            ),
        },
        {
            "pregunta": "¿Cuáles son sus honorarios?",
            "respuesta": (
                "El honorario es 5% en ventas y un mes en alquiler, "
                "siguiendo la Cámara Inmobiliaria de Venezuela."
            ),
        },
        {
            "pregunta": "¿Puedo negociar el precio de una propiedad?",
            "respuesta": (
                "Sería cuestión de plantear una oferta; con gusto se "
                "la presentaremos al propietario para que la evalúe."
            ),
        },
        {
            "pregunta": "Quiero vender o alquilar mi inmueble",
            "respuesta": (
                "Cuéntame la ubicación y el precio deseado para "
                "asignarte un asesor y seguir con la valoración."
            ),
        },
        {
            "pregunta": "Quiero ser asesor inmobiliario",
            "respuesta": (
                "Completa el formulario "
                "https://forms.gle/kJu3ogn32WWNxByE7. "
                "La inversión es USD 50 e incluye curso, material "
                "de apoyo y credenciales."
            ),
        },
        {
            "pregunta": (
                "Preguntan por estatus del formulario de ingreso"
            ),
            "respuesta": (
                "El equipo de reclutamiento revisa cada solicitud "
                "y contacta a quienes encajan. El equipo podrá "
                "consultar el estatus de tu solicitud."
            ),
        },
    ],
    "mensajes_tono": {
        "saludo_inicial": (
            "¡Hola! Soy Paty de Mettryc Realty, "
            "¿cómo puedo ayudarte hoy?"
        ),
        "despedida_corta": (
            "¡Con gusto! Si necesitas algo más, aquí estoy."
        ),
    },
}


# ============================================================
# MODELOS ESTRUCTURADOS
# ============================================================

class ActualizacionesConversacion(BaseModel):
    tipo_operacion: Optional[
        Literal["venta", "alquiler"]
    ] = None

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

    propietario_operacion: Optional[
        Literal["venta", "alquiler", "valoracion"]
    ] = None

    propietario_ubicacion: Optional[str] = None
    propietario_precio_deseado: Optional[float] = None


class AccionAgente(BaseModel):
    tipo: Literal[
        "responder",
        "mostrar_menu_principal",
        "buscar_propiedades",
        "mostrar_mas_propiedades",
        "buscar_por_codigo",
        "seleccionar_propiedad",
        "pedir_codigo_inmueble",
        "agendar_visita",
        "preguntar_sobre_propiedad",
        "solicitar_humano",
        "publicar_propiedad",
        "consulta_mettryc",
        "reiniciar_busqueda",
        "pedir_aclaracion",
    ] = "responder"

    codigo: Optional[str] = None
    posicion: Optional[int] = None
    categoria_pregunta: Optional[str] = None


class DecisionAgente(BaseModel):
    mensaje: str = ""

    rol: Optional[
        Literal["cliente", "colega_inmobiliario"]
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


class TextoIntroduccion(BaseModel):
    introduccion: str


class RespuestaConsultaPropiedad(BaseModel):
    respuesta: str
    informacion_encontrada: bool = True
    requiere_asesor: bool = False


class RespuestaBaseConocimiento(BaseModel):
    respuesta: str
    informacion_encontrada: bool = True
    sugerir_asesor: bool = False


class OpcionConversacional(BaseModel):
    id: str
    numero: int
    etiqueta: str
    aliases: List[str] = Field(default_factory=list)


class PreguntaConversacional(BaseModel):
    id: str
    texto: str
    opciones: List[OpcionConversacional]
    permite_texto_libre: bool = False


class ResultadoOpcion(BaseModel):
    opcion_id: Optional[str] = None
    entendida: bool = False


class DatosLeadExtraidos(BaseModel):
    nombre: Optional[str] = None
    correo: Optional[str] = None
    whatsapp: Optional[str] = None
    usar_numero_actual: bool = False


class EventoEntrada(BaseModel):
    tipo: Literal[
        "comando",
        "multimedia",
        "solicitud_humano",
        "mercadolibre",
        "otro_anuncio",
        "colega",
        "saludo",
        "busqueda_propiedad",
        "consulta_mettryc",
        "desconocido",
    ]

    codigo: Optional[str] = None


ModeloPydantic = TypeVar(
    "ModeloPydantic",
    bound=BaseModel,
)


# ============================================================
# ESTADO EN MEMORIA
# ============================================================

sesiones: Dict[str, Dict[str, Any]] = {}
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

catalogo_geografico: Dict[str, Any] = {
    "ciudades_norm": {},
    "zonas_norm": {},
    "zonas_por_ciudad": {},
    "frases_ciudades": [],
    "frases_zonas": [],
    "ultima_actualizacion": None,
}

inventory_refresh_lock = asyncio.Lock()
sheets_refresh_lock = asyncio.Lock()
round_robin_lock = asyncio.Lock()

round_robin_index = -1

http_client: Optional[httpx.AsyncClient] = None


# ============================================================
# CONSTANTES DE DETECCIÓN
# ============================================================

MERCADOLIBRE_URL_RE = re.compile(
    r"https?://[^\s]+?-(\d+)-_JM\b",
    re.IGNORECASE,
)

URL_METTRYC_RE = re.compile(
    r"https?://(?:www\.)?mettryc\.com/inmueble/(\d+)",
    re.IGNORECASE,
)

PALABRAS_CONSULTA_DIRECTA = {
    "precio",
    "informacion",
    "información",
    "info",
    "disponibilidad",
    "disponible",
    "sigue disponible",
    "sigue estando disponible",
    "quiero conocer",
    "quiero saber",
    "me interesa",
}

FRASES_OTROS_ANUNCIOS = {
    "vi una propiedad",
    "vi una casa",
    "vi un apartamento",
    "vi un inmueble",
    "vi un anuncio",
    "vi una publicacion",
    "vi una publicación",
    "quiero informacion de una propiedad",
    "quiero información de una propiedad",
    "quiero saber el precio",
    "cual es el precio",
    "cuál es el precio",
    "sigue disponible",
    "esta disponible",
    "está disponible",
    "informacion del anuncio",
    "información del anuncio",
    "precio del anuncio",
    "me interesa el anuncio",
}

FRASES_SALUDO = {
    "hola",
    "buenas",
    "buenos dias",
    "buen día",
    "buen dia",
    "buenas tardes",
    "buenas noches",
    "saludos",
    "hey",
    "holi",
    "hello",
}

PATRONES_SOLICITUD_HUMANO = [
    r"\bhablar\s+con\s+(un|una)\s+"
    r"(asesor|asesora|agente|persona|humano)\b",

    r"\bquiero\s+(un|una)\s+"
    r"(asesor|asesora|agente)\b",

    r"\bnecesito\s+hablar\s+con\s+alguien\b",
    r"\bquiero\s+hablar\s+con\s+alguien\b",

    r"\bcomunicarme\s+con\s+(un|una)\s+"
    r"(asesor|asesora|agente)\b",

    r"\batencion\s+humana\b",
    r"\bpersona\s+real\b",
    r"\bhablar\s+con\s+un\s+humano\b",
    r"\bque\s+me\s+llamen\b",
    r"\bpueden\s+contactarme\b",
    r"\bquiero\s+que\s+me\s+contacten\b",
]

PATRONES_COLEGA = [
    r"\bsoy\s+(asesor|asesora|agente|corredor|corredora|broker|realtor)\b",
    r"\bsoy\s+colega\b",
    r"\btengo\s+un\s+cliente\b",
    r"\bbusco\s+para\s+un\s+cliente\b",
    r"\btrabajo\s+en\s+una\s+inmobiliaria\b",
    r"\bcomparto\s+comision\b",
    r"\bpara\s+mi\s+cliente\b",
]

PATRONES_CLIENTE_EXPLICITO = [
    r"\bno\s+soy\s+(asesor|agente|corredor|broker|realtor)\b",
    r"\bsoy\s+cliente\b",
    r"\bes\s+para\s+mi\b",
    r"\bbusco\s+para\s+mi\b",
    r"\bpara\s+mi\s+familia\b",
]

FRASES_PUBLICAR_PROPIEDAD = {
    "quiero vender mi propiedad",
    "quiero vender mi casa",
    "quiero vender mi apartamento",
    "quiero alquilar mi propiedad",
    "quiero alquilar mi casa",
    "quiero alquilar mi apartamento",
    "quiero publicar una propiedad",
    "quiero publicar mi propiedad",
    "quiero ofrecer una propiedad",
    "tengo una propiedad para vender",
    "tengo una propiedad para alquilar",
    "quiero valorar mi propiedad",
    "quiero una valoracion",
    "quiero una valoración",
}

FRASES_BUSQUEDA_PROPIEDAD = {
    "busco",
    "quiero comprar",
    "quiero alquilar",
    "necesito una casa",
    "necesito un apartamento",
    "estoy buscando",
    "comprar una propiedad",
    "alquilar una propiedad",
}

FRASES_CONSULTA_METTRYC = {
    "mettryc",
    "honorarios",
    "comision",
    "comisión",
    "oficina",
    "oficinas",
    "direccion",
    "dirección",
    "donde estan",
    "dónde están",
    "quiero ser asesor",
    "reclutamiento",
    "formulario",
    "redes sociales",
    "correo",
    "servicios",
    "quienes son",
    "quiénes son",
    "que hacen",
    "qué hacen",
}


# ============================================================
# CAMPOS Y ETIQUETAS DE LEADS
# ============================================================

CAMPO_ACK_LABELS = {
    "nombre completo": "el nombre",
    "correo electrónico": "el correo electrónico",
    "confirmación del número de WhatsApp": (
        "el número de WhatsApp"
    ),
}

CAMPO_INSTRUCCIONES = {
    "nombre completo": "Nombre y apellido",
    "correo electrónico": (
        "Correo electrónico, por ejemplo usuario@dominio.com"
    ),
    "confirmación del número de WhatsApp": (
        "Número de WhatsApp con código del país, "
        "por ejemplo +584123456789"
    ),
}

RESPUESTAS_AFIRMATIVAS = {
    "si",
    "sii",
    "sip",
    "si claro",
    "claro",
    "claro que si",
    "correcto",
    "exacto",
    "afirmativo",
    "por supuesto",
    "ok",
    "okay",
    "listo",
    "dale",
    "de una",
    "perfecto",
}

RESPUESTAS_NEGATIVAS = {
    "no",
    "no gracias",
    "negativo",
    "aun no",
    "todavia no",
    "no es correcto",
    "incorrecto",
    "para nada",
}

# ============================================================
# UTILIDADES DE NORMALIZACIÓN
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


def contiene_termino(
    texto_normalizado: str,
    termino: str,
) -> bool:
    if not texto_normalizado or not termino:
        return False

    patron = r"\b" + re.escape(termino) + r"\b"
    return re.search(patron, texto_normalizado) is not None


def lista_sin_duplicados(
    valores: List[Any],
) -> List[Any]:
    resultado: List[Any] = []
    vistos: Set[str] = set()

    for valor in valores:
        clave = normalizar_texto(valor)

        if not clave or clave in vistos:
            continue

        vistos.add(clave)
        resultado.append(valor)

    return resultado


def texto_es_solo_numero(texto: str) -> bool:
    return bool(
        re.fullmatch(
            r"\s*\d+\s*",
            str(texto or ""),
        )
    )


# ============================================================
# TELÉFONOS
# ============================================================

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

    if telefono.startswith("0") and len(telefono) == 11:
        telefono = "58" + telefono[1:]

    if len(telefono) == 10 and telefono.startswith("4"):
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


def extraer_telefono_detallado(
    texto: str,
) -> Tuple[Optional[str], Optional[str]]:
    coincidencia = re.search(
        r"(\+?\d[\d\s\-()]{7,}\d)",
        texto or "",
    )

    if not coincidencia:
        return None, None

    original = coincidencia.group(1)

    return (
        normalizar_telefono(original),
        original,
    )


def formatear_whatsapp(
    valor: Optional[str],
) -> str:
    if not valor:
        return "N/D"

    telefono = str(valor).strip()

    if not telefono:
        return "N/D"

    if telefono.startswith("+"):
        return telefono

    if telefono.startswith("00"):
        telefono = telefono[2:]

    return f"+{telefono}"


# ============================================================
# CORREOS
# ============================================================

PATRON_CORREO = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)


def extraer_correo(
    texto: str,
) -> Optional[str]:
    coincidencia = PATRON_CORREO.search(
        texto or ""
    )

    if not coincidencia:
        return None

    return coincidencia.group(0).lower()


def extraer_correo_detallado(
    texto: str,
) -> Tuple[Optional[str], Optional[str]]:
    coincidencia = PATRON_CORREO.search(
        texto or ""
    )

    if not coincidencia:
        return None, None

    original = coincidencia.group(0)

    return original.lower(), original


def correo_valido(
    valor: Any,
) -> bool:
    return bool(
        re.fullmatch(
            r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
            str(valor or ""),
            re.IGNORECASE,
        )
    )


# ============================================================
# VALIDACIÓN DE NOMBRES
# ============================================================

PALABRAS_NOMBRE_BLOQUEADAS = {
    "Hola",
    "Buenas",
    "Gracias",
    "Primera",
    "Segunda",
    "Tercera",
    "Cuarta",
    "Quinta",
    "Apartamento",
    "Casa",
    "Townhouse",
    "Oficina",
    "Local",
    "Terreno",
    "Quiero",
    "Visitar",
    "Opcion",
    "Cliente",
    "Busco",
    "Buscar",
    "Necesito",
    "Propiedad",
    "Inmueble",
    "Favor",
    "Comprar",
    "Alquilar",
    "Venta",
    "Alquiler",
    "Agendar",
    "Asesor",
    "Agente",
}


def nombre_valido(
    valor: Any,
) -> bool:
    nombre = normalizar_nombre(valor)

    if not nombre:
        return False

    palabras = nombre.split()

    if any(
        palabra in PALABRAS_NOMBRE_BLOQUEADAS
        for palabra in palabras
    ):
        return False

    if any(
        re.search(r"\d", palabra)
        for palabra in palabras
    ):
        return False

    return 2 <= len(palabras) <= 6


# ============================================================
# PRECIOS DE WASI
# ============================================================

def parsear_numero_localizado(
    valor: Any,
) -> float:
    if valor in [None, "", "N/D"]:
        return 0.0

    if isinstance(valor, (int, float)):
        return float(valor)

    texto = str(valor).strip()
    texto = re.sub(r"[^\d.,\-]", "", texto)

    if not texto:
        return 0.0

    if "." in texto and "," in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "")
            texto = texto.replace(",", ".")
        else:
            texto = texto.replace(",", "")

    elif "," in texto:
        partes = texto.split(",")

        if len(partes) == 2 and len(partes[-1]) <= 2:
            texto = texto.replace(",", ".")
        else:
            texto = texto.replace(",", "")

    elif "." in texto:
        partes = texto.split(".")

        if (
            len(partes) > 2
            or (
                len(partes) == 2
                and len(partes[-1]) == 3
            )
        ):
            texto = texto.replace(".", "")

    try:
        return float(texto)
    except ValueError:
        return 0.0


def parsear_precio_wasi(
    valor: Any,
    etiqueta: Any,
) -> float:
    numero = parsear_numero_localizado(valor)

    if numero > 0:
        return numero

    return parsear_numero_localizado(etiqueta)


def extraer_area_principal_wasi(
    payload: Dict[str, Any],
) -> Optional[float]:
    if not isinstance(payload, dict):
        return None

    candidatos = [
        payload.get("area"),
        payload.get("constructed_area"),
        payload.get("construction_area"),
        payload.get("built_area"),
        payload.get("building_area"),
        payload.get("total_area"),
        payload.get("surface"),
        payload.get("surface_total"),
        payload.get("lot_area"),
        payload.get("land_area"),
    ]

    for valor in candidatos:
        if valor in [
            None,
            "",
            "N/D",
            0,
            "0",
            "0.0",
        ]:
            continue

        if isinstance(valor, (int, float)):
            if float(valor) > 0:
                return float(valor)

        elif isinstance(valor, str):
            numero = parsear_numero_localizado(valor)

            if numero > 0:
                return numero

        elif isinstance(valor, dict):
            posible = (
                valor.get("value")
                or valor.get("valor")
                or valor.get("amount")
                or valor.get("number")
            )

            numero = parsear_numero_localizado(posible)

            if numero > 0:
                return numero

    return None


# ============================================================
# TIPOS DE PROPIEDAD
# ============================================================

def normalizar_tipo_propiedad(
    valor: Any,
) -> str:
    texto = normalizar_texto(valor)

    equivalencias = {
        "apto": "apartamento",
        "apart": "apartamento",
        "apartamento tipo estudio": "apartamento",
        "town house": "townhouse",
        "towhouse": "townhouse",
        "tohouse": "townhouse",
        "th": "townhouse",
        "aptoquinta": "apartoquinta",
        "aparto quinta": "apartoquinta",
        "ph": "penthouse",
        "galpon": "galpon",
        "deposito": "galpon",
        "lote": "terreno",
        "parcela": "terreno",
    }

    if texto in equivalencias:
        return equivalencias[texto]

    tipos = [
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
    ]

    for tipo in tipos:
        if tipo in texto:
            return tipo

    return texto


def detectar_tipo_propiedad(
    texto: str,
) -> Optional[str]:
    texto_norm = normalizar_texto(texto)

    patrones = [
        (
            "apartamento",
            [
                "apartamento",
                "apartamentos",
                "apto",
                "aparta",
                "aparthouse",
                "loft",
            ],
        ),
        (
            "penthouse",
            [
                "penthouse",
                "pent house",
                "ph",
            ],
        ),
        (
            "casa",
            [
                "casa",
                "casas",
                "quinta",
                "chalet",
                "villa",
            ],
        ),
        (
            "townhouse",
            [
                "townhouse",
                "town house",
                "th",
            ],
        ),
        (
            "oficina",
            [
                "oficina",
                "office",
            ],
        ),
        (
            "local",
            [
                "local",
                "tienda",
                "local comercial",
            ],
        ),
        (
            "galpon",
            [
                "galpon",
                "galpón",
                "deposito",
                "depósito",
            ],
        ),
        (
            "terreno",
            [
                "terreno",
                "lote",
                "parcela",
            ],
        ),
    ]

    for tipo, palabras in patrones:
        for palabra in palabras:
            palabra_norm = normalizar_texto(palabra)

            if contiene_termino(
                texto_norm,
                palabra_norm,
            ):
                return tipo

    return None


# ============================================================
# TOKENS DE NOMBRES Y ZONAS
# ============================================================

def tokens_nombre(
    valor: Any,
) -> Set[str]:
    bloqueadas = {
        "de",
        "del",
        "la",
        "el",
        "los",
        "las",
        "asesor",
        "asesora",
        "agente",
    }

    return {
        token
        for token in normalizar_texto(valor).split()
        if len(token) >= 2
        and token not in bloqueadas
    }


def tokens_zona(
    valor: Any,
) -> Set[str]:
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
        "municipio",
        "venezuela",
    }

    return {
        token
        for token in normalizar_texto(valor).split()
        if len(token) >= 2
        and token not in bloqueadas
    }


# ============================================================
# DETECCIÓN DE PRESUPUESTO
# ============================================================

PALABRAS_CONTEXTO_NO_PRESUPUESTO = {
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
    "estacionamiento",
    "estacionamientos",
    "metros",
    "metro",
    "m2",
}


def detectar_presupuesto(
    texto: str,
    permitir_numero_solo: bool = False,
) -> float:
    if not texto:
        return 0.0

    texto_norm = normalizar_texto(texto)

    if (
        not permitir_numero_solo
        and texto_es_solo_numero(texto)
    ):
        return 0.0

    patron = re.compile(
        r"(?<!\d)"
        r"(\d+(?:[.,]\d+)?)"
        r"\s*"
        r"(millones?|millon|m|mil|k)?"
        r"\s*"
        r"(usd|dolares|dolar|\$)?"
        r"(?!\d)",
        re.IGNORECASE,
    )

    mejor = 0.0

    for coincidencia in patron.finditer(texto_norm):
        valor_bruto = coincidencia.group(1)
        multiplicador = coincidencia.group(2)
        moneda = coincidencia.group(3)

        inicio = max(0, coincidencia.start() - 25)
        fin = min(
            len(texto_norm),
            coincidencia.end() + 25,
        )

        contexto = texto_norm[inicio:fin]

        if any(
            palabra in contexto
            for palabra in PALABRAS_CONTEXTO_NO_PRESUPUESTO
        ):
            if not multiplicador and not moneda:
                continue

        numero = parsear_numero_localizado(
            valor_bruto
        )

        if numero <= 0:
            continue

        factor = 1.0

        if multiplicador:
            multiplicador_norm = normalizar_texto(
                multiplicador
            )

            if multiplicador_norm in {"mil", "k"}:
                factor = 1_000.0

            elif multiplicador_norm in {
                "millon",
                "millones",
                "m",
            }:
                factor = 1_000_000.0

        monto = numero * factor

        if (
            factor == 1.0
            and not moneda
            and monto < 100
            and not permitir_numero_solo
        ):
            continue

        if monto > mejor:
            mejor = monto

    return mejor


# ============================================================
# OPERACIÓN DE COMPRA, ALQUILER O CAPTACIÓN
# ============================================================

def detectar_operacion(
    texto: str,
) -> Optional[str]:
    texto_norm = normalizar_texto(texto)

    patrones_venta = [
        "comprar",
        "compra",
        "en venta",
        "para comprar",
        "adquirir",
        "inversion",
    ]

    patrones_alquiler = [
        "alquiler",
        "alquilar",
        "renta",
        "arrendar",
        "arrendamiento",
        "para alquilar",
    ]

    if any(
        patron in texto_norm
        for patron in patrones_venta
    ):
        return "venta"

    if any(
        patron in texto_norm
        for patron in patrones_alquiler
    ):
        return "alquiler"

    return None


def detectar_operacion_propietario(
    texto: str,
) -> Optional[str]:
    texto_norm = normalizar_texto(texto)

    if any(
        frase in texto_norm
        for frase in [
            "quiero vender",
            "para vender",
            "vender mi",
            "venta de mi",
        ]
    ):
        return "venta"

    if any(
        frase in texto_norm
        for frase in [
            "quiero alquilar mi",
            "para alquilar mi",
            "poner en alquiler",
            "alquiler de mi",
        ]
    ):
        return "alquiler"

    if any(
        frase in texto_norm
        for frase in [
            "valorar",
            "valoracion",
            "avaluo",
            "cuanto vale",
            "que precio tiene mi propiedad",
        ]
    ):
        return "valoracion"

    return None


# ============================================================
# HABITACIONES, BAÑOS Y GARAJES
# ============================================================

def extraer_numero_desde_texto(
    texto_normalizado: str,
    palabras_clave: List[str],
) -> Optional[int]:
    if not texto_normalizado:
        return None

    for palabra in palabras_clave:
        palabra_norm = normalizar_texto(palabra)

        patrones = [
            rf"\b{re.escape(palabra_norm)}\s*(\d{{1,2}})\b",
            rf"\b(\d{{1,2}})\s*{re.escape(palabra_norm)}\b",
        ]

        for patron in patrones:
            coincidencia = re.search(
                patron,
                texto_normalizado,
            )

            if coincidencia:
                numero = int(coincidencia.group(1))

                if 0 <= numero <= 30:
                    return numero

    coincidencia_habitaciones = re.search(
        r"\b(\d{1,2})\s*h\b",
        texto_normalizado,
    )

    if coincidencia_habitaciones:
        return int(
            coincidencia_habitaciones.group(1)
        )

    return None


def detectar_habitaciones(
    texto_normalizado: str,
) -> Optional[int]:
    return extraer_numero_desde_texto(
        texto_normalizado,
        [
            "habitaciones",
            "habitacion",
            "cuartos",
            "cuarto",
            "rooms",
            "room",
            "hab",
        ],
    )


def detectar_banos(
    texto_normalizado: str,
) -> Optional[int]:
    return extraer_numero_desde_texto(
        texto_normalizado,
        [
            "banos",
            "bano",
            "wc",
        ],
    )


def detectar_garajes(
    texto_normalizado: str,
) -> Optional[int]:
    return extraer_numero_desde_texto(
        texto_normalizado,
        [
            "puestos",
            "puesto",
            "estacionamientos",
            "estacionamiento",
            "garajes",
            "garaje",
            "garage",
            "parking",
        ],
    )


# ============================================================
# CARACTERÍSTICAS SOLICITADAS
# ============================================================

CARACTERISTICAS_CLAVE = {
    "jardin": "jardín",
    "terraza": "terraza",
    "patio": "patio",
    "piscina": "piscina",
    "amoblado": "amoblado",
    "amoblada": "amoblado",
    "mobiliario": "amoblado",
    "ascensor": "ascensor",
    "vigilancia": "vigilancia",
    "vigilancia privada": "vigilancia",
    "seguridad": "seguridad",
    "tanque": "tanque de agua",
    "tanque de agua": "tanque de agua",
    "pozo": "pozo de agua",
    "pozo de agua": "pozo de agua",
    "estudio": "estudio",
    "family room": "family room",
    "parrillera": "parrillera",
    "bbq": "parrillera",
    "planta electrica": "planta eléctrica",
    "planta": "planta eléctrica",
    "aire acondicionado": "aire acondicionado",
    "maletero": "maletero",
    "balcon": "balcón",
    "gas directo": "gas directo",
    "internet": "internet",
    "agua constante": "agua constante",
    "mascotas": "mascotas",
}


def detectar_caracteristicas_extra(
    texto_normalizado: str,
) -> List[str]:
    encontradas: Set[str] = set()

    for clave, etiqueta in CARACTERISTICAS_CLAVE.items():
        clave_norm = normalizar_texto(clave)

        if contiene_termino(
            texto_normalizado,
            clave_norm,
        ):
            encontradas.add(etiqueta)

    return sorted(encontradas)


# ============================================================
# CÓDIGOS Y ENLACES DE INMUEBLES
# ============================================================

def extraer_codigo_mercadolibre(
    texto: str,
) -> Optional[str]:
    if not texto:
        return None

    coincidencia = MERCADOLIBRE_URL_RE.search(
        texto
    )

    if not coincidencia:
        return None

    return coincidencia.group(1)


def extraer_codigo_inmueble(
    mensaje: str,
) -> Optional[str]:
    texto = str(mensaje or "").strip()

    coincidencia_mettryc = URL_METTRYC_RE.search(
        texto
    )

    if coincidencia_mettryc:
        return coincidencia_mettryc.group(1)

    codigo_ml = extraer_codigo_mercadolibre(texto)

    if codigo_ml:
        return codigo_ml

    patrones = [
        r"\b(?:codigo|código|cod|inmueble)"
        r"\s*[:#-]?\s*(\d{4,})\b",

        r"\b(?:ALM|EJL|LR|JM|MFR|TH|AM)"
        r"[-.\s]*(\d{4,})\b",

        r"/MLV-\d+-[A-Za-z0-9\-]+-(\d+)-?_JM",

        r"\b[A-Z]{1,5}[-.\s]+(\d{4,})\b",
    ]

    for patron in patrones:
        coincidencia = re.search(
            patron,
            texto,
            re.IGNORECASE,
        )

        if not coincidencia:
            continue

        codigo = re.sub(
            r"\D",
            "",
            coincidencia.group(1),
        )

        if re.fullmatch(r"\d{4,}", codigo):
            return codigo

    if re.fullmatch(r"\s*\d{4,10}\s*", texto):
        return re.sub(r"\D", "", texto)

    return None


# ============================================================
# DETECCIÓN DE POSICIONES
# ============================================================

def detectar_posicion(
    mensaje: str,
) -> Optional[int]:
    texto = normalizar_texto(mensaje)

    if not texto:
        return None

    patrones_por_posicion = {
        1: [
            r"\bprimera\b",
            r"\bprimero\b",
            r"\b1(?:era|ra)?\b",
            r"\bopcion\s*(?:numero\s*)?1\b",
        ],
        2: [
            r"\bsegunda\b",
            r"\bsegundo\b",
            r"\b2(?:da|nda)?\b",
            r"\bopcion\s*(?:numero\s*)?2\b",
        ],
        3: [
            r"\btercera\b",
            r"\btercero\b",
            r"\b3(?:era|ra)?\b",
            r"\bopcion\s*(?:numero\s*)?3\b",
        ],
        4: [
            r"\bcuarta\b",
            r"\bcuarto\b",
            r"\b4(?:ta|rta)?\b",
            r"\bopcion\s*(?:numero\s*)?4\b",
        ],
        5: [
            r"\bquinta\b",
            r"\bquinto\b",
            r"\b5(?:ta|nta)?\b",
            r"\bopcion\s*(?:numero\s*)?5\b",
        ],
    }

    for posicion, patrones in patrones_por_posicion.items():
        if any(
            re.search(patron, texto)
            for patron in patrones
        ):
            return posicion

    coincidencia = re.search(
        r"\b(?:opcion|propiedad|inmueble|casa)"
        r"\s*(?:numero|num|#)?\s*(\d)\b",
        texto,
    )

    if coincidencia:
        numero = int(coincidencia.group(1))

        if 1 <= numero <= 5:
            return numero

    if texto in {"ultima", "la ultima"}:
        return 5

    return None


# ============================================================
# ROL DEL USUARIO
# ============================================================

def detectar_rol_explicito(
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


def solicita_humano(
    mensaje: str,
) -> bool:
    texto = normalizar_texto(mensaje)

    return any(
        re.search(patron, texto)
        for patron in PATRONES_SOLICITUD_HUMANO
    )


# ============================================================
# CLASIFICACIÓN DE SALUDOS Y CONSULTAS
# ============================================================

def es_saludo_sin_intencion(
    mensaje: str,
) -> bool:
    texto = normalizar_texto(mensaje)

    if not texto:
        return False

    palabras = texto.split()

    if texto in {
        normalizar_texto(frase)
        for frase in FRASES_SALUDO
    }:
        return True

    contiene_saludo = any(
        texto.startswith(normalizar_texto(frase))
        for frase in FRASES_SALUDO
    )

    if not contiene_saludo:
        return False

    indicadores_intencion = (
        FRASES_OTROS_ANUNCIOS
        | FRASES_PUBLICAR_PROPIEDAD
        | FRASES_BUSQUEDA_PROPIEDAD
        | FRASES_CONSULTA_METTRYC
    )

    if any(
        normalizar_texto(frase) in texto
        for frase in indicadores_intencion
    ):
        return False

    if detectar_tipo_propiedad(texto):
        return False

    if detectar_operacion(texto):
        return False

    if solicita_humano(texto):
        return False

    return len(palabras) <= 8


def menciona_otro_anuncio(
    mensaje: str,
) -> bool:
    texto = normalizar_texto(mensaje)

    if extraer_codigo_mercadolibre(mensaje):
        return False

    if extraer_codigo_inmueble(mensaje):
        return True

    if any(
        normalizar_texto(frase) in texto
        for frase in FRASES_OTROS_ANUNCIOS
    ):
        return True

    menciona_propiedad = any(
        palabra in texto
        for palabra in [
            "propiedad",
            "inmueble",
            "casa",
            "apartamento",
            "anuncio",
            "publicacion",
        ]
    )

    pregunta_dato = any(
        normalizar_texto(frase) in texto
        for frase in PALABRAS_CONSULTA_DIRECTA
    )

    menciona_origen = any(
        origen in texto
        for origen in [
            "instagram",
            "facebook",
            "portal",
            "pagina web",
            "publicacion",
            "anuncio",
            "redes",
        ]
    )

    return (
        menciona_propiedad
        and (
            pregunta_dato
            or menciona_origen
        )
    )


def solicita_publicar_propiedad(
    mensaje: str,
) -> bool:
    texto = normalizar_texto(mensaje)

    if any(
        normalizar_texto(frase) in texto
        for frase in FRASES_PUBLICAR_PROPIEDAD
    ):
        return True

    tiene_posesivo = any(
        frase in texto
        for frase in [
            "mi propiedad",
            "mi casa",
            "mi apartamento",
            "mi inmueble",
            "un inmueble que tengo",
        ]
    )

    tiene_accion = any(
        accion in texto
        for accion in [
            "vender",
            "alquilar",
            "publicar",
            "valorar",
            "avaluo",
        ]
    )

    return tiene_posesivo and tiene_accion


def parece_busqueda_propiedad(
    mensaje: str,
) -> bool:
    texto = normalizar_texto(mensaje)

    if solicita_publicar_propiedad(mensaje):
        return False

    if any(
        normalizar_texto(frase) in texto
        for frase in FRASES_BUSQUEDA_PROPIEDAD
    ):
        return True

    return bool(
        detectar_tipo_propiedad(texto)
        or detectar_operacion(texto)
    )


def parece_consulta_mettryc(
    mensaje: str,
) -> bool:
    texto = normalizar_texto(mensaje)

    return any(
        normalizar_texto(frase) in texto
        for frase in FRASES_CONSULTA_METTRYC
    )


def pide_mas_opciones(
    mensaje: str,
) -> bool:
    texto = normalizar_texto(mensaje)

    frases = [
        "mas opciones",
        "otras opciones",
        "otras tres",
        "otras cinco",
        "muestrame otras",
        "enviame otras",
        "quiero ver mas",
        "ninguna me gusto",
        "ninguna me interesa",
        "siguientes opciones",
        "ver mas propiedades",
    ]

    return any(
        frase in texto
        for frase in frases
    )


def solicita_agendar_visita(
    mensaje: str,
) -> bool:
    texto = normalizar_texto(mensaje)

    frases = [
        "agendar una visita",
        "agendar visita",
        "coordinar una visita",
        "coordinar visita",
        "quiero visitar",
        "quiero verla",
        "quiero verlo",
        "visitar la propiedad",
        "visitar el inmueble",
        "conocer la propiedad",
        "ver la propiedad",
        "quiero ir",
    ]

    return any(
        frase in texto
        for frase in frases
    )


def solicita_preguntar_propiedad(
    mensaje: str,
) -> bool:
    texto = normalizar_texto(mensaje)

    frases = [
        "hacer una pregunta",
        "preguntar algo",
        "saber algo",
        "conocer mas",
        "conocer más",
        "mas informacion",
        "más información",
        "tengo una pregunta",
        "quiero preguntar",
        "consultar la propiedad",
    ]

    return any(
        normalizar_texto(frase) in texto
        for frase in frases
    )


# ============================================================
# CLASIFICADOR DETERMINISTA DE ENTRADA
# ============================================================

def clasificar_evento_entrada(
    mensaje: str,
    es_multimedia: bool = False,
) -> EventoEntrada:
    texto = str(mensaje or "").strip()

    if texto.startswith("/"):
        return EventoEntrada(tipo="comando")

    if es_multimedia:
        return EventoEntrada(tipo="multimedia")

    if solicita_humano(texto):
        return EventoEntrada(
            tipo="solicitud_humano"
        )

    codigo_ml = extraer_codigo_mercadolibre(
        texto
    )

    if codigo_ml:
        return EventoEntrada(
            tipo="mercadolibre",
            codigo=codigo_ml,
        )

    rol = detectar_rol_explicito(texto)

    if rol == "colega_inmobiliario":
        return EventoEntrada(tipo="colega")

    if solicita_publicar_propiedad(texto):
        return EventoEntrada(
            tipo="busqueda_propiedad"
        )

    if menciona_otro_anuncio(texto):
        return EventoEntrada(
            tipo="otro_anuncio",
            codigo=extraer_codigo_inmueble(texto),
        )

    if es_saludo_sin_intencion(texto):
        return EventoEntrada(tipo="saludo")

    if parece_busqueda_propiedad(texto):
        return EventoEntrada(
            tipo="busqueda_propiedad"
        )

    if parece_consulta_mettryc(texto):
        return EventoEntrada(
            tipo="consulta_mettryc"
        )

    return EventoEntrada(tipo="desconocido")


# ============================================================
# RESOLUCIÓN DE OPCIONES CONVERSACIONALES
# ============================================================

ORDINALES_POR_NUMERO = {
    1: {
        "primera",
        "primero",
        "la primera",
        "el primero",
    },
    2: {
        "segunda",
        "segundo",
        "la segunda",
        "el segundo",
    },
    3: {
        "tercera",
        "tercero",
        "la tercera",
        "el tercero",
    },
    4: {
        "cuarta",
        "cuarto",
        "la cuarta",
        "el cuarto",
    },
    5: {
        "quinta",
        "quinto",
        "la quinta",
        "el quinto",
    },
    6: {
        "sexta",
        "sexto",
        "la sexta",
        "el sexto",
    },
}


def resolver_opcion(
    mensaje: str,
    pregunta: PreguntaConversacional,
) -> ResultadoOpcion:
    texto = normalizar_texto(mensaje)

    if not texto:
        return ResultadoOpcion()

    for opcion in pregunta.opciones:
        representaciones_numero = {
            str(opcion.numero),
            f"opcion {opcion.numero}",
            f"numero {opcion.numero}",
            f"la opcion {opcion.numero}",
        }

        representaciones_numero.update(
            ORDINALES_POR_NUMERO.get(
                opcion.numero,
                set(),
            )
        )

        representaciones_norm = {
            normalizar_texto(valor)
            for valor in representaciones_numero
        }

        if texto in representaciones_norm:
            return ResultadoOpcion(
                opcion_id=opcion.id,
                entendida=True,
            )

        etiqueta_norm = normalizar_texto(
            opcion.etiqueta
        )

        if (
            texto == etiqueta_norm
            or etiqueta_norm in texto
        ):
            return ResultadoOpcion(
                opcion_id=opcion.id,
                entendida=True,
            )

        for alias in opcion.aliases:
            alias_norm = normalizar_texto(alias)

            if not alias_norm:
                continue

            if (
                texto == alias_norm
                or alias_norm in texto
            ):
                return ResultadoOpcion(
                    opcion_id=opcion.id,
                    entendida=True,
                )

    return ResultadoOpcion()


def renderizar_pregunta(
    pregunta: PreguntaConversacional,
    incluir_instruccion: bool = True,
) -> str:
    lineas = [
        pregunta.texto.strip(),
        "",
    ]

    for opcion in pregunta.opciones:
        lineas.append(
            f"{opcion.numero}. {opcion.etiqueta}"
        )

    if incluir_instruccion:
        lineas.extend(
            [
                "",
                "Responde con el número de la opción.",
            ]
        )

    return "\n".join(lineas).strip()


def guardar_pregunta_activa(
    estado: Dict[str, Any],
    pregunta: PreguntaConversacional,
    paso: str,
) -> str:
    estado["pregunta_activa"] = (
        pregunta.model_dump()
    )

    estado["opciones_esperadas"] = [
        opcion.id
        for opcion in pregunta.opciones
    ]

    estado["paso"] = paso

    return renderizar_pregunta(pregunta)


def obtener_pregunta_activa(
    estado: Dict[str, Any],
) -> Optional[PreguntaConversacional]:
    datos = estado.get("pregunta_activa")

    if not isinstance(datos, dict):
        return None

    try:
        return PreguntaConversacional.model_validate(
            datos
        )
    except ValidationError:
        estado["pregunta_activa"] = None
        estado["opciones_esperadas"] = []
        return None


def limpiar_pregunta_activa(
    estado: Dict[str, Any],
) -> None:
    estado["pregunta_activa"] = None
    estado["opciones_esperadas"] = []


# ============================================================
# RESPUESTAS AFIRMATIVAS Y NEGATIVAS
# ============================================================

def es_respuesta_afirmativa(
    texto: str,
) -> bool:
    texto_norm = normalizar_texto(texto)

    if not texto_norm:
        return False

    if texto_norm in RESPUESTAS_AFIRMATIVAS:
        return True

    return texto_norm.startswith("si ")


def es_respuesta_negativa(
    texto: str,
) -> bool:
    texto_norm = normalizar_texto(texto)

    if not texto_norm:
        return False

    if texto_norm in RESPUESTAS_NEGATIVAS:
        return True

    return texto_norm.startswith("no ")


# ============================================================
# DEDUPLICACIÓN DE MENSAJES
# ============================================================

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
# SEGURIDAD DE COMANDOS DE CHAT
# ============================================================

def es_sender_admin(
    sender: str,
) -> bool:
    sender_limpio = str(sender or "").strip()

    if not sender_limpio:
        return False

    return sender_limpio in CHAT_ADMIN_SENDERS

# ============================================================
# CATÁLOGO DE MENÚS Y PREGUNTAS ESTRUCTURADAS
# ============================================================

PREGUNTA_MENU_PRINCIPAL = PreguntaConversacional(
    id="menu_principal",
    texto="¡Hola! Soy Paty de Mettryc Realty. ¿Cómo te puedo ayudar hoy?",
    opciones=[
        OpcionConversacional(
            id="comprar",
            numero=1,
            etiqueta="Comprar una propiedad",
            aliases=["comprar", "compra", "busco comprar"],
        ),
        OpcionConversacional(
            id="alquilar",
            numero=2,
            etiqueta="Alquilar una propiedad",
            aliases=["alquilar", "alquiler", "renta", "busco alquilar"],
        ),
        OpcionConversacional(
            id="anuncio",
            numero=3,
            etiqueta="Consultar un anuncio o propiedad vista en internet",
            aliases=["anuncio", "mercado libre", "mercadolibre", "vi una propiedad"],
        ),
        OpcionConversacional(
            id="publicar",
            numero=4,
            etiqueta="Publicar mi propiedad para vender o alquilar",
            aliases=["publicar", "vender mi propiedad", "alquilar mi propiedad"],
        ),
        OpcionConversacional(
            id="humano",
            numero=5,
            etiqueta="Hablar con un asesor humano",
            aliases=["asesor", "humano", "agente", "hablar con alguien"],
        ),
        OpcionConversacional(
            id="mettryc",
            numero=6,
            etiqueta="Información sobre Mettryc Realty (oficinas, honorarios, etc.)",
            aliases=["oficinas", "honorarios", "informacion", "mettryc"],
        ),
    ],
)

PREGUNTA_OPERACION_CLIENTE = PreguntaConversacional(
    id="operacion_cliente",
    texto="¿Qué tipo de operación estás buscando?",
    opciones=[
        OpcionConversacional(
            id="venta",
            numero=1,
            etiqueta="Comprar una propiedad",
            aliases=["comprar", "compra", "venta"],
        ),
        OpcionConversacional(
            id="alquiler",
            numero=2,
            etiqueta="Alquilar una propiedad",
            aliases=["alquilar", "alquiler", "renta"],
        ),
    ],
)

PREGUNTA_TIPO_PROPIEDAD = PreguntaConversacional(
    id="tipo_propiedad",
    texto="¿Qué tipo de inmueble necesitas?",
    opciones=[
        OpcionConversacional(
            id="apartamento",
            numero=1,
            etiqueta="Apartamento",
            aliases=["apartamento", "apto"],
        ),
        OpcionConversacional(
            id="casa",
            numero=2,
            etiqueta="Casa / Quinta",
            aliases=["casa", "quinta"],
        ),
        OpcionConversacional(
            id="townhouse",
            numero=3,
            etiqueta="Townhouse",
            aliases=["townhouse", "town house", "th"],
        ),
        OpcionConversacional(
            id="oficina",
            numero=4,
            etiqueta="Oficina",
            aliases=["oficina"],
        ),
        OpcionConversacional(
            id="local",
            numero=5,
            etiqueta="Local comercial",
            aliases=["local", "comercial"],
        ),
        OpcionConversacional(
            id="galpon",
            numero=6,
            etiqueta="Galpón / Depósito",
            aliases=["galpon", "deposito"],
        ),
        OpcionConversacional(
            id="terreno",
            numero=7,
            etiqueta="Terreno / Lote",
            aliases=["terreno", "lote", "parcela"],
        ),
    ],
    permite_texto_libre=True,
)

PREGUNTA_ROL_USUARIO = PreguntaConversacional(
    id="rol_usuario",
    texto="¿Buscas esta propiedad para ti/tu familia o eres colega inmobiliario?",
    opciones=[
        OpcionConversacional(
            id="cliente",
            numero=1,
            etiqueta="Es para mí / uso personal o familiar",
            aliases=["para mi", "personal", "cliente final"],
        ),
        OpcionConversacional(
            id="colega_inmobiliario",
            numero=2,
            etiqueta="Soy asesor / colega inmobiliario y busco para un cliente",
            aliases=["colega", "asesor", "agente", "tengo un cliente"],
        ),
    ],
)

PREGUNTA_ACCION_RESULTADOS_CLIENTE = PreguntaConversacional(
    id="accion_resultados_cliente",
    texto="¿Qué te gustaría hacer con estas opciones?",
    opciones=[
        OpcionConversacional(
            id="agendar_visita",
            numero=1,
            etiqueta="Agendar una visita a una propiedad",
            aliases=["agendar", "visitar", "visita"],
        ),
        OpcionConversacional(
            id="preguntar_propiedad",
            numero=2,
            etiqueta="Hacer una pregunta sobre alguna de las propiedades",
            aliases=["preguntar", "saber mas", "informacion"],
        ),
        OpcionConversacional(
            id="mostrar_mas",
            numero=3,
            etiqueta="Ver más opciones disponibles",
            aliases=["mas opciones", "ver mas", "siguientes"],
        ),
        OpcionConversacional(
            id="cambiar_busqueda",
            numero=4,
            etiqueta="Cambiar los criterios de búsqueda (zona, presupuesto, tipo)",
            aliases=["cambiar", "nueva busqueda", "otra zona"],
        ),
        OpcionConversacional(
            id="solicitar_humano",
            numero=5,
            etiqueta="Hablar directamente con un asesor",
            aliases=["asesor", "humano", "agente"],
        ),
    ],
)

PREGUNTA_ACCION_RESULTADOS_COLEGA = PreguntaConversacional(
    id="accion_resultados_colega",
    texto="¿Qué deseas hacer, colega?",
    opciones=[
        OpcionConversacional(
            id="contactar_captador",
            numero=1,
            etiqueta="Seleccionar una opción para ver datos del captador",
            aliases=["captador", "contactar", "whatsapp captador"],
        ),
        OpcionConversacional(
            id="mostrar_mas",
            numero=2,
            etiqueta="Ver más opciones disponibles",
            aliases=["mas opciones", "ver mas"],
        ),
        OpcionConversacional(
            id="cambiar_busqueda",
            numero=3,
            etiqueta="Ajustar los criterios de búsqueda",
            aliases=["cambiar", "ajustar"],
        ),
        OpcionConversacional(
            id="solicitar_humano",
            numero=4,
            etiqueta="Solicitar apoyo directo del equipo administrativo Mettryc",
            aliases=["apoyo", "administracion", "soporte"],
        ),
    ],
)

PREGUNTA_ACCION_FICHA_UNICA = PreguntaConversacional(
    id="accion_ficha_unica",
    texto="¿Qué deseas hacer con esta propiedad?",
    opciones=[
        OpcionConversacional(
            id="agendar_visita",
            numero=1,
            etiqueta="Agendar una visita",
            aliases=["agendar", "visitar", "visita"],
        ),
        OpcionConversacional(
            id="preguntar_propiedad",
            numero=2,
            etiqueta="Hacer una pregunta sobre este inmueble",
            aliases=["preguntar", "saber mas", "informacion"],
        ),
        OpcionConversacional(
            id="solicitar_humano",
            numero=3,
            etiqueta="Hablar con un asesor",
            aliases=["asesor", "humano", "agente"],
        ),
        OpcionConversacional(
            id="buscar_similares",
            numero=4,
            etiqueta="Buscar otras propiedades similares",
            aliases=["similares", "otras opciones", "buscar mas"],
        ),
    ],
)

PREGUNTA_OPERACION_PROPIETARIO = PreguntaConversacional(
    id="operacion_propietario",
    texto="¡Excelente! En Mettryc Realty te ayudamos a promover tu inmueble. ¿Qué deseas hacer?",
    opciones=[
        OpcionConversacional(
            id="venta",
            numero=1,
            etiqueta="Vender mi propiedad",
            aliases=["vender", "venta"],
        ),
        OpcionConversacional(
            id="alquiler",
            numero=2,
            etiqueta="Alquilar mi propiedad",
            aliases=["alquilar", "alquiler"],
        ),
        OpcionConversacional(
            id="valoracion",
            numero=3,
            etiqueta="Solicitar una valoración de mercado previa",
            aliases=["valoracion", "avaluo", "precio"],
        ),
    ],
)

PREGUNTA_CATEGORIAS_METTRYC = PreguntaConversacional(
    id="categorias_mettryc",
    texto="¿Sobre qué tema deseas información de Mettryc Realty?",
    opciones=[
        OpcionConversacional(
            id="oficinas",
            numero=1,
            etiqueta="Ubicación de nuestras oficinas",
            aliases=["oficinas", "ubicacion", "direccion"],
        ),
        OpcionConversacional(
            id="honorarios",
            numero=2,
            etiqueta="Honorarios y comisiones",
            aliases=["honorarios", "comision", "porcentaje"],
        ),
        OpcionConversacional(
            id="reclutamiento",
            numero=3,
            etiqueta="Quiero ser asesor inmobiliario en Mettryc",
            aliases=["reclutamiento", "ser asesor", "trabajar"],
        ),
        OpcionConversacional(
            id="contacto",
            numero=4,
            etiqueta="Redes sociales y correo general",
            aliases=["contacto", "redes", "correo"],
        ),
        OpcionConversacional(
            id="menu_principal",
            numero=5,
            etiqueta="Volver al menú principal",
            aliases=["volver", "menu"],
        ),
    ],
)


# ============================================================
# SESIONES Y MÁQUINA DE ESTADOS
# ============================================================

def crear_sesion(sender: str) -> Dict[str, Any]:
    return {
        "sender": sender,
        "rol": None,
        "confianza_rol": 0.0,
        "pregunta_rol_realizada": False,

        "modo": "inicio",
        "paso": "esperando_intencion",
        "objetivo": "conversar",
        "origen_consulta": "directo",

        "pregunta_activa": None,
        "opciones_esperadas": [],

        "operacion_confirmada": False,
        "pregunta_presupuesto_colega_realizada": False,
        "esperando_presupuesto": False,
        "esperando_codigo": False,

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

        "propiedad_contexto": None,
        "propiedad_interes": None,
        "propiedad_pregunta": None,
        "propiedad_seleccionada_posicion": None,

        "lead_propietario": {
            "tipo_operacion": None,
            "ubicacion": None,
            "precio_deseado": None,
        },

        "lead": {
            "nombre": None,
            "correo": None,
            "whatsapp": None,
            "whatsapp_confirmado": False,
        },

        "motivo_lead": "interes_propiedad",
        "lead_confirmacion_pendiente": False,
        "lead_confirmado": False,
        "numero_canal": normalizar_telefono(sender) if SENDER_ES_WHATSAPP else None,
        "lead_id": None,
        "agente_asignado": None,
        "notificacion_enviada": False,

        "historial": [],
        "creado_en": datetime.utcnow().isoformat(),
        "actualizado_en": datetime.utcnow().isoformat(),

        "resultados_cache_key": None,
        "ultimos_resultados": [],
        "ultimo_resultado_enviado": [],
        "ultimo_motivo_falla": None,
        "requiere_confirmar_ciudad": None,
        "accion_sistema": None,
        "pausa_hasta": None,
    }


def obtener_sesion(sender: str) -> Dict[str, Any]:
    if sender not in sesiones:
        sesiones[sender] = crear_sesion(sender)
    return sesiones[sender]


def guardar_sesion(sender: str, estado: Dict[str, Any]) -> None:
    estado["actualizado_en"] = datetime.utcnow().isoformat()
    sesiones[sender] = estado


def agregar_historial(estado: Dict[str, Any], rol: str, contenido: str) -> None:
    contenido_limpio = str(contenido or "").strip()

    if len(contenido_limpio) > 5000:
        contenido_limpio = contenido_limpio[:5000]

    estado["historial"].append(
        {
            "role": rol,
            "content": contenido_limpio,
        }
    )

    estado["historial"] = estado["historial"][-MAX_HISTORIAL:]


def reiniciar_busqueda(estado: Dict[str, Any]) -> Dict[str, Any]:
    rol = estado.get("rol")
    confianza = estado.get("confianza_rol", 0.0)
    historial = estado.get("historial", [])
    numero_canal = estado.get("numero_canal")

    nuevo = crear_sesion(numero_canal or estado.get("sender", ""))
    nuevo["rol"] = rol
    nuevo["confianza_rol"] = confianza
    nuevo["pregunta_rol_realizada"] = bool(rol)
    nuevo["historial"] = historial
    nuevo["numero_canal"] = numero_canal
    nuevo["modo"] = "busqueda"
    nuevo["paso"] = "esperando_filtros"

    return nuevo


def verificar_caducidad_y_amnesia(estado: Dict[str, Any]) -> Dict[str, Any]:
    if not estado.get("actualizado_en"):
        return estado

    ahora = datetime.utcnow()

    try:
        ultima_actualizacion = datetime.fromisoformat(
            estado["actualizado_en"]
        )
    except ValueError:
        return estado

    tiempo_inactivo = ahora - ultima_actualizacion
    rol = estado.get("rol", "cliente")
    sender = estado.get("sender") or estado.get("numero_canal", "")

    if rol == "colega_inmobiliario" and tiempo_inactivo > timedelta(hours=24):
        logger.info(
            f"⏳ Sesión de colega ({sender[-4:] if len(sender)>=4 else sender}) "
            "caducada por inactividad (24h). Reiniciando estado."
        )
        return crear_sesion(sender)

    if rol != "colega_inmobiliario" and tiempo_inactivo > timedelta(days=30):
        logger.info(
            f"⏳ Sesión de cliente ({sender[-4:] if len(sender)>=4 else sender}) "
            "superó los 30 días. Reiniciando estado."
        )
        return crear_sesion(sender)

    if rol != "colega_inmobiliario" and tiempo_inactivo > timedelta(hours=24):
        if estado.get("historial"):
            nombre_log = estado["lead"].get("nombre") or "Desconocido"
            logger.info(
                f"🧠 Amnesia Selectiva Mettryc: Limpiando mensajes viejos de "
                f"{nombre_log} (>24h). Filtros y sesión conservados."
            )
            estado["historial"] = []

    return estado


# ============================================================
# AUDITORÍA INTERNA Y LOGS (CHISMOSO)
# ============================================================

class Chismoso:
    @staticmethod
    def _safe_sender(sender: Any) -> str:
        if sender is None:
            return "DEBUG"
        sender_str = str(sender)
        return sender_str if len(sender_str) >= 4 else f"DEBUG_{sender_str}"

    @staticmethod
    def log_extraction(
        sender: Any,
        campo: str,
        valor: Any,
        metodo: str,
        confianza: float = None,
    ):
        if not DEBUG_MODE:
            return

        sender_str = Chismoso._safe_sender(sender)
        mensaje = (
            f"\n🔍 [EXTRACCIÓN] ({sender_str[-4:]})\n"
            f"   Campo: {campo}\n"
            f"   Valor: {valor}\n"
            f"   Método: {metodo}\n"
            + (f"   Confianza IA: {confianza}\n" if confianza else "")
        )
        logging.info(mensaje)

    @staticmethod
    def log_inventario(
        sender: Any,
        filtros: dict,
        total_propiedades: int = 0,
    ):
        if not DEBUG_MODE:
            return

        sender_str = Chismoso._safe_sender(sender)
        mensaje = (
            f"\n📊 [INVENTARIO] ({sender_str[-4:]})\n"
            f"   Total propiedades: {total_propiedades}\n"
            f"   Filtros aplicados:\n"
            f"   - Operación: {filtros.get('tipo_operacion')}\n"
            f"   - Tipo: {filtros.get('tipo_propiedad')}\n"
            f"   - Ciudad: {filtros.get('ciudad')}\n"
            f"   - Zona: {filtros.get('zona')}\n"
            f"   - Presupuesto: {formato_moneda(filtros.get('presupuesto_max'))}\n"
            f"   - Habitaciones: {filtros.get('habitaciones_min')}\n"
        )
        logging.info(mensaje)

    @staticmethod
    def log_resultados(
        sender: Any,
        propiedades: List[dict],
        total_evaluadas: int = 0,
        exactas: int = 0,
        aproximadas: int = 0,
    ):
        if not DEBUG_MODE:
            return

        sender_str = Chismoso._safe_sender(sender)
        mensaje = (
            f"\n📩 [RESULTADOS] ({sender_str[-4:]})\n"
            f"   Total evaluadas: {total_evaluadas}\n"
            f"   Exactas: {exactas}\n"
            f"   Aproximadas: {aproximadas}\n"
            f"   Enviadas: {len(propiedades)}\n"
        )

        for idx, prop in enumerate(propiedades, 1):
            mensaje += (
                f"   {idx}. {prop.get('titulo')}\n"
                f"       ID: {prop.get('id')}\n"
                f"       Zona: {prop.get('zona')}\n"
                f"       Ciudad: {prop.get('ciudad')}\n"
                f"       Coincidencia: {prop.get('_coincidencia')}\n"
                f"       Score: {prop.get('_score')}\n"
            )

        logging.info(mensaje)

    @staticmethod
    def log_falla(sender: Any, motivo: str, filtros: dict):
        if not DEBUG_MODE:
            return

        sender_str = Chismoso._safe_sender(sender)
        mensaje = (
            f"\n⚠️ [FALLA BÚSQUEDA] ({sender_str[-4:]})\n"
            f"   Motivo: {motivo}\n"
            f"   Filtros:\n"
            f"   - Operación: {filtros.get('tipo_operacion')}\n"
            f"   - Tipo: {filtros.get('tipo_propiedad')}\n"
            f"   - Ciudad: {filtros.get('ciudad')}\n"
            f"   - Zona: {filtros.get('zona')}\n"
            f"   - Presupuesto: {formato_moneda(filtros.get('presupuesto_max'))}\n"
        )
        logging.info(mensaje)

# ============================================================
# INTEGRACIÓN Y CACHÉ DE WASI API
# ============================================================

def inventario_necesita_actualizacion() -> bool:
    ultima = inventory_cache.get("ultima_actualizacion")

    if not inventory_cache["inventario"]:
        return True

    if not ultima:
        return True

    return datetime.utcnow() - ultima >= INTERVALO_ACTUALIZACION_WASI


def normalizar_caracteristicas_wasi(valor: Any) -> List[str]:
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
                    or elemento.get("value")
                )
                if nombre:
                    resultado.append(str(nombre).strip())
            elif elemento not in [None, "", False, 0, "0"]:
                resultado.append(str(elemento).strip())

    elif isinstance(valor, dict):
        for clave, contenido in valor.items():
            if contenido in [None, "", False, 0, "0"]:
                continue

            if isinstance(contenido, str) and contenido.strip():
                resultado.append(contenido.strip())
            else:
                resultado.append(str(clave).strip())

    return list(
        dict.fromkeys(
            elemento for elemento in resultado if elemento
        )
    )


def convertir_caracteristicas_a_texto(valor: Any) -> str:
    lista = normalizar_caracteristicas_wasi(valor)
    return " ".join(lista).strip()


async def obtener_inventario_wasi() -> List[dict]:
    if not WASI_TOKEN or not WASI_COMPANY_ID:
        logger.error("Faltan WASI_TOKEN o WASI_COMPANY_ID en las variables de entorno.")
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
                    f"Error Wasi skip={skip} intento={intento + 1} tipo={type(exc).__name__}"
                )
                await asyncio.sleep(2 ** intento)

        if not isinstance(data, dict):
            break

        cantidad_pagina = 0

        for clave, valor in data.items():
            if not (isinstance(valor, dict) and str(clave).isdigit()):
                continue

            cantidad_pagina += 1
            property_id = valor.get("id_property")

            if not property_id:
                continue

            usuario = valor.get("user_data") or {}
            first_name = usuario.get("first_name", "") or ""
            last_name = usuario.get("last_name", "") or ""
            captador = f"{first_name} {last_name}".strip()

            descripcion = (
                valor.get("description")
                or valor.get("observations")
                or ""
            )

            caracteristicas_generales = normalizar_caracteristicas_wasi(
                valor.get("features")
            )
            caracteristicas_internas = normalizar_caracteristicas_wasi(
                valor.get("internal_features")
            )
            caracteristicas_externas = normalizar_caracteristicas_wasi(
                valor.get("external_features")
            )

            caracteristicas_texto = " ".join(
                [
                    convertir_caracteristicas_a_texto(valor.get("features")),
                    convertir_caracteristicas_a_texto(valor.get("internal_features")),
                    convertir_caracteristicas_a_texto(valor.get("external_features")),
                ]
            ).strip()

            localidad_wasi = str(valor.get("location_label") or "").strip()
            zona_wasi = str(valor.get("zone_label") or "").strip()

            if localidad_wasi and zona_wasi:
                if localidad_wasi.lower() in zona_wasi.lower():
                    zona_combinada = zona_wasi
                else:
                    zona_combinada = f"{localidad_wasi}, {zona_wasi}"
            else:
                zona_combinada = localidad_wasi or zona_wasi or "N/D"

            area_principal = extraer_area_principal_wasi(valor)

            precio_vta = parsear_precio_wasi(
                valor.get("sale_price"),
                valor.get("sale_price_label"),
            )
            precio_alq = parsear_precio_wasi(
                valor.get("rent_price"),
                valor.get("rent_price_label"),
            )

            propiedades.append(
                {
                    "id": str(property_id),
                    "titulo": valor.get("title", "Propiedad Mettryc"),
                    "descripcion": descripcion,
                    "ciudad": valor.get("city_label", "N/D"),
                    "zona": zona_combinada,
                    "tipo_propiedad_wasi": valor.get("type_label", "N/D"),
                    "precio_venta": precio_vta,
                    "precio_alquiler": precio_alq,
                    "precio_venta_label": valor.get("sale_price_label", "N/D"),
                    "precio_alquiler_label": valor.get("rent_price_label", "N/D"),
                    "area": area_principal if area_principal else "N/D",
                    "habitaciones": valor.get("bedrooms", "N/D"),
                    "banos": valor.get("bathrooms", "N/D"),
                    "garajes": valor.get("garages", "N/D"),
                    "caracteristicas_generales": caracteristicas_generales,
                    "caracteristicas_internas": caracteristicas_internas,
                    "caracteristicas_externas": caracteristicas_externas,
                    "caracteristicas_texto": caracteristicas_texto,
                    "captador_wasi": captador or "Asesor Mettryc",
                    "telefono_captador_wasi": usuario.get("phone", ""),
                    "estado_activo": True,
                    "enlace": f"https://www.mettryc.com/inmueble/{property_id}",
                }
            )

        if cantidad_pagina < take:
            break

        skip += take
        await asyncio.sleep(0.25)

    logger.info(f"Inventario Wasi cargado exitosamente. Propiedades: {len(propiedades)}")
    return propiedades


async def actualizar_inventario(force: bool = False) -> bool:
    if not force and not inventario_necesita_actualizacion():
        return False

    async with inventory_refresh_lock:
        if not force and not inventario_necesita_actualizacion():
            return False

        propiedades = await obtener_inventario_wasi()

        if not propiedades:
            logger.error("Wasi no devolvió propiedades. Se conserva el inventario anterior.")
            return False

        inventory_cache["inventario"] = propiedades
        inventory_cache["ultima_actualizacion"] = datetime.utcnow()
        reconstruir_catalogo_geografico()
        return True


# ============================================================
# CATÁLOGO GEOGRÁFICO DINÁMICO
# ============================================================

FALLBACK_ZONAS_AMBIGUAS = {
    "el trigal": ["Valencia", "Cabudare"],
    "trigal": ["Valencia", "Cabudare"],
    "trigal norte": ["Valencia", "Cabudare"],
    "el trigal norte": ["Valencia", "Cabudare"],
    "los caobos": ["Valencia", "Caracas"],
    "centro": ["Valencia", "Barquisimeto", "Caracas"],
}


def reconstruir_catalogo_geografico() -> None:
    catalogo_geografico["ciudades_norm"] = {}
    catalogo_geografico["zonas_norm"] = {}
    catalogo_geografico["zonas_por_ciudad"] = {}

    for propiedad in inventory_cache.get("inventario", []):
        ciudad_original = str(propiedad.get("ciudad") or "").strip()
        zona_original = str(propiedad.get("zona") or "").strip()

        if ciudad_original and ciudad_original != "N/D":
            ciudad_norm = normalizar_texto(ciudad_original)
            if ciudad_norm and ciudad_norm not in catalogo_geografico["ciudades_norm"]:
                catalogo_geografico["ciudades_norm"][ciudad_norm] = ciudad_original

        if zona_original and zona_original != "N/D":
            zonas = [
                valor.strip()
                for valor in re.split(r"[\/|·\-–,]", zona_original)
                if valor.strip()
            ]

            ciudad_norm = normalizar_texto(ciudad_original)

            for zona in zonas:
                zona_norm = normalizar_texto(zona)
                if not zona_norm or len(zona_norm) < 3:
                    continue

                catalogo_geografico["zonas_norm"].setdefault(zona_norm, set()).add(zona)

                if ciudad_norm:
                    ciudad_nombre = catalogo_geografico["ciudades_norm"].get(
                        ciudad_norm, ciudad_original or "N/D"
                    )
                    catalogo_geografico["zonas_por_ciudad"].setdefault(
                        zona_norm, set()
                    ).add(ciudad_nombre)

    catalogo_geografico["frases_ciudades"] = sorted(
        catalogo_geografico["ciudades_norm"].items(),
        key=lambda item: len(item[0]),
        reverse=True,
    )

    catalogo_geografico["frases_zonas"] = []
    for zona_norm, nombres in catalogo_geografico["zonas_norm"].items():
        preferido = sorted(nombres, key=len)[0]
        catalogo_geografico["frases_zonas"].append((zona_norm, preferido))

    catalogo_geografico["frases_zonas"].sort(
        key=lambda item: len(item[0]), reverse=True
    )
    catalogo_geografico["ultima_actualizacion"] = datetime.utcnow()


def obtener_ciudades_para_zona(zona: Optional[str]) -> Set[str]:
    if not zona:
        return set()

    zona_norm = normalizar_texto(zona)
    ciudades: Set[str] = set()

    if zona_norm in catalogo_geografico["zonas_por_ciudad"]:
        ciudades.update(catalogo_geografico["zonas_por_ciudad"][zona_norm])

    tokens_objetivo = tokens_zona(zona_norm)

    if tokens_objetivo:
        for clave, ciudades_clave in catalogo_geografico["zonas_por_ciudad"].items():
            tokens_clave = tokens_zona(clave)
            if tokens_objetivo.issubset(tokens_clave) or tokens_clave.issubset(tokens_objetivo):
                ciudades.update(ciudades_clave)

    if not ciudades and zona_norm in FALLBACK_ZONAS_AMBIGUAS:
        ciudades.update(FALLBACK_ZONAS_AMBIGUAS[zona_norm])

    return ciudades


def obtener_variantes_zona(zona_referencia: Optional[str]) -> List[str]:
    if not zona_referencia:
        return []

    tokens_objetivo = tokens_zona(normalizar_texto(zona_referencia))
    if not tokens_objetivo:
        return []

    variantes: Set[str] = set()
    for zona_norm, nombres in catalogo_geografico.get("zonas_norm", {}).items():
        tokens_catalogo = tokens_zona(zona_norm)
        if not tokens_catalogo:
            continue

        if tokens_objetivo.issubset(tokens_catalogo) or tokens_catalogo.issubset(tokens_objetivo):
            variantes.update(nombres)

    return sorted(variantes)


def detectar_zona_ciudad(texto_normalizado: str) -> Dict[str, Any]:
    if not texto_normalizado:
        return {}

    resultado: Dict[str, Any] = {}

    for ciudad_norm, ciudad_original in catalogo_geografico.get("frases_ciudades", []):
        if contiene_termino(texto_normalizado, ciudad_norm):
            resultado["ciudad"] = ciudad_original
            break

    zona_preferida: Optional[str] = None
    for zona_norm, zona_original in catalogo_geografico.get("frases_zonas", []):
        if contiene_termino(texto_normalizado, zona_norm):
            zona_preferida = zona_original
            break

    def aplicar_ciudades_para_zona(zona_etiqueta: str) -> None:
        ciudades_asociadas = obtener_ciudades_para_zona(zona_etiqueta)
        if not ciudades_asociadas:
            return

        ciudades_ordenadas = sorted(ciudades_asociadas)
        resultado["ciudades_posibles"] = ciudades_ordenadas

        ciudad_norm_actual = normalizar_texto(resultado.get("ciudad") or "")
        ciudades_norm = {normalizar_texto(ciudad) for ciudad in ciudades_asociadas}

        if len(ciudades_asociadas) == 1:
            resultado["ciudad"] = ciudades_ordenadas[0]
            return

        if not ciudad_norm_actual or ciudad_norm_actual not in ciudades_norm:
            resultado["ambiguedad"] = True
            resultado.pop("ciudad", None)
            ciudades_texto = ", ".join(ciudades_ordenadas)
            resultado["mensaje_ambiguedad"] = (
                f"La zona {zona_etiqueta} aparece en varias ciudades: {ciudades_texto}. "
                "¿En cuál de esas ciudades deseas buscar?"
            )

    if zona_preferida:
        resultado["zona"] = zona_preferida
        aplicar_ciudades_para_zona(resultado["zona"])
    else:
        for zona_alias in FALLBACK_ZONAS_AMBIGUAS:
            zona_alias_norm = normalizar_texto(zona_alias)
            if contiene_termino(texto_normalizado, zona_alias_norm):
                zona_formateada = normalizar_nombre(zona_alias)
                resultado["zona"] = zona_formateada
                aplicar_ciudades_para_zona(resultado["zona"])
                break

    return resultado


# ============================================================
# INTEGRACIÓN GOOGLE SHEETS Y CAPTADORES
# ============================================================

def sheets_necesita_actualizacion() -> bool:
    ultima = sheets_cache.get("ultima_actualizacion")
    if not ultima:
        return True
    return datetime.utcnow() - ultima >= INTERVALO_ACTUALIZACION_SHEETS


def agregar_captador_sheet(resultado: dict, nombre: Any, telefono: Any) -> None:
    nombre_limpio = str(nombre or "").strip()
    telefono_limpio = normalizar_telefono(telefono)
    if nombre_limpio and telefono_limpio:
        resultado[nombre_limpio] = telefono_limpio


def procesar_captadores_sheet(payload: Any) -> Dict[str, str]:
    captadores: Dict[str, str] = {}
    if isinstance(payload, dict):
        for nombre, telefono in payload.items():
            if isinstance(telefono, dict):
                agregar_captador_sheet(
                    captadores,
                    telefono.get("nombre") or nombre,
                    telefono.get("telefono") or telefono.get("phone") or telefono.get("whatsapp"),
                )
            else:
                agregar_captador_sheet(captadores, nombre, telefono)

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
                registro.get("telefono") or registro.get("phone") or registro.get("whatsapp"),
            )

    return captadores


async def sincronizar_google_sheet(force: bool = False) -> bool:
    if not GOOGLE_SHEET_TURNOS_URL:
        logger.warning("GOOGLE_SHEET_TURNOS_URL no configurada.")
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
                raise ValueError("Sheets no devolvió un objeto JSON válido.")

            agentes = payload.get("agentes", [])
            if not isinstance(agentes, list):
                agentes = []

            captadores = procesar_captadores_sheet(payload.get("captadores", {}))
            if not captadores:
                captadores = procesar_captadores_sheet(
                    payload.get("captadores_data", []) or payload.get("asesores", [])
                )

            sheets_cache["agentes"] = agentes
            sheets_cache["captadores"] = captadores
            sheets_cache["ultima_actualizacion"] = datetime.utcnow()

            logger.info(
                f"Sheets sincronizado: agentes={len(agentes)} captadores={len(captadores)}"
            )
            return True

        except Exception as exc:
            logger.error(f"Error cargando Sheets: tipo={type(exc).__name__} detalle={str(exc)[:200]}")
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
    mejor = None
    mejor_score = 0.0

    for nombre_sheet, telefono in captadores.items():
        tokens_sheet = tokens_nombre(nombre_sheet)
        if not tokens_wasi or not tokens_sheet:
            continue

        interseccion = tokens_wasi.intersection(tokens_sheet)
        union = tokens_wasi.union(tokens_sheet)

        score_jaccard = len(interseccion) / len(union) if union else 0
        cobertura = len(interseccion) / len(tokens_wasi) if tokens_wasi else 0
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
        for agente in sheets_cache["agentes"]
        if isinstance(agente, dict) and (agente.get("nombre") or agente.get("name"))
    ]

    if not agentes:
        return None

    global round_robin_index

    async with round_robin_lock:
        round_robin_index = (round_robin_index + 1) % len(agentes)
        agente = agentes[round_robin_index]

    if not agente.get("nombre"):
        agente["nombre"] = agente.get("name")

    logger.info(f"🎯 Agente asignado por Round Robin: {agente.get('nombre')} (índice {round_robin_index})")
    return agente

# ============================================================
# PROMPTS DEL SISTEMA
# ============================================================

PROMPT_MAESTRO = """
Eres Paty, la asesora virtual de Mettryc Realty, la Primera Tecnoinmobiliaria de Venezuela.

Hablas español venezolano de forma cálida, profesional, breve, natural y humana. Nunca parezcas un formulario rígido.

TU FUNCIÓN

Comprender el mensaje utilizando toda la conversación y el estado comercial. Extraes información, identificas la intención y decides qué herramienta o acción debe ejecutar el sistema.

No inventes propiedades, precios, enlaces, códigos, captadores, agentes ni disponibilidad. El sistema se encarga de consultar e imprimir las fichas reales.

REGLAS CONVERSACIONALES

1. Aprovecha cualquier dato dicho anteriormente. Nunca vuelvas a preguntar algo que ya aparece en el estado comercial.
2. El usuario puede dar requisitos en cualquier orden o usar lenguaje informal.
3. Si el usuario hace una pregunta sobre la empresa, respóndela brevemente utilizando la base de conocimiento y retoma naturalmente la conversación.
4. No interrogues. Haz una o máximo dos preguntas cortas relacionadas.
5. No es obligatorio recopilar todos los filtros para buscar.
6. Requisitos mínimos recomendados para buscar:
   - tipo_operacion (venta o alquiler)
   - tipo_propiedad (apartamento, casa, townhouse, etc.)
   - ciudad y/o zona.
7. Habitaciones, baños, garajes y características adicionales son preferencias opcionales.
8. Si el usuario cambia zona, presupuesto, tipo u otra condición, extrae el nuevo valor.
9. Si dice "no importa", "cualquiera", "ninguna", "me da igual" o "no tengo", registra el campo en campos_sin_preferencia.
10. Interpreta números según el contexto de la conversación (presupuesto, código, opción, habitaciones).
11. No repitas saludos en cada mensaje.

CLASIFICACIÓN DE ROL

- "colega_inmobiliario": solo si se identifica explícitamente como asesor, agente, broker, corredor, realtor, o dice que busca para un cliente.
- "cliente": si busca para sí mismo, para su familia o uso propio.
- Para colegas nunca solicites datos personales del cliente del colega.
- Para clientes que muestren interés en agendar visita o contactar, activa la selección de propiedad o captura de lead.

INMUEBLES ESPECÍFICOS Y ANUNCIOS

- Si proporciona código o enlace de Mettryc o Mercado Libre, usa buscar_por_codigo.
- Si menciona haber visto un anuncio pero no proporciona código ni enlace, usa pedir_codigo_inmueble.
- Los precios están expresados en dólares ($).

ACCIONES POSIBLES

- responder: conversar, dar información general o pedir una aclaración.
- mostrar_menu_principal: si el usuario saluda o pide ver las opciones principales.
- buscar_propiedades: ejecutar búsqueda con los filtros actuales o nuevos.
- mostrar_mas_propiedades: mostrar el siguiente grupo de opciones.
- buscar_por_codigo: consultar un inmueble específico por código.
- seleccionar_propiedad: el usuario eligió una opción mostrada.
- pedir_codigo_inmueble: falta el código del anuncio.
- agendar_visita: el cliente desea agendar visita a una propiedad.
- preguntar_sobre_propiedad: el usuario quiere saber un dato específico de una ficha.
- solicitar_humano: el usuario pide hablar con un asesor/humano.
- publicar_propiedad: el usuario quiere vender, alquilar o valorar su inmueble.
- consulta_mettryc: preguntas sobre Mettryc Realty.
- reiniciar_busqueda: comenzar una nueva búsqueda desde cero.

Devuelve únicamente el JSON solicitado.
"""


# ============================================================
# OPENROUTER Y MODELOS LLM
# ============================================================

def limpiar_json_modelo(contenido: str) -> str:
    texto = str(contenido or "").strip()

    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?\s*", "", texto, flags=re.IGNORECASE)
        texto = re.sub(r"\s*```$", "", texto)

    inicio = texto.find("{")
    fin = texto.rfind("}")

    if inicio >= 0 and fin > inicio:
        return texto[inicio : fin + 1]

    return texto


async def llamar_openrouter_json(
    modelo_pydantic: Type[ModeloPydantic],
    mensajes: List[dict],
    temperatura: float = 0.2,
) -> Optional[ModeloPydantic]:
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY no configurada.")
        return None

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://www.mettryc.com",
        "X-Title": "Mettryc Realty Paty",
    }

    modelos = [MODELO_AGENTE_PRINCIPAL, MODELO_AGENTE_RESPALDO]

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
                "max_tokens": 1000,
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

                data = respuesta.json()
                choices = data.get("choices", [{}])
                message = choices[0].get("message", {})
                contenido = message.get("content", "")

                if isinstance(contenido, list):
                    contenido = "".join(
                        elemento.get("text", "")
                        for elemento in contenido
                        if isinstance(elemento, dict)
                    )

                contenido_limpio = limpiar_json_modelo(contenido)
                return modelo_pydantic.model_validate_json(contenido_limpio)

            except (ValidationError, ValueError, httpx.HTTPError) as exc:
                logger.warning(
                    f"OpenRouter error modelo={modelo} formato={response_format.get('type')} "
                    f"tipo={type(exc).__name__}"
                )
            except Exception as exc:
                logger.warning(
                    f"OpenRouter excepcion modelo={modelo} tipo={type(exc).__name__} "
                    f"detalle={str(exc)[:150]}"
                )

    return None


def construir_estado_para_ia(estado: dict) -> dict:
    propiedad_interes = estado.get("propiedad_interes")

    return {
        "rol": estado.get("rol"),
        "confianza_rol": estado.get("confianza_rol"),
        "modo": estado.get("modo"),
        "paso": estado.get("paso"),
        "objetivo": estado.get("objetivo"),
        "origen_consulta": estado.get("origen_consulta"),
        "filtros": estado.get("filtros"),
        "sin_preferencia": estado.get("sin_preferencia"),
        "esperando_codigo": estado.get("esperando_codigo"),
        "ultimo_lote": estado.get("ultimo_lote"),
        "propiedad_interes": (
            {
                "id": propiedad_interes.get("id"),
                "titulo": propiedad_interes.get("titulo"),
                "zona": propiedad_interes.get("zona"),
                "ciudad": propiedad_interes.get("ciudad"),
            }
            if isinstance(propiedad_interes, dict)
            else None
        ),
        "lead_propietario": estado.get("lead_propietario"),
        "lead": {
            "nombre": estado["lead"].get("nombre"),
            "correo": estado["lead"].get("correo"),
            "whatsapp": "disponible" if estado["lead"].get("whatsapp") else None,
            "numero_actual_disponible": bool(estado.get("numero_canal")),
        },
    }


async def decidir_con_ia(mensaje: str, estado: dict) -> DecisionAgente:
    contexto = {
        "estado_comercial": construir_estado_para_ia(estado),
        "mensaje_actual": mensaje,
    }

    mensajes = [
        {"role": "system", "content": PROMPT_MAESTRO},
        *estado.get("historial", [])[-12:],
        {
            "role": "user",
            "content": (
                "Analiza el mensaje actual usando el estado comercial. Devuelve la decisión estructurada.\n\n"
                + json.dumps(contexto, ensure_ascii=False)
            ),
        },
    ]

    decision = await llamar_openrouter_json(DecisionAgente, mensajes, temperatura=0.2)

    if decision:
        return decision

    return decision_fallback(mensaje, estado)


# ============================================================
# FALLBACK DETERMINISTA DE DECISIÓN
# ============================================================

def decision_fallback(mensaje: str, estado: dict) -> DecisionAgente:
    codigo = extraer_codigo_inmueble(mensaje)
    posicion = detectar_posicion(mensaje)
    rol = detectar_rol_explicito(mensaje)

    if codigo:
        return DecisionAgente(
            mensaje="",
            rol=rol,
            confianza_rol=1.0 if rol else 0.0,
            intencion="consulta_inmueble",
            accion=AccionAgente(tipo="buscar_por_codigo", codigo=codigo),
        )

    if pide_mas_opciones(mensaje):
        return DecisionAgente(
            mensaje="",
            rol=rol,
            confianza_rol=1.0 if rol else 0.0,
            intencion="mas_opciones",
            accion=AccionAgente(tipo="mostrar_mas_propiedades"),
        )

    if posicion and estado.get("ultimo_lote"):
        return DecisionAgente(
            mensaje="",
            rol=rol,
            confianza_rol=1.0 if rol else 0.0,
            intencion="interes_propiedad",
            accion=AccionAgente(tipo="seleccionar_propiedad", posicion=posicion),
        )

    if menciona_otro_anuncio(mensaje):
        return DecisionAgente(
            mensaje="Por favor, envíame el código o enlace de la propiedad para buscarla.",
            rol=rol,
            confianza_rol=1.0 if rol else 0.0,
            intencion="anuncio_sin_codigo",
            accion=AccionAgente(tipo="pedir_codigo_inmueble"),
        )

    if es_saludo_sin_intencion(mensaje):
        return DecisionAgente(
            mensaje="",
            rol=rol,
            confianza_rol=0.0,
            intencion="saludo",
            accion=AccionAgente(tipo="mostrar_menu_principal"),
        )

    return DecisionAgente(
        mensaje="¿En qué puedo ayudarte con respecto a tus necesidades inmobiliarias?",
        rol=rol,
        confianza_rol=1.0 if rol else 0.0,
        intencion="conversar",
        accion=AccionAgente(tipo="responder"),
    )


# ============================================================
# HUMANIZACIÓN DE TEXTOS Y BASE DE CONOCIMIENTO IA
# ============================================================

async def humanizar_texto_con_ia(
    estado: dict,
    instruccion_cruda: str,
    mensaje_usuario: str,
) -> str:
    if not OPENROUTER_API_KEY:
        return instruccion_cruda

    prompt_sistema = (
        "Eres Paty, la asistente virtual de Mettryc Realty.\n"
        "Tu sistema interno generó esta pregunta/orden técnica:\n"
        f'"{instruccion_cruda}"\n\n'
        "TAREA:\n"
        "Tradúcela a tu personalidad cálida, venezolana, profesional y breve (máximo 30 palabras).\n"
        "1. Si el usuario dio un dato útil recién, valídalo amablemente.\n"
        "2. Haz ÚNICAMENTE la pregunta indicada, sin agregar otras preguntas ni cambiar el sentido.\n"
        "3. No agregues saludos largos repetitivos."
    )

    mensajes = [
        {"role": "system", "content": prompt_sistema},
        *estado.get("historial", [])[-4:],
        {"role": "user", "content": mensaje_usuario},
    ]

    payload = {
        "model": MODELO_HUMANIZACION,
        "messages": mensajes,
        "max_tokens": 150,
        "temperature": 0.4,
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://www.mettryc.com",
        "X-Title": "Mettryc Realty Humanizacion",
    }

    try:
        resp = await http_client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=15.0,
        )
        resp.raise_for_status()

        data = resp.json()
        contenido = data["choices"][0]["message"]["content"]
        return contenido.strip() if contenido else instruccion_cruda
    except Exception as e:
        logger.error(f"Error humanizando texto con IA: {e}")
        return instruccion_cruda


async def responder_consulta_mettryc_ia(
    estado: dict,
    pregunta: str,
) -> str:
    mensajes = [
        {
            "role": "system",
            "content": (
                "Eres Paty de Mettryc Realty. Responde a la pregunta usando EXCLUSIVAMENTE "
                "la base de conocimiento oficial de Mettryc Realty que se te proporciona en JSON.\n"
                "Sé amable, profesional y concisa. Si la información no está en la base, indica que "
                "con gusto puedes poner al usuario en contacto con un asesor humano para resolver sus dudas."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "base_conocimiento": BASE_CONOCIMIENTO_METTRYC,
                    "pregunta_usuario": pregunta,
                },
                ensure_ascii=False,
            ),
        },
    ]

    resultado = await llamar_openrouter_json(
        RespuestaBaseConocimiento,
        mensajes,
        temperatura=0.2,
    )

    if resultado and resultado.respuesta:
        return resultado.respuesta

    return (
        "En Mettryc Realty combinamos tecnología y asesoría humana para ayudarte en la compra, "
        "venta y alquiler de inmuebles. Para detalles específicos, ¿te gustaría que un asesor te contacte?"
    )


async def responder_pregunta_propiedad_ia(
    estado: dict,
    propiedad: dict,
    pregunta: str,
) -> str:
    propiedad_segura = {
        "id": propiedad.get("id"),
        "titulo": propiedad.get("titulo"),
        "descripcion": propiedad.get("descripcion"),
        "ciudad": propiedad.get("ciudad"),
        "zona": propiedad.get("zona"),
        "precio_venta": propiedad.get("precio_venta"),
        "precio_alquiler": propiedad.get("precio_alquiler"),
        "area": propiedad.get("area"),
        "habitaciones": propiedad.get("habitaciones"),
        "banos": propiedad.get("banos"),
        "garajes": propiedad.get("garajes"),
        "caracteristicas_generales": propiedad.get("caracteristicas_generales", []),
        "caracteristicas_internas": propiedad.get("caracteristicas_internas", []),
        "caracteristicas_externas": propiedad.get("caracteristicas_externas", []),
    }

    mensajes = [
        {
            "role": "system",
            "content": (
                "Eres Paty de Mettryc Realty. El usuario está preguntando algo específico "
                "sobre la propiedad cuyos datos reales de Wasi se adjuntan en JSON.\n"
                "REGLAS ABSOLUTAS:\n"
                "1. Responde ÚNICAMENTE con base en los datos provistos en el objeto propiedad.\n"
                "2. Si la información no aparece expresamente (por ejemplo: acepta mascotas, financiamiento, "
                "planta eléctrica no mencionada, etc.), di claramente que ese dato no está especificado en la ficha "
                "y ofrece ponerlo en contacto con un asesor para confirmarlo con el propietario.\n"
                "3. Nunca inventes ni asumas características que no estén escritas."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "propiedad": propiedad_segura,
                    "pregunta_usuario": pregunta,
                },
                ensure_ascii=False,
            ),
        },
    ]

    resultado = await llamar_openrouter_json(
        RespuestaConsultaPropiedad,
        mensajes,
        temperatura=0.1,
    )

    if resultado and resultado.respuesta:
        return resultado.respuesta

    return (
        "Ese detalle no aparece especificado en la ficha técnica del inmueble. "
        "¿Te gustaría agendar una visita o que un asesor te confirme ese dato?"
    )

# ============================================================
# APLICACIÓN DE DECISIONES DE IA AL ESTADO
# ============================================================

def normalizar_campo_sin_preferencia(campo: str) -> Optional[str]:
    texto = normalizar_texto(campo).replace(" ", "_")

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
        "ciudad": "ciudad",
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


def aplicar_decision(estado: dict, decision: DecisionAgente, mensaje: str) -> bool:
    hubo_cambio_busqueda = False

    rol_explicito = detectar_rol_explicito(mensaje)
    if rol_explicito:
        estado["rol"] = rol_explicito
        estado["confianza_rol"] = 1.0
        estado["pregunta_rol_realizada"] = True
    elif (
        decision.rol
        and decision.confianza_rol >= 0.80
        and (not estado.get("rol") or decision.confianza_rol >= estado.get("confianza_rol", 0.0))
    ):
        estado["rol"] = decision.rol
        estado["confianza_rol"] = decision.confianza_rol
        estado["pregunta_rol_realizada"] = True

    actualizaciones = decision.actualizaciones.model_dump()
    campos_busqueda = [
        "tipo_operacion",
        "tipo_propiedad",
        "ciudad",
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
            valor = normalizar_tipo_propiedad(valor)

        if campo == "caracteristicas":
            valor = list(
                dict.fromkeys(
                    normalizar_texto(elemento)
                    for elemento in valor
                    if normalizar_texto(elemento)
                )
            )

        anterior = estado["filtros"].get(campo)
        if anterior != valor:
            estado["filtros"][campo] = valor
            hubo_cambio_busqueda = True

            if campo in estado["sin_preferencia"]:
                estado["sin_preferencia"].remove(campo)

            if campo == "tipo_operacion":
                estado["operacion_confirmada"] = True

            if campo == "presupuesto_max":
                estado["pregunta_presupuesto_colega_realizada"] = True

    for campo_original in decision.campos_sin_preferencia:
        campo = normalizar_campo_sin_preferencia(campo_original)
        if not campo:
            continue

        if campo not in estado["sin_preferencia"]:
            estado["sin_preferencia"].append(campo)

        nuevo_valor = [] if campo == "caracteristicas" else None
        if estado["filtros"].get(campo) != nuevo_valor:
            estado["filtros"][campo] = nuevo_valor
            hubo_cambio_busqueda = True

        if campo == "presupuesto_max":
            estado["pregunta_presupuesto_colega_realizada"] = True

    lead = estado["lead"]

    nombre = actualizaciones.get("nombre")
    if nombre and nombre_valido(nombre):
        lead["nombre"] = normalizar_nombre(nombre)

    correo = extraer_correo(mensaje) or actualizaciones.get("correo")
    if correo and correo_valido(correo):
        lead["correo"] = correo.lower()

    telefono = extraer_telefono(mensaje) or actualizaciones.get("whatsapp")
    if telefono:
        telefono = normalizar_telefono(telefono)
        if telefono:
            lead["whatsapp"] = telefono
            lead["whatsapp_confirmado"] = True

    if actualizaciones.get("usar_numero_actual") and estado.get("numero_canal"):
        lead["whatsapp"] = estado["numero_canal"]
        lead["whatsapp_confirmado"] = True

    if hubo_cambio_busqueda and estado.get("propiedades_enviadas"):
        estado["propiedades_enviadas"] = []
        estado["ultimo_lote"] = []
        estado["propiedad_interes"] = None
        estado["resultados_cache_key"] = None

    return hubo_cambio_busqueda


# ============================================================
# MOTOR DE BÚSQUEDA Y EVALUACIÓN DE INMUEBLES
# ============================================================

def obtener_precio(propiedad: dict, operacion: str) -> float:
    if "alquiler" in operacion or "renta" in operacion:
        return convertir_float(propiedad.get("precio_alquiler"))
    return convertir_float(propiedad.get("precio_venta"))


def coincide_tipo(propiedad: dict, tipo_buscado: str) -> bool:
    if not tipo_buscado:
        return True

    buscado = normalizar_tipo_propiedad(tipo_buscado)
    tipo_wasi = normalizar_tipo_propiedad(propiedad.get("tipo_propiedad_wasi", ""))
    titulo = normalizar_texto(propiedad.get("titulo", ""))

    if buscado == "casa":
        aceptados = {"casa", "quinta", "townhouse", "apartoquinta", "chalet", "villa"}
        return any(tipo in tipo_wasi or tipo in titulo for tipo in aceptados)

    if buscado == "apartamento":
        return any(tipo in tipo_wasi or tipo in titulo for tipo in ["apartamento", "penthouse", "loft", "apto"])

    return buscado in tipo_wasi or buscado in titulo


def zona_coincide(zona_buscada: Optional[str], zona_propiedad: Optional[str], ciudad_propiedad: Optional[str]) -> bool:
    if not zona_buscada:
        return True

    zona_buscada_norm = normalizar_texto(zona_buscada)
    zona_prop_norm = normalizar_texto(zona_propiedad)
    ciudad_prop_norm = normalizar_texto(ciudad_propiedad)

    if zona_buscada_norm and zona_buscada_norm in f"{zona_prop_norm} {ciudad_prop_norm}".strip():
        return True

    tokens_buscada = tokens_zona(zona_buscada_norm)
    tokens_prop = tokens_zona(zona_prop_norm)
    tokens_ciudad = tokens_zona(ciudad_prop_norm)

    if tokens_ciudad:
        tokens_prop -= tokens_ciudad

    if not tokens_prop:
        tokens_prop = tokens_zona(zona_prop_norm) or tokens_zona(ciudad_prop_norm)

    if not tokens_buscada:
        return True
    if not tokens_prop:
        return False

    if tokens_buscada.issubset(tokens_prop) or tokens_prop.issubset(tokens_buscada):
        return True

    coincidencias = tokens_buscada & tokens_prop
    return bool(coincidencias) and len(coincidencias) >= max(1, len(tokens_buscada) - 1)


def evaluar_propiedad(original: dict, filtros: dict) -> Optional[dict]:
    propiedad = deepcopy(original)
    operacion = filtros.get("tipo_operacion", "venta")
    precio = obtener_precio(propiedad, operacion)

    if precio <= 0:
        return None

    score = 0.0
    diferencias = []
    es_exacta = True

    tipo = filtros.get("tipo_propiedad")
    zona = filtros.get("zona")
    presupuesto = filtros.get("presupuesto_max")
    habitaciones_min = filtros.get("habitaciones_min")
    banos_min = filtros.get("banos_min")
    garajes_min = filtros.get("garajes_min")
    caracteristicas = filtros.get("caracteristicas", [])

    if tipo:
        if coincide_tipo(propiedad, tipo):
            score += 35
        else:
            return None

    if zona:
        if zona_coincide(zona, propiedad.get("zona", ""), propiedad.get("ciudad", "")):
            score += 30
        else:
            return None

    if presupuesto and presupuesto > 0:
        if precio <= presupuesto:
            proporcion = precio / presupuesto
            score += 20 * (1 - max(0, proporcion - 1))
        elif precio <= presupuesto * (1 + MAX_EXCESO_PRESUPUESTO):
            score += 5
            es_exacta = False
            diferencias.append(f"Inversión de {formato_moneda(precio)}")
        else:
            return None

    habitaciones = convertir_entero(propiedad.get("habitaciones"))
    if habitaciones_min is not None and habitaciones_min > 0:
        if habitaciones >= habitaciones_min:
            score += 10
        elif habitaciones == habitaciones_min - 1:
            score += 5
            es_exacta = False
            diferencias.append(f"Tiene {habitaciones} hab.")
        else:
            return None

    banos = convertir_entero(propiedad.get("banos"))
    if banos_min is not None and banos_min > 0:
        if banos >= banos_min:
            score += 5
        elif banos == banos_min - 1:
            score += 2
            es_exacta = False
            diferencias.append(f"Tiene {banos} baños")
        else:
            return None

    garajes = convertir_entero(propiedad.get("garajes"))
    if garajes_min is not None and garajes_min > 0:
        if garajes >= garajes_min:
            score += 5
        elif garajes == garajes_min - 1:
            score += 2
            es_exacta = False
            diferencias.append(f"Tiene {garajes} puestos")
        else:
            return None

    texto_propiedad = normalizar_texto(
        " ".join(
            [
                str(propiedad.get("titulo", "")),
                str(propiedad.get("descripcion", "")),
                str(propiedad.get("caracteristicas_texto", "")),
            ]
        )
    )

    no_confirmadas = []
    for caracteristica in caracteristicas:
        car_norm = normalizar_texto(caracteristica)
        if car_norm and car_norm in texto_propiedad:
            score += 5
        elif car_norm:
            no_confirmadas.append(caracteristica)

    if no_confirmadas:
        diferencias.append("No especifica: " + ", ".join(no_confirmadas))
        es_exacta = False

    propiedad["_score"] = round(score, 2)
    propiedad["_diferencias"] = diferencias
    propiedad["_coincidencia"] = "exacta" if es_exacta and not diferencias else "aproximada"
    propiedad["operacion_buscada"] = operacion
    return propiedad


def buscar_mejores_propiedades(estado: dict, cantidad: int) -> Tuple[List[dict], str]:
    excluir = {str(property_id) for property_id in estado.get("propiedades_enviadas", [])}
    evaluadas = []
    pasaron_zona_tipo = 0

    filtros = estado.get("filtros", {})
    zona_buscada = normalizar_texto(filtros.get("zona", ""))
    tipo_buscado = filtros.get("tipo_propiedad")
    operacion = str(filtros.get("tipo_operacion", "venta")).lower()
    presupuesto_max = convertir_float(filtros.get("presupuesto_max"))
    ciudad_buscada = normalizar_texto(filtros.get("ciudad", ""))

    cache_key = (
        f"{zona_buscada}-{ciudad_buscada}-{tipo_buscado}-{operacion}-"
        f"{presupuesto_max}-{len(excluir)}"
    )

    if estado.get("resultados_cache_key") == cache_key and estado.get("ultimos_resultados"):
        return [], "resultados_ya_enviados"

    Chismoso.log_inventario(
        sender=estado.get("sender") or "DEBUG",
        filtros=filtros,
        total_propiedades=len(inventory_cache["inventario"]),
    )

    for original in inventory_cache["inventario"]:
        property_id = str(original.get("id", ""))
        zona_prop = normalizar_texto(original.get("zona", ""))
        ciudad_prop = normalizar_texto(original.get("ciudad", ""))

        if not property_id or property_id in excluir:
            continue

        tipo_ok = coincide_tipo(original, tipo_buscado) if tipo_buscado else True
        if not tipo_ok:
            continue

        if ciudad_buscada and ciudad_buscada not in ciudad_prop:
            continue

        zona_ok = True
        if zona_buscada:
            zonas_buscadas_usuario = [
                segmento.strip()
                for segmento in (filtros.get("zona") or "").split(",")
                if segmento.strip()
            ] or [filtros.get("zona") or zona_buscada]

            zonas_complementarias: List[str] = []
            for zona_usuario in list(zonas_buscadas_usuario):
                zonas_complementarias.extend(obtener_variantes_zona(zona_usuario))

            if zonas_complementarias:
                zonas_buscadas_usuario.extend(zonas_complementarias)
                zonas_buscadas_usuario = list(dict.fromkeys(zonas_buscadas_usuario))

            zona_ok = any(
                zona_coincide(zona_usuario, original.get("zona", ""), original.get("ciudad", ""))
                for zona_usuario in zonas_buscadas_usuario
            )

            if not zona_ok:
                continue

        precio_aplicable = 0.0
        label_precio_aplicable = "N/D"

        if "venta" in operacion or "compr" in operacion:
            precio_aplicable = convertir_float(original.get("precio_venta", 0))
            label_precio_aplicable = original.get("precio_venta_label", "N/D")
            if precio_aplicable <= 0:
                continue
        elif "alquiler" in operacion or "renta" in operacion or "alquil" in operacion:
            precio_aplicable = convertir_float(original.get("precio_alquiler", 0))
            label_precio_aplicable = original.get("precio_alquiler_label", "N/D")
            if precio_aplicable <= 0:
                continue

        if presupuesto_max and presupuesto_max > 0:
            if precio_aplicable > presupuesto_max * (1 + MAX_EXCESO_PRESUPUESTO):
                continue

        pasaron_zona_tipo += 1
        original_clon = deepcopy(original)
        original_clon.update(
            {
                "precio": label_precio_aplicable,
                "precio_venta_float": convertir_float(original.get("precio_venta", 0)),
                "precio_renta_float": convertir_float(original.get("precio_alquiler", 0)),
                "operacion_buscada": "venta" if "venta" in operacion or "compr" in operacion else "alquiler",
            }
        )

        propiedad = evaluar_propiedad(original_clon, estado["filtros"])
        if propiedad:
            evaluadas.append(propiedad)

    exactas = sorted(
        [p for p in evaluadas if p["_coincidencia"] == "exacta"],
        key=lambda p: p["_score"],
        reverse=True,
    )
    aproximadas = sorted(
        [p for p in evaluadas if p["_coincidencia"] == "aproximada"],
        key=lambda p: p["_score"],
        reverse=True,
    )

    resultado = exactas[:cantidad]
    if len(resultado) < cantidad:
        resultado.extend(aproximadas[: cantidad - len(resultado)])

    estado["resultados_cache_key"] = cache_key
    estado["ultimos_resultados"] = [p["id"] for p in resultado]

    if resultado:
        Chismoso.log_resultados(
            sender=estado.get("sender") or "DEBUG",
            propiedades=resultado,
            total_evaluadas=len(evaluadas),
            exactas=len(exactas),
            aproximadas=len(aproximadas),
        )

    motivo_falla = ""
    if not resultado:
        motivo_falla = "precio_o_caracs" if pasaron_zona_tipo > 0 else "zona_o_tipo"
        Chismoso.log_falla(
            sender=estado.get("sender") or "DEBUG",
            motivo=motivo_falla,
            filtros=filtros,
        )

    return resultado, motivo_falla


def complementar_propiedades(estado: dict, seleccion: List[dict], cantidad: int) -> List[dict]:
    faltantes = cantidad - len(seleccion)
    if faltantes <= 0:
        return seleccion

    filtros = estado.get("filtros", {})
    usados = {str(propiedad.get("id")) for propiedad in seleccion}
    usados.update({str(pid) for pid in estado.get("propiedades_enviadas", [])})

    ciudad_objetivo = normalizar_texto(filtros.get("ciudad", ""))
    tipo_objetivo = filtros.get("tipo_propiedad")
    operacion = filtros.get("tipo_operacion") or "venta"
    presupuesto_max = convertir_float(filtros.get("presupuesto_max"))

    for original in inventory_cache["inventario"]:
        property_id = str(original.get("id", ""))
        if not property_id or property_id in usados:
            continue

        if tipo_objetivo and not coincide_tipo(original, tipo_objetivo):
            continue

        if ciudad_objetivo and ciudad_objetivo != normalizar_texto(original.get("ciudad", "")):
            continue

        precio_referencia = (
            convertir_float(original.get("precio_venta", 0))
            if "venta" in operacion
            else convertir_float(original.get("precio_alquiler", 0))
        )

        if presupuesto_max and presupuesto_max > 0:
            margen_relajado = presupuesto_max * (1 + MAX_EXCESO_COMPLEMENTARIO)
            if precio_referencia <= 0 or precio_referencia > margen_relajado:
                continue

        clon = deepcopy(original)
        clon["operacion_buscada"] = "venta" if "venta" in operacion else "alquiler"
        clon["precio_venta_float"] = convertir_float(original.get("precio_venta", 0))
        clon["precio_renta_float"] = convertir_float(original.get("precio_alquiler", 0))
        clon["_coincidencia"] = "relajada"
        clon["_score"] = 5.0
        clon["_diferencias"] = ["Sugerida para ampliar opciones disponibles"]

        seleccion.append(clon)
        usados.add(property_id)

        if len(seleccion) >= cantidad:
            break

    return seleccion


def buscar_por_codigo(codigo: str) -> Optional[dict]:
    codigo_limpio = re.sub(r"\D", "", str(codigo or ""))
    if not codigo_limpio:
        return None

    for propiedad in inventory_cache["inventario"]:
        if str(propiedad.get("id", "")) == codigo_limpio:
            return deepcopy(propiedad)

    return None


# ============================================================
# FORMATEADOR DE FICHAS Y REDACCIÓN DE RESULTADOS
# ============================================================

async def formatear_ficha(propiedad: dict, es_colega: bool, posicion: Optional[int] = None) -> str:
    operacion = propiedad.get("operacion_buscada", "venta")
    precio = obtener_precio(propiedad, operacion)
    titulo = propiedad.get("titulo", "Propiedad Mettryc")

    if posicion:
        titulo = f"Opción {posicion}: {titulo}"

    area_valor = convertir_float(propiedad.get("area"))
    area_texto = f"{area_valor:,.0f} m²".replace(",", ".") if area_valor > 0 else "N/D"

    lineas = [
        f"*{titulo}*",
        f"📍 {propiedad.get('zona', 'N/D')}, {propiedad.get('ciudad', 'N/D')}",
        f"💰 {formato_moneda(precio)}",
        f"📐 {area_texto} | 🛏️ {propiedad.get('habitaciones', 'N/D')} | 🛁 {propiedad.get('banos', 'N/D')} | 🚗 {propiedad.get('garajes', 'N/D')}",
        f"🔗 {propiedad.get('enlace', '')}",
    ]

    diferencias = propiedad.get("_diferencias", [])
    if diferencias:
        lineas.append("ℹ️ *Consideraciones:* " + "; ".join(diferencias[:2]))

    if es_colega:
        captador_wasi = propiedad.get("captador_wasi", "Asesor Mettryc")
        cruce = cruzar_captador_con_sheet(captador_wasi)
        telefono = cruce.get("telefono")

        lineas.append(f"👤 *Captador:* {cruce.get('nombre') or captador_wasi}")
        if telefono:
            lineas.append(f"📲 *WhatsApp captador:* https://wa.me/{telefono}")
        else:
            lineas.append("📲 *WhatsApp captador:* No localizado en el directorio.")

    return "\n".join(lineas)


async def redactar_resultado_ia(estado: dict, cantidad: int, aproximadas: int, especifica: bool = False) -> TextoIntroduccion:
    rol = estado.get("rol") or "cliente"
    instrucciones = {
        "rol": rol,
        "cantidad": cantidad,
        "aproximadas": aproximadas,
        "propiedad_especifica": especifica,
        "reglas": [
            "Redacta ÚNICAMENTE una frase de introducción breve y natural.",
            "No incluyas precios, direcciones, códigos ni enlaces.",
            "No incluyas preguntas de cierre (las opciones las imprimirá el sistema).",
        ],
    }

    mensajes = [
        {
            "role": "system",
            "content": (
                "Eres Paty de Mettryc Realty. Redacta la frase de introducción corta para "
                "presentar las fichas encontradas. Devuelve exclusivamente el JSON solicitado."
            ),
        },
        {"role": "user", "content": json.dumps(instrucciones, ensure_ascii=False)},
    ]

    resultado = await llamar_openrouter_json(TextoIntroduccion, mensajes, temperatura=0.4)
    if resultado and resultado.introduccion:
        return resultado

    if especifica:
        introduccion = "Claro, aquí tienes la información de la propiedad que consultaste:"
    elif aproximadas:
        introduccion = "Encontré estas opciones que pueden interesarte:"
    else:
        introduccion = "Encontré estas excelentes opciones disponibles en nuestro inventario:"

    return TextoIntroduccion(introduccion=introduccion)


async def construir_respuesta_fichas(estado: dict, propiedades: List[dict], especifica: bool = False) -> str:
    es_colega = estado.get("rol") == "colega_inmobiliario"
    if es_colega:
        await sincronizar_google_sheet()

    aproximadas = sum(1 for propiedad in propiedades if propiedad.get("_coincidencia") == "aproximada")
    textos = await redactar_resultado_ia(
        estado,
        cantidad=len(propiedades),
        aproximadas=aproximadas,
        especifica=especifica,
    )

    fichas = []
    for indice, propiedad in enumerate(propiedades, start=1):
        fichas.append(
            await formatear_ficha(
                propiedad,
                es_colega,
                indice if (not especifica and len(propiedades) > 1) else None,
            )
        )

    bloque_fichas = "\n\n".join([textos.introduccion.strip(), *fichas])

    if es_colega:
        cierre = guardar_pregunta_activa(
            estado,
            PREGUNTA_ACCION_RESULTADOS_COLEGA,
            paso="esperando_accion_resultados",
        )
    elif len(propiedades) == 1:
        cierre = guardar_pregunta_activa(
            estado,
            PREGUNTA_ACCION_FICHA_UNICA,
            paso="esperando_accion_resultados",
        )
    else:
        cierre = guardar_pregunta_activa(
            estado,
            PREGUNTA_ACCION_RESULTADOS_CLIENTE,
            paso="esperando_accion_resultados",
        )

    return f"{bloque_fichas}\n\n{cierre}"

# ============================================================
# CAPTURA Y VALIDACIÓN DE DATOS DE LEADS
# ============================================================

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
        faltantes.append("confirmación del número de WhatsApp")

    return faltantes


def formatear_campos_para_respuesta(campos: List[str]) -> str:
    etiquetas = [CAMPO_ACK_LABELS.get(campo, campo) for campo in campos if campo]
    if not etiquetas:
        return ""
    if len(etiquetas) == 1:
        return etiquetas[0]
    return ", ".join(etiquetas[:-1]) + " y " + etiquetas[-1]


def preparar_lista_datos_lead(faltantes: List[str]) -> str:
    lineas = []
    for indice, campo in enumerate(faltantes, 1):
        descripcion = CAMPO_INSTRUCCIONES.get(campo, campo.title())
        lineas.append(f"{indice}. {descripcion}")
    return "\n".join(lineas)


def mensaje_solicitud_datos_lead(
    faltantes: List[str],
    actualizados: Optional[List[str]] = None,
    saludo: bool = False,
) -> str:
    actualizados = list(dict.fromkeys(actualizados or []))
    partes = []

    if saludo:
        partes.append(
            "¡Excelente! Para que un asesor te contacte y agendar tu solicitud, "
            "necesito recopilar estos datos de contacto:"
        )
    elif actualizados:
        partes.append(f"¡Gracias! Registré {formatear_campos_para_respuesta(actualizados)}.")
        if faltantes:
            partes.append("Para completar, necesito lo siguiente:")
    else:
        partes.append("Para asignarte a un asesor, necesito la siguiente información:")

    if faltantes:
        partes.append(preparar_lista_datos_lead(faltantes))
        partes.append(
            "Puedes enviarme los datos en un solo mensaje o por separado "
            "(ejemplo: María Pérez / maria@gmail.com / +584121234567)."
        )
    else:
        partes.append("Si tus datos están correctos, responde “sí” para asignarte al asesor de turno.")

    return "\n\n".join(parte.strip() for parte in partes if parte).strip()


def resumen_datos_lead(estado: dict) -> str:
    lead = estado.get("lead", {})
    whatsapp = formatear_whatsapp(lead.get("whatsapp"))

    lineas = [
        f"- Nombre: {lead.get('nombre') or 'N/D'}",
        f"- Correo: {lead.get('correo') or 'N/D'}",
        f"- WhatsApp: {whatsapp}",
    ]

    if estado.get("motivo_lead") == "publicar_propiedad":
        propietario = estado.get("lead_propietario", {})
        lineas.append(
            f"- Operación deseada: {propietario.get('tipo_operacion') or 'Venta/Alquiler'}"
        )
        if propietario.get("ubicacion"):
            lineas.append(f"- Ubicación inmueble: {propietario.get('ubicacion')}")
        if propietario.get("precio_deseado"):
            lineas.append(
                f"- Precio estimado: {formato_moneda(propietario.get('precio_deseado'))}"
            )

    return "\n".join(lineas)


def mensaje_confirmacion_lead(estado: dict) -> str:
    return (
        "✔️ *Por favor confirma tus datos:*\n\n"
        f"{resumen_datos_lead(estado)}\n\n"
        "¿Es correcta esta información? (Responde *Sí* o *No*)"
    )


def construir_mensaje_errores_lead(errores: List[str]) -> str:
    mensajes: List[str] = []
    if "nombre completo" in errores:
        mensajes.append("Por favor escribe tu nombre y apellido completos.")
    if "correo electrónico" in errores:
        mensajes.append("El correo no parece válido. Usa el formato usuario@dominio.com.")
    if "confirmación del número de WhatsApp" in errores:
        mensajes.append("Incluye el código de país en tu WhatsApp (ejemplo: +584121234567).")
    return " ".join(mensajes).strip()


def actualizar_lead_desde_mensaje(
    estado: dict, mensaje: str
) -> Tuple[List[str], List[str]]:
    lead = estado["lead"]
    actualizados: List[str] = []
    errores: List[str] = []

    if not mensaje:
        return actualizados, errores

    correo, correo_original = extraer_correo_detallado(mensaje)
    if correo_original:
        if correo_valido(correo):
            if lead.get("correo") != correo:
                lead["correo"] = correo
                actualizados.append("correo electrónico")
        else:
            errores.append("correo electrónico")

    telefono, telefono_original = extraer_telefono_detallado(mensaje)
    if telefono_original:
        if telefono:
            if lead.get("whatsapp") != telefono:
                lead["whatsapp"] = telefono
                actualizados.append("confirmación del número de WhatsApp")
            lead["whatsapp_confirmado"] = True
        else:
            errores.append("confirmación del número de WhatsApp")

    texto_norm = normalizar_texto(mensaje)
    if (
        not lead.get("whatsapp")
        and estado.get("numero_canal")
        and any(
            frase in texto_norm
            for frase in [
                "mismo numero",
                "mismo numero del chat",
                "numero del chat",
                "numero actual",
                "este numero",
                "este mismo",
            ]
        )
    ):
        lead["whatsapp"] = estado["numero_canal"]
        lead["whatsapp_confirmado"] = True
        actualizados.append("confirmación del número de WhatsApp")

    if not nombre_valido(lead.get("nombre")):
        texto_para_nombre = mensaje
        for valor in filter(None, [correo_original, telefono_original]):
            texto_para_nombre = texto_para_nombre.replace(valor, " ")

        coincidencia = re.search(
            r"(?:mi\s+nombre\s+es|me\s+llamo|soy)\s+([A-Za-zÀ-ÖØ-öø-ÿ' ]{3,})",
            mensaje or "",
            flags=re.IGNORECASE,
        )

        nombre_bruto = coincidencia.group(1) if coincidencia else None

        if not nombre_bruto:
            texto_filtrado = re.sub(
                r"[^A-Za-zÀ-ÖØ-öø-ÿ' ]", " ", texto_para_nombre
            )
            texto_filtrado = re.sub(r"\s+", " ", texto_filtrado).strip()
            if texto_filtrado.count(" ") >= 1:
                nombre_bruto = texto_filtrado

        if nombre_bruto and nombre_valido(nombre_bruto):
            lead["nombre"] = normalizar_nombre(nombre_bruto)
            actualizados.append("nombre completo")

    if lead.get("whatsapp"):
        lead["whatsapp_confirmado"] = True

    actualizados = list(dict.fromkeys(actualizados))
    errores = list(dict.fromkeys(errores))
    return actualizados, errores


# ============================================================
# NOTIFICACIONES TELEGRAM SEPARADAS (CLIENTES VS COLEGAS)
# ============================================================

async def enviar_telegram(chat_id: str, mensaje: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False

    try:
        respuesta = await http_client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
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
            f"Error Telegram chat={str(chat_id)[-4:]} tipo={type(exc).__name__}"
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
        "habitaciones_min": "Habitaciones mín.",
        "banos_min": "Baños mín.",
        "garajes_min": "Puestos mín.",
    }

    for campo, etiqueta in etiquetas.items():
        valor = filtros.get(campo)
        if valor not in [None, "", []]:
            lineas.append(f"- {etiqueta}: {valor}")

    if filtros.get("presupuesto_max"):
        lineas.append("- Presupuesto máx: " + formato_moneda(filtros["presupuesto_max"]))

    if filtros.get("caracteristicas"):
        lineas.append("- Características: " + ", ".join(filtros["caracteristicas"]))

    return "\n".join(lineas) or "- Sin filtros definidos"


async def notificar_lead_cliente(estado: dict) -> bool:
    lead = estado["lead"]
    propiedad = estado.get("propiedad_interes")
    agente = estado.get("agente_asignado")
    motivo = estado.get("motivo_lead", "interes_propiedad")

    propiedad_texto = "No especificada"
    if propiedad and isinstance(propiedad, dict):
        propiedad_texto = (
            f"{propiedad.get('titulo')}\n"
            f"ID: {propiedad.get('id')}\n"
            f"Precio: {formato_moneda(obtener_precio(propiedad, propiedad.get('operacion_buscada', 'venta')))}\n"
            f"Ubicación: {propiedad.get('zona')}, {propiedad.get('ciudad')}\n"
            f"Link: {propiedad.get('enlace')}"
        )

    whatsapp = normalizar_telefono(lead.get("whatsapp"))
    contacto_link = f"https://wa.me/{whatsapp}" if whatsapp else "N/D"

    titulo_encabezado = "🏠 NUEVO LEAD CLIENTE (BUSCA PROPIEDAD)"
    if motivo == "publicar_propiedad":
        titulo_encabezado = "🏢 NUEVO LEAD PROPIETARIO (CAPTACIÓN)"

    propietario_texto = ""
    if motivo == "publicar_propiedad":
        p_info = estado.get("lead_propietario", {})
        propietario_texto = (
            "\n📋 DATOS PROPIETARIO:\n"
            f"- Tipo Operación: {p_info.get('tipo_operacion') or 'N/D'}\n"
            f"- Ubicación: {p_info.get('ubicacion') or 'N/D'}\n"
            f"- Precio estimado: {formato_moneda(p_info.get('precio_deseado'))}\n"
        )

    mensaje = (
        f"{titulo_encabezado}\n\n"
        f"ID Lead: {estado.get('lead_id')}\n"
        f"Nombre: {lead.get('nombre')}\n"
        f"Correo: {lead.get('correo')}\n"
        f"WhatsApp: {whatsapp or 'N/D'}\n"
        f"Contacto directo: {contacto_link}\n"
        f"{propietario_texto}\n"
        "📋 CRITERIOS DE BÚSQUEDA:\n"
        f"{resumen_filtros(estado)}\n\n"
        "⭐ PROPIEDAD DE INTERÉS:\n"
        f"{propiedad_texto}\n\n"
        "👤 AGENTE ASIGNADO EN TURNO:\n"
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

    resultados = []
    for destino in destinos:
        resultados.append(await enviar_telegram(destino, mensaje))

    return any(resultados)


async def notificar_solicitud_colega_admin(
    estado: dict, mensaje_usuario: str
) -> bool:
    sender_tel = normalizar_telefono(
        estado.get("sender") or estado.get("numero_canal")
    )
    contacto_link = f"https://wa.me/{sender_tel}" if sender_tel else "N/D"

    mensaje = (
        "🤝 SOLICITUD DE APOYO DE COLEGA INMOBILIARIO\n\n"
        f"WhatsApp Colega: {formatear_whatsapp(sender_tel)}\n"
        f"Contacto directo: {contacto_link}\n"
        f"Mensaje del colega: \"{mensaje_usuario}\"\n\n"
        "📋 BÚSQUEDA DEL COLEGA:\n"
        f"{resumen_filtros(estado)}\n\n"
        "⚠️ NOTA: Esta solicitud fue enviada ÚNICAMENTE a Administradores. "
        "No se asignó ningún agente en turno."
    )

    destinos = set(TELEGRAM_ADMIN_IDS)
    resultados = []

    for destino in destinos:
        resultados.append(await enviar_telegram(destino, mensaje))

    return any(resultados)


# ============================================================
# ASIGNACIÓN DE LEAD Y CORTESÍA FINAL
# ============================================================

async def completar_y_asignar_lead(estado: dict) -> str:
    estado["lead_confirmacion_pendiente"] = False
    estado["lead_confirmado"] = True

    if not estado.get("lead_id"):
        estado["lead_id"] = str(uuid.uuid4())

    if not estado.get("agente_asignado"):
        estado["agente_asignado"] = await asignar_agente_round_robin()

    if not estado.get("notificacion_enviada"):
        estado["notificacion_enviada"] = await notificar_lead_cliente(estado)

    estado["objetivo"] = "lead_asignado"
    limpiar_pregunta_activa(estado)

    agente = estado.get("agente_asignado")
    nombre_cliente = estado["lead"].get("nombre", "estimado cliente")

    if estado.get("motivo_lead") == "publicar_propiedad":
        if agente:
            return (
                f"¡Excelente, {nombre_cliente}! He registrado la información de tu inmueble. "
                f"Nuestro agente de turno, *{agente.get('nombre')}*, te contactará por WhatsApp "
                "para coordinar la valoración y promoción de tu propiedad."
            )
        return (
            f"¡Excelente, {nombre_cliente}! He registrado tu inmueble. Un asesor de Mettryc Realty "
            "te contactará muy pronto por WhatsApp para iniciar la promoción."
        )

    if agente:
        return (
            f"¡Listo, {nombre_cliente}! Registrar tu solicitud fue un éxito. "
            f"Nuestro asesor *{agente.get('nombre')}* ha recibido tus datos "
            "y te escribirá directamente por WhatsApp para coordinar los siguientes pasos."
        )

    return (
        f"¡Listo, {nombre_cliente}! He registrado tus datos. El equipo de Mettryc Realty "
        "te contactará por WhatsApp a la brevedad."
    )

# ============================================================
# FLUJO ESPECIAL: MERCADO LIBRE
# ============================================================

async def procesar_flujo_mercadolibre(
    estado: dict,
    mensaje: str,
    codigo_ml: Optional[str] = None,
) -> str:
    codigo = codigo_ml or extraer_codigo_mercadolibre(mensaje)

    if not codigo:
        codigo = estado.get("codigo_anuncio_pendiente")

    if codigo:
        propiedad = buscar_por_codigo(codigo)

        if propiedad:
            estado["propiedad_contexto"] = propiedad
            estado["propiedad_interes"] = propiedad
            estado["ultimo_lote"] = [str(propiedad["id"])]
            estado["esperando_codigo"] = False
            estado["modo"] = "anuncio"

            return await construir_respuesta_fichas(
                estado,
                [propiedad],
                especifica=True,
            )

    estado["esperando_codigo"] = True
    estado["paso"] = "esperando_codigo_anuncio"
    estado["modo"] = "anuncio"

    return (
        "Gracias por contactarnos sobre la propiedad que viste en Mercado Libre. "
        "Por favor, envíame el código que aparece al final del título o la descripción "
        "(ejemplo: MLV-123456789) para mostrarte la ficha completa."
    )


# ============================================================
# FLUJO ESPECIAL: OTROS ANUNCIOS SIN CÓDIGO DIRECTO
# ============================================================

async def procesar_flujo_otro_anuncio(
    estado: dict,
    mensaje: str,
    codigo_detectado: Optional[str] = None,
) -> str:
    codigo = codigo_detectado or extraer_codigo_inmueble(mensaje)

    if codigo:
        propiedad = buscar_por_codigo(codigo)

        if propiedad:
            estado["propiedad_contexto"] = propiedad
            estado["propiedad_interes"] = propiedad
            estado["ultimo_lote"] = [str(propiedad["id"])]
            estado["esperando_codigo"] = False
            estado["modo"] = "anuncio"

            return await construir_respuesta_fichas(
                estado,
                [propiedad],
                especifica=True,
            )

        return (
            f"No encontré una propiedad activa con el código *{codigo}*. "
            "Por favor verifica el número o envíame el enlace directo del anuncio."
        )

    estado["esperando_codigo"] = True
    estado["paso"] = "esperando_codigo_anuncio"
    estado["modo"] = "anuncio"

    return (
        "¡Con gusto te doy información del anuncio! Por favor envíame el código "
        "del inmueble (aparece al final del título o en la descripción) "
        "o el enlace de la publicación para mostrarte la ficha oficial."
    )


# ============================================================
# FLUJO ESPECIAL: PROPIETARIOS (CAPTACIÓN DE PROPIEDADES)
# ============================================================

async def procesar_flujo_propietario(
    estado: dict,
    mensaje: str,
) -> str:
    estado["modo"] = "propietario"
    p_info = estado.setdefault("lead_propietario", {})

    pregunta_activa = obtener_pregunta_activa(estado)

    if pregunta_activa and pregunta_activa.id == "operacion_propietario":
        res_op = resolver_opcion(mensaje, pregunta_activa)
        if res_op.entendida:
            p_info["tipo_operacion"] = res_op.opcion_id
            limpiar_pregunta_activa(estado)

    if not p_info.get("tipo_operacion"):
        op_detectada = detectar_operacion_propietario(mensaje)
        if op_detectada:
            p_info["tipo_operacion"] = op_detectada

    if not p_info.get("tipo_operacion"):
        return guardar_pregunta_activa(
            estado,
            PREGUNTA_OPERACION_PROPIETARIO,
            paso="esperando_operacion_propietario",
        )

    if not p_info.get("ubicacion"):
        zona_ciudad = detectar_zona_ciudad(normalizar_texto(mensaje))
        if zona_ciudad.get("zona") or zona_ciudad.get("ciudad"):
            ciudad = zona_ciudad.get("ciudad", "")
            zona = zona_ciudad.get("zona", "")
            p_info["ubicacion"] = f"{zona}, {ciudad}".strip(" ,")
        elif len(mensaje.split()) >= 2 and not p_info.get("tipo_operacion_confirmada"):
            p_info["tipo_operacion_confirmada"] = True
        elif p_info.get("tipo_operacion_confirmada"):
            p_info["ubicacion"] = mensaje.strip()

    if not p_info.get("ubicacion"):
        return await humanizar_texto_con_ia(
            estado,
            "¿En qué ciudad y zona/urbanización se encuentra ubicado tu inmueble?",
            mensaje,
        )

    if not p_info.get("precio_deseado"):
        precio = detectar_presupuesto(mensaje, permitir_numero_solo=True)
        if precio > 0:
            p_info["precio_deseado"] = precio

    if not p_info.get("precio_deseado"):
        return await humanizar_texto_con_ia(
            estado,
            "¿Cuál es el precio aproximado o canon estimado que tienes pensado para tu propiedad?",
            mensaje,
        )

    estado["motivo_lead"] = "publicar_propiedad"
    estado["objetivo"] = "captura_lead"
    estado["paso"] = "capturando_lead"

    faltantes = datos_lead_faltantes(estado)
    return mensaje_solicitud_datos_lead(faltantes, saludo=True)


# ============================================================
# FLUJO ESPECIAL: COLEGA INMOBILIARIO
# ============================================================

async def procesar_flujo_colega(
    estado: dict,
    mensaje: str,
) -> str:
    estado["rol"] = "colega_inmobiliario"
    estado["confianza_rol"] = 1.0
    estado["pregunta_rol_realizada"] = True
    estado["modo"] = "busqueda"

    filtros = estado.setdefault("filtros", {})

    op = detectar_operacion(mensaje)
    if op:
        filtros["tipo_operacion"] = op
        estado["operacion_confirmada"] = True

    tipo = detectar_tipo_propiedad(mensaje)
    if tipo:
        filtros["tipo_propiedad"] = tipo

    zc = detectar_zona_ciudad(normalizar_texto(mensaje))
    if zc.get("ciudad"):
        filtros["ciudad"] = zc["ciudad"]
    if zc.get("zona"):
        filtros["zona"] = zc["zona"]

    presupuesto = detectar_presupuesto(mensaje)
    if presupuesto > 0:
        filtros["presupuesto_max"] = presupuesto

    if criterios_suficientes(estado):
        return await mostrar_propiedades(estado)

    if not filtros.get("tipo_operacion"):
        return guardar_pregunta_activa(
            estado,
            PREGUNTA_OPERACION_CLIENTE,
            paso="esperando_operacion_colega",
        )

    if not filtros.get("tipo_propiedad"):
        return guardar_pregunta_activa(
            estado,
            PREGUNTA_TIPO_PROPIEDAD,
            paso="esperando_tipo_colega",
        )

    if not filtros.get("ciudad"):
        return "¿En qué ciudad o zona estás buscando para tu cliente?"

    return await mostrar_propiedades(estado)


# ============================================================
# FLUJO ESPECIAL: PREGUNTAS BASE DE CONOCIMIENTO METTRYC
# ============================================================

async def procesar_flujo_mettryc(
    estado: dict,
    mensaje: str,
) -> str:
    pregunta_activa = obtener_pregunta_activa(estado)

    if pregunta_activa and pregunta_activa.id == "categorias_mettryc":
        res_op = resolver_opcion(mensaje, pregunta_activa)

        if res_op.entendida:
            limpiar_pregunta_activa(estado)

            if res_op.opcion_id == "menu_principal":
                return guardar_pregunta_activa(
                    estado,
                    PREGUNTA_MENU_PRINCIPAL,
                    paso="esperando_menu_principal",
                )

            if res_op.opcion_id == "oficinas":
                oficinas = BASE_CONOCIMIENTO_METTRYC["oficinas"]
                lineas = ["📍 *Nuestras oficinas Mettryc Realty:*\n"]
                for ofi in oficinas:
                    lineas.append(
                        f"• *{ofi['ciudad']}*: {ofi['direccion']}\n  Maps: {ofi['ubicacion_maps']}"
                    )
                lineas.append(
                    "\n¿Deseas consultar algo más o volver al menú principal?"
                )
                return "\n".join(lineas)

            if res_op.opcion_id == "honorarios":
                hon = BASE_CONOCIMIENTO_METTRYC["honorarios"]
                return (
                    "💼 *Honorarios Profesionales Mettryc Realty:*\n\n"
                    f"• *Venta:* {hon['venta']}\n"
                    f"• *Alquiler:* {hon['alquiler']}\n\n"
                    f"_{hon['nota_negociabilidad']}_\n\n"
                    "¿Deseas realizar otra consulta?"
                )

            if res_op.opcion_id == "reclutamiento":
                rec = BASE_CONOCIMIENTO_METTRYC["reclutamiento"]
                return (
                    "🚀 *Únete al equipo Mettryc Realty:*\n\n"
                    f"• *Inversión inicial:* {rec['inversion_inicial']}\n"
                    f"• *Formulario de ingreso:* {rec['formulario']}\n\n"
                    f"{rec['mensaje_estatus']}"
                )

            if res_op.opcion_id == "contacto":
                cnt = BASE_CONOCIMIENTO_METTRYC["contacto"]
                return (
                    "🌐 *Contacto Mettryc Realty:*\n\n"
                    f"• *Sitio Web:* {cnt['sitio_web']}\n"
                    f"• *Correo:* {cnt['correo_general']}\n"
                    f"• *Redes Sociales:* {cnt['redes_sociales']}"
                )

    respuesta_ia = await responder_consulta_mettryc_ia(estado, mensaje)

    pregunta_menu = guardar_pregunta_activa(
        estado,
        PREGUNTA_CATEGORIAS_METTRYC,
        paso="esperando_categoria_mettryc",
    )

    return f"{respuesta_ia}\n\n{pregunta_menu}"

# ============================================================
# SOLICITUD DIRECTA DE ATENCIÓN HUMANA
# ============================================================

async def procesar_solicitud_humano(
    sender: str,
    mensaje: str,
    estado: dict,
) -> str:
    rol = estado.get("rol") or "cliente"

    if rol == "colega_inmobiliario":
        await notificar_solicitud_colega_admin(estado, mensaje)
        return (
            "Entendido, colega. He notificado directamente al equipo administrativo "
            "de Mettryc Realty sobre tu solicitud. Un representante se comunicará contigo.\n\n"
            "¿Deseas realizar alguna otra consulta mientras tanto?"
        )

    estado["objetivo"] = "captura_lead"
    estado["motivo_lead"] = "solicitud_humano"
    estado["paso"] = "capturando_lead"

    faltantes = datos_lead_faltantes(estado)

    if not faltantes:
        estado["lead_confirmacion_pendiente"] = True
        return mensaje_confirmacion_lead(estado)

    return (
        "¡Con mucho gusto te conecto con un asesor humano! "
        "Para que la persona asignada te contacte, por favor facilitame los siguientes datos:\n\n"
        f"{mensaje_solicitud_datos_lead(faltantes)}"
    )


# ============================================================
# MÁQUINA DE ESTADOS ACTIVA (RESPUESTAS A PREGUNTAS ESTRUCTURADAS)
# ============================================================

async def procesar_estado_activo(
    sender: str,
    mensaje: str,
    estado: dict,
) -> Optional[str]:
    paso = estado.get("paso")
    pregunta_activa = obtener_pregunta_activa(estado)

    # --- 1. Confirmación de Lead ---
    if estado.get("lead_confirmacion_pendiente"):
        if es_respuesta_afirmativa(mensaje):
            return await completar_y_asignar_lead(estado)

        if es_respuesta_negativa(mensaje):
            estado["lead_confirmacion_pendiente"] = False
            estado["lead_confirmado"] = False
            return (
                "Entendido. Por favor indícame qué dato deseas corregir "
                "(Nombre, Correo o WhatsApp) y lo actualizo de inmediato."
            )

        actualizados, errores = actualizar_lead_desde_mensaje(estado, mensaje)
        if actualizados:
            return mensaje_confirmacion_lead(estado)
        if errores:
            return construir_mensaje_errores_lead(errores)

        return (
            "Por favor responde *Sí* si tus datos son correctos para asignarte "
            "el asesor, o dime qué dato deseas modificar."
        )

    # --- 2. Captura de Lead Activa ---
    if estado.get("objetivo") == "captura_lead" and paso == "capturando_lead":
        actualizados, errores = actualizar_lead_desde_mensaje(estado, mensaje)

        if errores:
            return construir_mensaje_errores_lead(errores)

        faltantes = datos_lead_faltantes(estado)
        if not faltantes:
            estado["lead_confirmacion_pendiente"] = True
            return mensaje_confirmacion_lead(estado)

        return mensaje_solicitud_datos_lead(
            faltantes,
            actualizados=actualizados,
        )

    if not pregunta_activa:
        return None

    # --- 3. Menú Principal ---
    if pregunta_activa.id == "menu_principal":
        res_op = resolver_opcion(mensaje, pregunta_activa)
        if res_op.entendida:
            limpiar_pregunta_activa(estado)

            if res_op.opcion_id == "comprar":
                estado["filtros"]["tipo_operacion"] = "venta"
                estado["operacion_confirmada"] = True
                estado["modo"] = "busqueda"
                return guardar_pregunta_activa(
                    estado,
                    PREGUNTA_TIPO_PROPIEDAD,
                    paso="esperando_tipo_propiedad",
                )

            if res_op.opcion_id == "alquilar":
                estado["filtros"]["tipo_operacion"] = "alquiler"
                estado["operacion_confirmada"] = True
                estado["modo"] = "busqueda"
                return guardar_pregunta_activa(
                    estado,
                    PREGUNTA_TIPO_PROPIEDAD,
                    paso="esperando_tipo_propiedad",
                )

            if res_op.opcion_id == "anuncio":
                estado["modo"] = "anuncio"
                estado["esperando_codigo"] = True
                return (
                    "¡Claro! Envíame el código que aparece en la publicación "
                    "(por ejemplo MLV-123456789 o el ID Mettryc) o el enlace directo "
                    "para mostrarte la propiedad."
                )

            if res_op.opcion_id == "publicar":
                return await procesar_flujo_propietario(estado, mensaje)

            if res_op.opcion_id == "humano":
                return await procesar_solicitud_humano(sender, mensaje, estado)

            if res_op.opcion_id == "mettryc":
                estado["modo"] = "mettryc"
                return guardar_pregunta_activa(
                    estado,
                    PREGUNTA_CATEGORIAS_METTRYC,
                    paso="esperando_categoria_mettryc",
                )

    # --- 4. Tipo de Operación Cliente ---
    if pregunta_activa.id == "operacion_cliente":
        res_op = resolver_opcion(mensaje, pregunta_activa)
        if res_op.entendida:
            limpiar_pregunta_activa(estado)
            estado["filtros"]["tipo_operacion"] = res_op.opcion_id
            estado["operacion_confirmada"] = True

            if not estado["filtros"].get("tipo_propiedad"):
                return guardar_pregunta_activa(
                    estado,
                    PREGUNTA_TIPO_PROPIEDAD,
                    paso="esperando_tipo_propiedad",
                )

    # --- 5. Tipo de Propiedad ---
    if pregunta_activa.id == "tipo_propiedad":
        res_op = resolver_opcion(mensaje, pregunta_activa)
        if res_op.entendida:
            limpiar_pregunta_activa(estado)
            estado["filtros"]["tipo_propiedad"] = res_op.opcion_id
        else:
            tipo_detectado = detectar_tipo_propiedad(mensaje)
            if tipo_detectado:
                limpiar_pregunta_activa(estado)
                estado["filtros"]["tipo_propiedad"] = tipo_detectado

        if estado["filtros"].get("tipo_propiedad"):
            if not estado.get("pregunta_rol_realizada"):
                return guardar_pregunta_activa(
                    estado,
                    PREGUNTA_ROL_USUARIO,
                    paso="esperando_rol_usuario",
                )

    # --- 6. Rol de Usuario ---
    if pregunta_activa.id == "rol_usuario":
        res_op = resolver_opcion(mensaje, pregunta_activa)
        if res_op.entendida:
            limpiar_pregunta_activa(estado)
            estado["rol"] = res_op.opcion_id
            estado["confianza_rol"] = 1.0
            estado["pregunta_rol_realizada"] = True

            if res_op.opcion_id == "colega_inmobiliario":
                return await procesar_flujo_colega(estado, mensaje)

    # --- 7. Acción en Resultados Cliente ---
    if pregunta_activa.id in ["accion_resultados_cliente", "accion_ficha_unica"]:
        res_op = resolver_opcion(mensaje, pregunta_activa)
        if res_op.entendida:
            limpiar_pregunta_activa(estado)

            if res_op.opcion_id == "agendar_visita":
                lote = estado.get("ultimo_lote", [])
                if len(lote) == 1:
                    prop = buscar_por_codigo(lote[0])
                    if prop:
                        estado["propiedad_interes"] = prop
                    estado["objetivo"] = "captura_lead"
                    estado["motivo_lead"] = "agendar_visita"
                    estado["paso"] = "capturando_lead"
                    faltantes = datos_lead_faltantes(estado)
                    return mensaje_solicitud_datos_lead(faltantes, saludo=True)

                estado["paso"] = "esperando_seleccion_visita"
                return (
                    "¿Para cuál de las opciones deseas agendar la visita? "
                    "Responde con el número de opción (ej. 1, 2 o 3)."
                )

            if res_op.opcion_id == "preguntar_propiedad":
                lote = estado.get("ultimo_lote", [])
                if len(lote) == 1:
                    prop = buscar_por_codigo(lote[0])
                    if prop:
                        estado["propiedad_pregunta"] = prop
                    estado["paso"] = "esperando_pregunta_propiedad"
                    return "¿Qué consulta o duda tienes sobre esta propiedad? Escríbela con gusto te respondo."

                estado["paso"] = "esperando_seleccion_pregunta"
                return (
                    "¿Sobre cuál opción deseas consultar? "
                    "Responde con el número de opción (ejemplo: 1, 2 o 3)."
                )

            if res_op.opcion_id == "mostrar_mas":
                return await mostrar_propiedades(estado)

            if res_op.opcion_id == "cambiar_busqueda":
                estado = reiniciar_busqueda(estado)
                return guardar_pregunta_activa(
                    estado,
                    PREGUNTA_OPERACION_CLIENTE,
                    paso="esperando_operacion_cliente",
                )

            if res_op.opcion_id == "solicitar_humano":
                return await procesar_solicitud_humano(sender, mensaje, estado)

            if res_op.opcion_id == "buscar_similares":
                return await mostrar_propiedades(estado)

    # --- 8. Acción en Resultados Colega ---
    if pregunta_activa.id == "accion_resultados_colega":
        res_op = resolver_opcion(mensaje, pregunta_activa)
        if res_op.entendida:
            limpiar_pregunta_activa(estado)

            if res_op.opcion_id == "contactar_captador":
                estado["paso"] = "esperando_seleccion_captador_colega"
                return "Indícame el número de opción (1 a 5) de la cual deseas revisar los datos del captador."

            if res_op.opcion_id == "mostrar_mas":
                return await mostrar_propiedades(estado)

            if res_op.opcion_id == "cambiar_busqueda":
                estado = reiniciar_busqueda(estado)
                return "Entendido, colega. ¿Qué tipo de propiedad y zona busca tu cliente?"

            if res_op.opcion_id == "solicitar_humano":
                return await procesar_solicitud_humano(sender, mensaje, estado)

    # --- 9. Pasos Secundarios de Selección ---
    if paso == "esperando_seleccion_visita":
        pos = detectar_posicion(mensaje)
        lote = estado.get("ultimo_lote", [])
        if pos and 1 <= pos <= len(lote):
            prop = buscar_por_codigo(lote[pos - 1])
            if prop:
                estado["propiedad_interes"] = prop
            estado["objetivo"] = "captura_lead"
            estado["motivo_lead"] = "agendar_visita"
            estado["paso"] = "capturando_lead"
            limpiar_pregunta_activa(estado)
            faltantes = datos_lead_faltantes(estado)
            return mensaje_solicitud_datos_lead(faltantes, saludo=True)

    if paso == "esperando_seleccion_pregunta":
        pos = detectar_posicion(mensaje)
        lote = estado.get("ultimo_lote", [])
        if pos and 1 <= pos <= len(lote):
            prop = buscar_por_codigo(lote[pos - 1])
            if prop:
                estado["propiedad_pregunta"] = prop
            estado["paso"] = "esperando_pregunta_propiedad"
            limpiar_pregunta_activa(estado)
            return f"Perfecto, dime tu pregunta sobre la Opción {pos} (*{prop.get('titulo') if prop else ''}*):"

    if paso == "esperando_pregunta_propiedad":
        prop = estado.get("propiedad_pregunta") or estado.get("propiedad_contexto")
        if prop:
            respuesta_ia = await responder_pregunta_propiedad_ia(estado, prop, mensaje)
            estado["paso"] = "esperando_accion_resultados"
            cierre = guardar_pregunta_activa(
                estado,
                PREGUNTA_ACCION_FICHA_UNICA,
                paso="esperando_accion_resultados",
            )
            return f"{respuesta_ia}\n\n{cierre}"

    if paso == "esperando_seleccion_captador_colega":
        pos = detectar_posicion(mensaje)
        lote = estado.get("ultimo_lote", [])
        if pos and 1 <= pos <= len(lote):
            prop = buscar_por_codigo(lote[pos - 1])
            limpiar_pregunta_activa(estado)
            if prop:
                captador_wasi = prop.get("captador_wasi", "Asesor Mettryc")
                cruce = cruzar_captador_con_sheet(captador_wasi)
                tel = cruce.get("telefono")

                if tel:
                    return (
                        f"El captador de la Opción {pos} (*{prop.get('titulo')}*) es *{cruce['nombre']}*.\n"
                        f"📲 Puedes escribirle directamente por WhatsApp: https://wa.me/{tel}"
                    )
                return (
                    f"El captador asignado en Wasi es *{captador_wasi}*, pero su número "
                    "no aparece en nuestro directorio actual de turnos."
                )

    return None

# ============================================================
# MOSTRAR PROPIEDADES Y PREGUNTAS FALTANTES
# ============================================================

def criterios_suficientes(estado: dict) -> bool:
    filtros = estado.get("filtros", {})

    if not estado.get("operacion_confirmada"):
        return False

    rol = estado.get("rol") or "cliente"

    if rol == "colega_inmobiliario":
        return bool(
            filtros.get("tipo_operacion")
            and filtros.get("tipo_propiedad")
            and filtros.get("ciudad")
        )

    return bool(
        filtros.get("tipo_operacion")
        and filtros.get("tipo_propiedad")
        and (filtros.get("zona") or filtros.get("ciudad"))
    )


def obtener_pregunta_faltante(estado: dict) -> str:
    filtros = estado.get("filtros", {})

    if not filtros.get("tipo_propiedad"):
        return guardar_pregunta_activa(
            estado,
            PREGUNTA_TIPO_PROPIEDAD,
            paso="esperando_tipo_propiedad",
        )

    if not estado.get("operacion_confirmada") or not filtros.get("tipo_operacion"):
        return guardar_pregunta_activa(
            estado,
            PREGUNTA_OPERACION_CLIENTE,
            paso="esperando_operacion_cliente",
        )

    if not filtros.get("ciudad") and not filtros.get("zona"):
        return "¿En qué ciudad o zona/urbanización te gustaría encontrar la propiedad?"

    if not filtros.get("presupuesto_max"):
        tipo_operacion = filtros.get("tipo_operacion", "").lower()
        if "alquiler" in tipo_operacion or "alquilar" in tipo_operacion:
            return "¿Cuál es tu presupuesto estimado para alquilar?"
        return "¿Cuál es tu presupuesto estimado para la compra?"

    return "¿Hay alguna característica adicional importante? (habitaciones, baños, estacionamiento, jardín, etc.)"


async def mostrar_propiedades(estado: dict) -> str:
    rol = estado.get("rol") or "cliente"
    cantidad = MAX_PROPIEDADES_COLEGA if rol == "colega_inmobiliario" else MAX_PROPIEDADES_CLIENTE

    propiedades, motivo_falla = buscar_mejores_propiedades(estado, cantidad)

    if rol == "colega_inmobiliario" and not propiedades:
        filtros = estado.get("filtros", {})
        zona_ref = filtros.get("zona") or filtros.get("ciudad") or "esa zona"
        zona_legible = normalizar_nombre(zona_ref)

        if motivo_falla == "precio_o_caracs":
            return (
                f"No tengo inmuebles disponibles en {zona_legible} con ese presupuesto "
                "o características en este momento. ¿Quieres ajustar el presupuesto?"
            )
        return (
            f"No encontré inmuebles activos en {zona_legible} con los datos actuales. "
            "¿Podemos ampliar la zona o ajustar la ciudad?"
        )

    if rol != "colega_inmobiliario" and len(propiedades) < cantidad:
        propiedades = complementar_propiedades(estado, propiedades, cantidad)

    if not propiedades:
        estado["ultimo_lote"] = []
        filtros = estado.get("filtros", {})
        tipo_str = normalizar_nombre(filtros.get("tipo_propiedad", "inmuebles"))
        ciudad_str = normalizar_nombre(filtros.get("ciudad", "esa ciudad"))
        presupuesto = filtros.get("presupuesto_max")

        if motivo_falla == "precio_o_caracs":
            return (
                f"No tenemos {tipo_str} disponibles en {ciudad_str} por "
                f"{formato_moneda(presupuesto) if presupuesto else 'ese presupuesto'}. "
                "¿Busco en otro rango de precio o en una zona cercana?"
            )
        return (
            f"No disponemos de {tipo_str} en {ciudad_str} en este momento. "
            "¿Te gustaría buscar en otra ciudad o zona cercana?"
        )

    ids = [str(propiedad["id"]) for propiedad in propiedades]

    for property_id in ids:
        if property_id not in estado.get("propiedades_enviadas", []):
            estado.setdefault("propiedades_enviadas", []).append(property_id)

    estado["ultimo_lote"] = ids
    estado["objetivo"] = "evaluar_resultados"
    estado["modo"] = "busqueda"

    return await construir_respuesta_fichas(estado, propiedades)


async def mostrar_inmueble_especifico(estado: dict, codigo: str) -> str:
    propiedad = buscar_por_codigo(codigo)

    if not propiedad:
        estado["esperando_codigo"] = True
        return (
            f"No encontré un inmueble activo con el código *{codigo}*. "
            "Revisa si está escrito correctamente o envíame el enlace directo."
        )

    precio_venta = convertir_float(propiedad.get("precio_venta"))
    precio_alquiler = convertir_float(propiedad.get("precio_alquiler"))
    propiedad["precio_venta_float"] = precio_venta
    propiedad["precio_renta_float"] = precio_alquiler

    operacion = estado.get("filtros", {}).get("tipo_operacion")
    if not operacion:
        operacion = "venta" if precio_venta > 0 else "alquiler"

    propiedad["operacion_buscada"] = operacion
    property_id = str(propiedad["id"])

    estado["ultimo_lote"] = [property_id]
    if property_id not in estado.get("propiedades_enviadas", []):
        estado.setdefault("propiedades_enviadas", []).append(property_id)

    estado["propiedad_contexto"] = propiedad
    estado["esperando_codigo"] = False
    estado["objetivo"] = "evaluar_resultados"
    estado["modo"] = "anuncio"

    return await construir_respuesta_fichas(estado, [propiedad], especifica=True)


# ============================================================
# NÚCLEO DEL MOTOR CONVERSACIONAL
# ============================================================

async def procesar_mensaje(sender: str, mensaje: str) -> str:
    estado = obtener_sesion(sender)
    estado = verificar_caducidad_y_amnesia(estado)

    # --- 1. Interrupción por solicitud directa de atención humana ---
    if solicita_humano(mensaje):
        respuesta_humano = await procesar_solicitud_humano(sender, mensaje, estado)
        agregar_historial(estado, "user", mensaje)
        agregar_historial(estado, "assistant", respuesta_humano)
        guardar_sesion(sender, estado)
        return respuesta_humano

    # --- 2. Procesamiento de Estado Activo (Máquina de Estados) ---
    respuesta_estado_activo = await procesar_estado_activo(sender, mensaje, estado)
    if respuesta_estado_activo is not None:
        agregar_historial(estado, "user", mensaje)
        agregar_historial(estado, "assistant", respuesta_estado_activo)
        guardar_sesion(sender, estado)
        return respuesta_estado_activo

    # --- 3. Clasificación de Evento de Entrada Determinista ---
    evento = clasificar_evento_entrada(mensaje)

    if evento.tipo == "saludo":
        respuesta = guardar_pregunta_activa(
            estado,
            PREGUNTA_MENU_PRINCIPAL,
            paso="esperando_menu_principal",
        )
        agregar_historial(estado, "user", mensaje)
        agregar_historial(estado, "assistant", respuesta)
        guardar_sesion(sender, estado)
        return respuesta

    if evento.tipo == "mercadolibre":
        respuesta = await procesar_flujo_mercadolibre(estado, mensaje, evento.codigo)
        agregar_historial(estado, "user", mensaje)
        agregar_historial(estado, "assistant", respuesta)
        guardar_sesion(sender, estado)
        return respuesta

    if evento.tipo == "otro_anuncio":
        respuesta = await procesar_flujo_otro_anuncio(estado, mensaje, evento.codigo)
        agregar_historial(estado, "user", mensaje)
        agregar_historial(estado, "assistant", respuesta)
        guardar_sesion(sender, estado)
        return respuesta

    if evento.tipo == "colega":
        respuesta = await procesar_flujo_colega(estado, mensaje)
        agregar_historial(estado, "user", mensaje)
        agregar_historial(estado, "assistant", respuesta)
        guardar_sesion(sender, estado)
        return respuesta

    if evento.tipo == "consulta_mettryc":
        respuesta = await procesar_flujo_mettryc(estado, mensaje)
        agregar_historial(estado, "user", mensaje)
        agregar_historial(estado, "assistant", respuesta)
        guardar_sesion(sender, estado)
        return respuesta

    # --- 4. Análisis de Decisión con Inteligencia Artificial ---
    decision = await decidir_con_ia(mensaje, estado)

    if decision.accion.tipo == "mostrar_menu_principal":
        respuesta = guardar_pregunta_activa(
            estado,
            PREGUNTA_MENU_PRINCIPAL,
            paso="esperando_menu_principal",
        )
        agregar_historial(estado, "user", mensaje)
        agregar_historial(estado, "assistant", respuesta)
        guardar_sesion(sender, estado)
        return respuesta

    if decision.accion.tipo == "publicar_propiedad":
        respuesta = await procesar_flujo_propietario(estado, mensaje)
        agregar_historial(estado, "user", mensaje)
        agregar_historial(estado, "assistant", respuesta)
        guardar_sesion(sender, estado)
        return respuesta

    if decision.accion.tipo == "consulta_mettryc":
        respuesta = await procesar_flujo_mettryc(estado, mensaje)
        agregar_historial(estado, "user", mensaje)
        agregar_historial(estado, "assistant", respuesta)
        guardar_sesion(sender, estado)
        return respuesta

    # --- 5. Aplicar Filtros y Actualizaciones de la IA ---
    hubo_cambio = aplicar_decision(estado, decision, mensaje)

    accion_tipo = decision.accion.tipo

    if accion_tipo == "reiniciar_busqueda":
        estado = reiniciar_busqueda(estado)
        respuesta = guardar_pregunta_activa(
            estado,
            PREGUNTA_OPERACION_CLIENTE,
            paso="esperando_operacion_cliente",
        )

    elif accion_tipo in ["buscar_por_codigo", "pedir_codigo_inmueble"]:
        codigo = decision.accion.codigo or extraer_codigo_inmueble(mensaje)
        if codigo:
            respuesta = await mostrar_inmueble_especifico(estado, codigo)
        else:
            respuesta = await procesar_flujo_otro_anuncio(estado, mensaje)

    elif accion_tipo == "mostrar_mas_propiedades":
        respuesta = await mostrar_propiedades(estado)

    elif accion_tipo == "buscar_propiedades":
        if criterios_suficientes(estado):
            respuesta = await mostrar_propiedades(estado)
        else:
            pregunta = obtener_pregunta_faltante(estado)
            respuesta = await humanizar_texto_con_ia(estado, pregunta, mensaje)

    else:
        if criterios_suficientes(estado) and hubo_cambio:
            respuesta = await mostrar_propiedades(estado)
        elif not criterios_suficientes(estado):
            pregunta = obtener_pregunta_faltante(estado)
            respuesta = await humanizar_texto_con_ia(estado, pregunta, mensaje)
        else:
            respuesta = decision.mensaje or (
                "Perfecto. Cuéntame si deseas agregar algún otro detalle o preferencia."
            )

    if (
        estado.get("objetivo") == "captura_lead"
        and lead_completo(estado)
        and estado.get("lead_confirmado")
    ):
        respuesta = await completar_y_asignar_lead(estado)

    if not respuesta:
        respuesta = "¿Podrías indicarme qué tipo de propiedad o servicio necesitas?"

    agregar_historial(estado, "user", mensaje)
    agregar_historial(estado, "assistant", respuesta)
    guardar_sesion(sender, estado)

    return respuesta


# ============================================================
# INICIALIZACIÓN DE SERVICIOS
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
                f"Error en inicialización inicial: tipo={type(resultado).__name__} "
                f"detalle={str(resultado)[:200]}"
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        follow_redirects=True,
        trust_env=False,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        headers={
            "User-Agent": "Mettryc-Chatbot/2.0",
        },
    )

    tarea = asyncio.create_task(inicializar_datos())
    yield

    if not tarea.done():
        tarea.cancel()
        try:
            await tarea
        except asyncio.CancelledError:
            pass

    await http_client.aclose()


# ============================================================
# APLICACIÓN FASTAPI
# ============================================================

app = FastAPI(
    title="Mettryc Realty Paty",
    version="2.0.0",
    lifespan=lifespan,
)


def validar_api_key(api_key: Optional[str]) -> None:
    if not API_KEYS_AGENTES:
        raise HTTPException(
            status_code=503,
            detail="API_KEYS_AGENTES no está configurada en las variables de entorno.",
        )
    if not api_key or api_key not in API_KEYS_AGENTES:
        raise HTTPException(
            status_code=403,
            detail="Acceso denegado. Clave de API no válida.",
        )


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {
        "service": "Mettryc Realty Paty",
        "version": "2.0.0",
        "status": "online",
    }


@app.get("/health")
async def health():
    ultima_actualizacion = inventory_cache.get("ultima_actualizacion")
    return {
        "status": "ok",
        "inventario": len(inventory_cache["inventario"]),
        "ultima_actualizacion_inventario": (
            ultima_actualizacion.isoformat() if ultima_actualizacion else None
        ),
        "agentes": len(sheets_cache["agentes"]),
        "captadores": len(sheets_cache["captadores"]),
        "sesiones_memoria": len(sesiones),
        "persistencia": "memoria",
    }


@app.post("/admin/refresh")
async def refresh(
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
):
    validar_api_key(x_api_key)
    await asyncio.gather(
        actualizar_inventario(force=True),
        sincronizar_google_sheet(force=True),
    )
    return {
        "ok": True,
        "propiedades": len(inventory_cache["inventario"]),
        "agentes": len(sheets_cache["agentes"]),
        "captadores": len(sheets_cache["captadores"]),
    }


@app.post("/admin/reset/{sender}")
async def reset_session(
    sender: str,
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
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
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
):
    validar_api_key(x_api_key)

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="JSON de entrada no válido.",
        )

    payload = data.get("query") if isinstance(data.get("query"), dict) else data
    sender = str(payload.get("sender", "")).strip()
    mensaje_raw = str(payload.get("message", "")).strip()
    message_id = str(payload.get("message_id") or payload.get("id") or "").strip()

    if not sender:
        raise HTTPException(
            status_code=422,
            detail="Falta el parámetro obligatorio 'sender'.",
        )

    if not mensaje_raw:
        return {"replies": []}

    if not message_id:
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{sender}:{mensaje_raw}"))

    if mensaje_es_duplicado(sender, message_id):
        return {"replies": []}

    estado = obtener_sesion(sender)

    # --- Comandos de Control de Chat (Administradores) ---
    mensaje_cmd = mensaje_raw.lower()

    if mensaje_cmd.startswith("/pause"):
        if es_sender_admin(sender):
            partes = mensaje_cmd.split()
            minutos = 30
            if len(partes) > 1:
                try:
                    minutos = max(1, min(240, int(partes[1])))
                except ValueError:
                    pass
            pausa_hasta = datetime.utcnow() + timedelta(minutes=minutos)
            estado["pausa_hasta"] = pausa_hasta.isoformat()
            guardar_sesion(sender, estado)
            return {
                "replies": [
                    {
                        "message": f"⏸️ Bot pausado en este chat por {minutos} minutos. Usa /play para reanudarlo."
                    }
                ]
            }

    if mensaje_cmd.startswith("/play"):
        if es_sender_admin(sender):
            if estado.pop("pausa_hasta", None):
                guardar_sesion(sender, estado)
                return {"replies": [{"message": "▶️ Bot reanudado exitosamente en este chat."}]}
            return {"replies": [{"message": "ℹ️ Este chat no estaba en pausa."}]}

    if mensaje_cmd == "/reiniciar":
        if es_sender_admin(sender):
            sesiones[sender] = crear_sesion(sender)
            return {"replies": [{"message": "🧹 Chat e historial reiniciados exitosamente."}]}

    if mensaje_cmd == "/status":
        if es_sender_admin(sender):
            status_msg = [
                f"🖥️ Estado Mettryc Bot - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                f"👥 Usuarios en memoria: {len(sesiones)}",
                f"🏠 Propiedades en Wasi: {len(inventory_cache.get('inventario', []))}",
                f"🔄 Última actualización inventario: {inventory_cache.get('ultima_actualizacion', 'N/D')}",
                f"🤖 Modelo IA: {MODELO_AGENTE_PRINCIPAL}",
            ]
            return {"replies": [{"message": "\n".join(status_msg)}]}

    pausa_hasta_str = estado.get("pausa_hasta")
    if pausa_hasta_str:
        try:
            pausa_hasta = datetime.fromisoformat(pausa_hasta_str)
            if datetime.utcnow() < pausa_hasta and not es_sender_admin(sender):
                agregar_historial(estado, "user", mensaje_raw)
                guardar_sesion(sender, estado)
                return {"replies": []}
            if datetime.utcnow() >= pausa_hasta:
                estado.pop("pausa_hasta", None)
        except ValueError:
            estado.pop("pausa_hasta", None)

    if sender not in locks_usuarios:
        locks_usuarios[sender] = asyncio.Lock()

    try:
        if not inventory_cache["inventario"]:
            await actualizar_inventario(force=True)
        elif inventario_necesita_actualizacion():
            asyncio.create_task(actualizar_inventario())

        if sheets_necesita_actualizacion():
            asyncio.create_task(sincronizar_google_sheet())
    except Exception as e:
        logger.error(f"Error comprobando actualizaciones en background: {str(e)}")

    try:
        async with locks_usuarios[sender]:
            respuesta = await procesar_mensaje(sender, mensaje_raw)

            if respuesta is None:
                respuesta = "No se pudo generar una respuesta."

            if isinstance(respuesta, dict):
                return respuesta

            respuesta_formateada = str(respuesta).replace("**", "*")

            return {
                "replies": [
                    {
                        "message": respuesta_formateada,
                    }
                ]
            }

    except Exception as exc:
        logger.exception(f"Error procesando mensaje sender={sender[-4:]} tipo={type(exc).__name__}")
        return {
            "replies": [
                {
                    "message": (
                        "Disculpa, tuve un inconveniente procesando tu mensaje. "
                        "¿Podrías intentarlo de nuevo?"
                    )
                }
            ]
        }

