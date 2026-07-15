import re
import unicodedata
from typing import Any, Optional, Set

# ============================================================
# UTILIDADES DE TEXTO Y NÚMEROS
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

# ============================================================
# UTILIDADES DE DATOS DE CONTACTO (LEAD)
# ============================================================

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

# ============================================================
# UTILIDADES INMOBILIARIAS Y WASI
# ============================================================

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


def zona_coincide(buscada: str, zona_prop: str, ciudad_prop: str) -> bool:
    if not buscada: return True
    ciudad_prop_norm = normalizar_texto(ciudad_prop)
    zona_prop_norm = normalizar_texto(zona_prop)
    buscados = tokens_zona(buscada)
    disponibles = tokens_zona(f"{zona_prop} {ciudad_prop}")

    if not buscados: return True
    if not disponibles: return False

    ciudades_mettryc = {"valencia", "naguanagua", "san diego", "guacara", "barquisimeto", "cabudare", "caracas"}
    ciudades_pedidas = buscados.intersection(ciudades_mettryc)
    if ciudades_pedidas:
        if not any(ciudad in ciudad_prop_norm for ciudad in ciudades_pedidas):
            return False

    coincidencias = buscados.intersection(disponibles)
    if len(coincidencias) >= 1: 
        return True

    texto_completo_prop = f"{zona_prop_norm} {ciudad_prop_norm}"
    for palabra in buscados:
        if palabra in texto_completo_prop:
            return True

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


def detectar_rol_explicito(mensaje: str) -> Optional[str]:
    texto = normalizar_texto(mensaje)

    patrones_cliente = [
        r"\bno\s+soy\s+(asesor|agente|corredor|broker|realtor|colega)\b",
        r"\bsoy\s+cliente\b",
        r"\bes\s+para\s+mi\b",
        r"\bbusco\s+para\s+mi\b",
    ]

    if any(re.search(patron, texto) for patron in patrones_cliente):
        return "cliente"

    patrones_colega = [
        r"\bsoy\s+(asesor|asesora|agente|corredor|corredora|broker|realtor|asociado)\b",
        r"\bcolega\b",
        r"\btengo\s+un\s+cliente\b",
        r"\bbusco\s+para\s+un\s+cliente\b",
        r"\btrabajo\s+en\s+una\s+inmobiliaria\b",
        r"\bcomparto\s+comision\b",
        r"\bmettryc\s+realty\b",
        r"perfil\s+juridico",
        r"perfil\s+natural",
        r"dinero\s+en\s+mano",
    ]

    if any(re.search(patron, texto) for patron in patrones_colega):
        return "colega_inmobiliario"

    palabras_solicitud = ["solicito", "solicitud", "requerimiento", "requiero"]
    palabras_jerga = [
        "canon", "perfil", "cliente", "asesor", "asociado", "inmobiliaria", 
        "realty", "realtor", "broker", "real estate", "negociacion", 
        "comision", "aliado"
    ]
    
    if any(palabra in texto for palabra in palabras_solicitud) and any(jerga in texto for jerga in palabras_jerga):
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

# ============================================================
# UTILIDADES DE JSON (IA)
# ============================================================

def limpiar_json_modelo(contenido: str) -> str:
    texto = str(contenido or "").strip()

    if texto.startswith("```"):
        texto = re.sub(
            r"^
http://googleusercontent.com/immersive_entry_chip/0

**¿Cuál es el siguiente paso?**
El siguiente archivo lógico y más sencillo de separar debería ser **`config.py`** (para sacar todas las variables de entorno como API Keys y URLs) o **`models.py`** (para mover los esquemas Pydantic). 

¿Cuál prefieres que extraigamos a continuación?