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
# CONFIGURACIÓN Y MODELOS EN CASCADA (TRIPLE RESPALDO)
# ============================================================

HORARIOS_ACTUALIZACION_INVENTARIO = [0, 12]

# Configuración de Modelos: Principal -> Respaldo 1 -> Respaldo 2
MODELO_PRINCIPAL = os.getenv("MODELO_PRINCIPAL", "google/gemini-2.5-flash-lite")
MODELO_RESPALDO_1 = os.getenv("MODELO_RESPALDO_1", "anthropic/claude-3-haiku")
MODELO_RESPALDO_2 = os.getenv("MODELO_RESPALDO_2", "openai/gpt-4o-mini")
MAX_TOKENS_IA = int(os.getenv("MAX_TOKENS_IA", "600"))

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

# ============================================================
# ESTADOS Y UTILIDADES
# ============================================================

ESTADO_DIAGNOSTICO_IA = "diagnostico_ia"
ESTADO_MOSTRANDO_PROPIEDADES = "mostrando_propiedades"
ESTADO_CAPTURANDO_LEAD = "capturando_lead"
ESTADO_LEAD_COMPLETO = "lead_completo"

STOPWORDS_ZONA = {"el", "la", "los", "las", "de", "del", "en", "y", "urb", "urbanizacion", "urbanización", "sector", "zona", "ciudad", "estado", "venezuela"}
TOKENS_DIRECCION_ZONA = {"norte", "sur", "este", "oeste", "centro"}
SINONIMOS_TIPO = {"tohouse": "townhouse", "towhouse": "townhouse", "twhouse": "townhouse", "town house": "townhouse", "aparto quinta": "apartoquinta", "aptoquinta": "apartoquinta", "apartoquita": "apartoquinta"}
STOPWORDS_NOMBRE_EXTRA = {"la", "el", "que", "enviaste", "ultima", "última", "primera", "segunda", "tercera", "opcion", "opción", "interesa", "quiero", "esa", "esta", "hola", "buenas", "soy", "me", "llamo", "mi", "nombre", "es"}

def normalizar_texto(texto: str) -> str:
    if not texto: return ""
    texto = str(texto).lower().strip()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = re.sub(r"[^a-z0-9\s@.+-]", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()

def capitalizar_nombre(nombre: str) -> str:
    if not nombre: return ""
    palabras = [p for p in re.split(r"\s+", nombre.strip()) if p]
    return " ".join(p[:1].upper() + p[1:].lower() for p in palabras)

def limpiar_telefono(valor: str) -> str:
    return re.sub(r"\D", "", str(valor or ""))

def formato_moneda(valor) -> str:
    try:
        val = float(valor)
        if val == 0: return "N/D"
        return f"${val:,.0f}".replace(",", ".")
    except Exception:
        return "N/D"

def normalizar_tipo_propiedad(tipo: str) -> str:
    t = normalizar_texto(tipo)
    if t in SINONIMOS_TIPO: return SINONIMOS_TIPO[t]
    if "townhouse" in t: return "townhouse"
    if any(k in t for k in ["apartoquinta", "aparto quinta", "apartoquita"]): return "apartoquinta"
    if "penthouse" in t: return "penthouse"
    if "apartamento" in t or t == "apto": return "apartamento"
    if "casa" in t: return "casa"
    return t

def normalizar_operacion(op: str) -> str:
    t = normalizar_texto(op)
    if t in {"venta", "comprar", "compra", "para comprar", "comprarla", "adquirir"}: return "venta"
    if t in {"alquiler", "alquilar", "arrendar", "arrendamiento", "renta"}: return "alquiler"
    return ""

def mensaje_parece_contacto(mensaje: str) -> bool:
    if not mensaje: return False
    tiene_correo = bool(re.search(r"[\w\.-]+@[\w\.-]+\.\w+", mensaje, re.IGNORECASE))
    tiene_tel = bool(re.search(r"\+?\d[\d\s\-\(\)]{7,}\d", mensaje))
    return tiene_correo or tiene_tel

def es_nombre_persona_valido(nombre: str) -> bool:
    if not nombre: return False
    n = normalizar_texto(nombre)
    palabras = [p for p in re.findall(r"[A-Za-zÀ-ÖØ-ÿ'´-]+", nombre) if normalizar_texto(p) not in STOPWORDS_NOMBRE_EXTRA and len(p) >= 2]
    return 2 <= len(palabras) <= 4

def parsear_presupuesto_texto(texto: str):
    if not texto: return None
    t = normalizar_texto(texto)
    if re.search(r"\+?\d[\d\s\-\(\)]{9,}\d", texto) and not any(k in t for k in ["presupuesto", "maximo", "máximo", "hasta", "usd", "dolar", "dolares", "$", "mil", "millon", "millones", "k", "mm"]): return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(mil|k)\b", t)
    if m: return float(m.group(1).replace(",", ".")) * 1000
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(millon|millones|mm)\b", t)
    if m: return float(m.group(1).replace(",", ".")) * 1_000_000
    candidatos = re.findall(r"(?:usd|us|dolares|dolar|\$)?\s*([0-9][0-9\.,]{3,})", t)
    for c in candidatos:
        limpio = c.strip()
        if "." in limpio and "," in limpio: limpio = limpio.replace(".", "").replace(",", ".") if limpio.rfind(",") > limpio.rfind(".") else limpio.replace(",", "")
        elif "." in limpio and len(limpio.split(".")[-1]) == 3: limpio = limpio.replace(".", "")
        elif "," in limpio: limpio = limpio.replace(",", "") if len(limpio.split(",")[-1]) == 3 else limpio.replace(",", ".")
        try:
            val = float(limpio)
            if 1000 <= val <= 20_000_000: return val
        except Exception: continue
    return None

def parsear_precio_wasi(valor_numerico=None, valor_label=None) -> float:
    if valor_numerico not in [None, "", "N/D"]:
        try: return float(valor_numerico)
        except Exception: pass
    if valor_label in [None, "", "N/D"]: return 0.0
    t = re.sub(r"[^\d.,]", "", str(valor_label).lower().strip())
    if not t: return 0.0
    if "." in t and "," in t: t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "." in t and len(t.split(".")[-1]) == 3: t = t.replace(".", "")
    elif "," in t: t = t.replace(",", "") if len(t.split(",")[-1]) == 3 else t.replace(",", ".")
    try: return float(t)
    except Exception: return 0.0

def tokens_relevantes(texto: str) -> set:
    return {t for t in normalizar_texto(texto).split() if t not in STOPWORDS_ZONA and len(t) > 1}

def zona_coincide(zona_buscada: str, zona_propiedad: str, ciudad_propiedad: str = "") -> bool:
    buscada_norm = normalizar_texto(zona_buscada)
    texto_propiedad = f"{normalizar_texto(zona_propiedad)} {normalizar_texto(ciudad_propiedad)}".strip()
    if not buscada_norm: return True
    if not texto_propiedad: return False
    tokens_busqueda = tokens_relevantes(buscada_norm)
    tokens_propiedad = tokens_relevantes(texto_propiedad)
    if not tokens_busqueda: return True
    direcciones = tokens_busqueda.intersection(TOKENS_DIRECCION_ZONA)
    if direcciones and not direcciones.issubset(tokens_propiedad): return False
    if len(tokens_busqueda) >= 2: return tokens_busqueda.issubset(tokens_propiedad)
    return (next(iter(tokens_busqueda)) in tokens_propiedad) or (buscada_norm in texto_propiedad)

def actualizar_memoria(sender: str, mensaje_usuario: str, respuesta_bot: str):
    if sender not in memoria_conversaciones: memoria_conversaciones[sender] = []
    memoria_conversaciones[sender].append({"role": "user", "content": mensaje_usuario})
    memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_bot})
    if len(memoria_conversaciones[sender]) > 30: memoria_conversaciones[sender] = memoria_conversaciones[sender][-30:]

def obtener_estado_usuario(sender: str) -> dict:
    if sender not in estado_usuarios:
        estado_usuarios[sender] = {
            "estado": ESTADO_DIAGNOSTICO_IA, "rol": None,
            "filtros": {"tipo_propiedad": "", "tipo_operacion": "", "zona": "", "presupuesto": None, "habitaciones": "", "caracteristicas": ""},
            "propiedades_enviadas": [], "lead": {"nombre": "", "correo": "", "whatsapp": ""},
            "ultima_respuesta": "", "mensaje_previo": "", "ultimo_mensaje_ts": 0.0
        }
    return estado_usuarios[sender]

def obtener_lock_usuario(sender: str) -> asyncio.Lock:
    if sender not in locks_usuarios: locks_usuarios[sender] = asyncio.Lock()
    return locks_usuarios[sender]

# ============================================================
# INVENTARIO WASI Y GOOGLE SHEETS
# ============================================================

def calcular_proxima_actualizacion(ahora: datetime = None) -> datetime:
    ahora = ahora or datetime.now()
    ventanas = sorted(HORARIOS_ACTUALIZACION_INVENTARIO)
    for hora in ventanas:
        candidato = ahora.replace(hour=hora, minute=0, second=0, microsecond=0)
        if candidato > ahora: return candidato
    return (ahora + timedelta(days=1)).replace(hour=ventanas[0], minute=0, second=0, microsecond=0)

def necesita_actualizar_inventario(force: bool = False) -> bool:
    if force or not cache["inventario"]: return True
    return datetime.now() >= cache.get("proxima_actualizacion", datetime.min)

def obtener_inventario_desde_wasi():
    propiedades, take, skip = [], 100, 0
    while True:
        params = {"wasi_token": os.getenv("WASI_TOKEN"), "id_company": os.getenv("WASI_COMPANY_ID"), "take": take, "skip": skip, "status": 1}
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
                        "captador_propiedad": f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip() or "Asesor Mettryc",
                        "telefono_captador_wasi": user_data.get("phone", "")
                    })
            if contador < take: break
            skip += take
            time.sleep(1.0)
        except Exception as e:
            logger.error(f"Error Wasi: {e}")
            break
    return propiedades

def actualizar_cache_inventario(force: bool = False):
    if not necesita_actualizar_inventario(force): return False
    props = obtener_inventario_desde_wasi()
    if props:
        ahora = datetime.now()
        cache["inventario"] = props
        cache["ultima_actualizacion"] = ahora
        cache["proxima_actualizacion"] = calcular_proxima_actualizacion(ahora + timedelta(seconds=1))
        return True
    return False

async def garantizar_inventario_actualizado(force: bool = False):
    await asyncio.to_thread(actualizar_cache_inventario, force)

def sincronizar_google_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL")
    if not script_url or (datetime.now() - sheets_cache["ultima_actualizacion"] <= INTERVALO_ACTUALIZACION_SHEETS and sheets_cache["agentes"]): return
    try:
        r = requests.get(script_url, timeout=12)
        r.raise_for_status()
        payload = r.json()
        if not isinstance(payload, dict): return
        sheets_cache["agentes"] = payload.get("agentes", [])
        captadores_payload = payload.get("captadores", {})
        pairs = captadores_payload.items() if isinstance(captadores_payload, dict) else [(r.get("nombre") or r.get("name"), r.get("telefono") or r.get("phone")) for r in (captadores_payload if isinstance(captadores_payload, list) else []) if isinstance(r, dict)]
        sheets_cache["captadores"] = {str(k).strip(): str(v).strip() for k, v in pairs if k and v}
        sheets_cache["ultima_actualizacion"] = datetime.now()
    except Exception as e: logger.error(f"Error Sheets: {e}")

def asignar_agente_round_robin():
    sincronizar_google_sheet()
    agentes = sheets_cache["agentes"]
    if not agentes: return None
    sheets_cache["ultimo_indice"] = (sheets_cache["ultimo_indice"] + 1) % len(agentes)
    return agentes[sheets_cache["ultimo_indice"]]

def obtener_telefono_captador_de_sheet(nombre_captador: str) -> str:
    if not sheets_cache["captadores"]: sincronizar_google_sheet()
    if not nombre_captador: return "N/D"
    nombre_norm = normalizar_texto(nombre_captador)
    for nombre_sheet, telefono in sheets_cache["captadores"].items():
        if normalizar_texto(nombre_sheet) == nombre_norm: return telefono
    tokens_wasi = tokens_relevantes(nombre_captador)
    best_score, best_phone = 0, "N/D"
    for nombre_sheet, telefono in sheets_cache["captadores"].items():
        score = len(tokens_wasi.intersection(tokens_relevantes(nombre_sheet)))
        if score > best_score: best_score, best_phone = score, telefono
    return best_phone if best_score >= 2 else "N/D"

# ============================================================
# OPENROUTER IA CON SISTEMA DE TRIPLE RESPALDO FLEXIBLE
# ============================================================

def consultar_ia(mensajes: list, max_tokens: int = MAX_TOKENS_IA, fallback: str = "", modelos_personalizados: list = None) -> str:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key: return fallback
    
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://www.mettryc.com",
        "X-Title": "Mettryc Bot"
    }
    
    modelos_en_cascada = modelos_personalizados if modelos_personalizados else [MODELO_PRINCIPAL, MODELO_RESPALDO_1, MODELO_RESPALDO_2]
    
    for modelo in modelos_en_cascada:
        try:
            resp = requests.post(url, headers=headers, json={
                "model": modelo,
                "messages": mensajes,
                "max_tokens": max_tokens,
                "temperature": 0.2
            }, timeout=15)
            resp.raise_for_status()
            c = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            if c: return c.strip()
        except Exception as e:
            continue
            
    return fallback

def extraer_json_de_texto(texto: str) -> dict:
    if not texto: return {}
    try: return json.loads(texto)
    except Exception: pass
    ini, fin = texto.find("{"), texto.rfind("}")
    if ini != -1 and fin != -1 and fin > ini:
        try: return json.loads(texto[ini:fin + 1])
        except Exception: pass
    return {}

# ============================================================
# ARQUITECTURA DE DOS PASOS (IA ANALÍTICA + IA CONVERSACIONAL)
# ============================================================

def analizar_mensaje_ia(mensaje_usuario: str, estado: dict, historial: list) -> dict:
    """Capa 1: Extractor. Ahora también extrae códigos de inmuebles (Ads)."""
    historial_breve = [{"role": h.get("role", "user"), "content": h.get("content", "")[:220]} for h in historial[-8:]]
    system = "Eres un analista experto. Responde UNICAMENTE con un objeto JSON válido según el esquema."
    user_prompt = f"""
    Estado actual: {json.dumps({"filtros": estado.get("filtros"), "lead": estado.get("lead"), "rol": estado.get("rol")}, ensure_ascii=False)}
    Mensaje actual del usuario: "{mensaje_usuario}"

    Reglas:
    1. intencion: "saludo"|"buscar"|"mas_opciones"|"ajustar_busqueda"|"interes_propiedad"|"enviar_datos"|"objecion"|"consulta_inmueble_especifico"
    2. rol: "colega_inmobiliario" si se identifica como colega. De lo contrario "cliente".
    3. codigo_inmueble: Extrae el código si el usuario pregunta por un inmueble específico (ej. "ALM-12345" o "12345").
    4. lead: extrae nombre, correo, whatsapp si los provee explícitamente.

    Devuelve EXACTAMENTE este esquema JSON:
    {{ "intencion": "...", "rol": "cliente|colega_inmobiliario", "codigo_inmueble": "", "filtros": {{"tipo_propiedad": "", "tipo_operacion": "", "zona": "", "presupuesto": null, "habitaciones": "", "caracteristicas": ""}}, "lead": {{"nombre": "", "correo": "", "whatsapp": ""}} }}
    """
    return extraer_json_de_texto(consultar_ia([{"role": "system", "content": system}] + historial_breve + [{"role": "user", "content": user_prompt}], max_tokens=350, fallback="{}"))

def generar_respuesta_conversacional_paty(mensaje_usuario: str, contexto_sistema: dict, historial: list) -> str:
    props_encontradas = contexto_sistema.get('propiedades_encontradas_texto', '')
    if not props_encontradas:
        if "No se encontraron" in contexto_sistema.get('mensaje_interno', ''):
            props_encontradas = "[Búsqueda realizada: 0 resultados. Ofrece ajustar filtros.]"
        else:
            props_encontradas = "[Fase de diagnóstico. Sigue preguntando el dato que falta o saluda si es el primer mensaje.]"
            
    faltantes_busqueda = ", ".join(contexto_sistema.get('faltantes_busqueda', [])) or "Ninguno"
    faltantes_lead = ", ".join(contexto_sistema.get('faltantes_lead', [])) or "Ninguno"
    agente = contexto_sistema.get('agente_asignado', '') or "Ninguno"
    rol = contexto_sistema.get('rol_detectado', 'cliente')
    mensaje_interno = contexto_sistema.get('mensaje_interno', '')
    
    es_inicio = len(historial) <= 2
    
    instrucciones_contexto = f"""
    --- INSTRUCCIONES ESTRICTAS ---
    ES PRIMER MENSAJE (OBLIGATORIO SALUDAR): {"SÍ" if es_inicio else "NO"}
    ROL DETECTADO: {rol}
    DATOS DE BÚSQUEDA FALTANTES (Pregunta SOLO UNO a la vez): {faltantes_busqueda}
    DATOS DE CONTACTO FALTANTES PARA LEAD: {faltantes_lead}
    ASESOR ASIGNADO ACTUALMENTE: {agente}
    MENSAJE INTERNO: {mensaje_interno}
    PROPIEDADES A MOSTRAR: {props_encontradas}
    -------------------------------
    """

    prompt_sistema = f"""
    Eres Paty, la especialista VIP de Mettryc Realty (Valencia, Venezuela).
    {instrucciones_contexto}
    
    REGLAS Y GUIONES MAESTROS (INQUEBRANTABLES):
    1. FLUJO 1: EL SALUDO OBLIGATORIO:
       Si "ES PRIMER MENSAJE" es SÍ, TIENES QUE usar este saludo exacto:
       "¡Hola! Mi nombre es Paty de Mettryc Realty La Primera Tecnoinmobiliaria de Venezuela. ¿Cómo te puedo ayudar?"
       NO pases a preguntar datos hasta que el cliente te responda a este saludo.
       
    2. FLUJO 2: EL CLIENTE (EMBUDO DE VENTAS):
       Si "DATOS DE BÚSQUEDA FALTANTES" tiene información, haz UNA SOLA PREGUNTA siguiendo este guion:
       - Operación: "¿La buscas para comprar o alquilar?"
       - Tipo: "Ok... cuéntame ¿qué tipo de propiedad estás buscando?"
       - Zona: "Perfecto, ¿en qué zona o urbanización te gustaría?"
       - Presupuesto: "¡Buena zona! ¿Tienes un precio en mente o presupuesto máximo?"
       - Habitaciones: "Entiendo. Me puedes decir ¿Cuántas habitaciones y baños son ideales para ti?"
       - Características: "¡Excelente! Por último, ¿hay algo más que sea importante que debe tener tu próxima propiedad?"
       IMPORTANTE: Si el cliente ya dio un dato en su mensaje (ej: "hasta 200 mil"), NO LO VUELVAS A PREGUNTAR.

    3. FLUJO 3: CAPTURA DE LEAD PARA CLIENTES INTERESADOS:
       Si el cliente se interesa en una propiedad y faltan "DATOS DE CONTACTO", usa estas frases:
       - Faltando Nombre: "¡Qué bien! Te voy asignar uno de nuestros agentes para que te dé más información y puedas coordinar una visita sin compromiso. Por favor, dime ¿Cuál es tu nombre completo?"
       - Faltando Correo: "¡Un gusto conocerte! Indícame tu correo electrónico para que nuestro sistema te siga enviando opciones similares."
       - Faltando WhatsApp: "¡Gracias! Por último, dime tu número de WhatsApp para que nuestro agente te contacte."
       - Si ya hay ASESOR ASIGNADO: "¡Listo! Ya nuestro agente [Nombre del Asesor] tiene tu información y te estará contactando a tu número. ¿Hay algo más en lo que te pueda servir?"

    4. FLUJO 4: EL COLEGA INMOBILIARIO:
       Si ROL DETECTADO es "colega_inmobiliario":
       - Trátalo de profesional a profesional. ("Hola colega, con gusto te apoyo para tu cliente...").
       - Muestra la ficha de la propiedad tal cual te llega (con los datos de nuestro captador).
       - PROHIBIDO pedirle nombre, correo o WhatsApp. El Flujo de Captura de Lead NO APLICA para colegas.
       
    5. FLUJO 5: INMUEBLE ESPECÍFICO (ADS):
       Si el "MENSAJE INTERNO" te indica que el cliente viene por un anuncio o código específico, entrega la propiedad de "PROPIEDADES A MOSTRAR" inmediatamente y pregúntale de forma entusiasta si desea agendar una visita.

    6. REGLAS DE FORMATO:
       - CERO ALUCINACIONES: NO inventes propiedades que no estén en tu lista.
       - CERO JSON: Tienes prohibido usar llaves {{}}.
       - ENLACES PLANOS: Muestra los enlaces de forma PLANA (ej. https://mettryc.com/inmueble/123). No uses corchetes de Markdown.
    """
    
    mensajes = [{"role": "system", "content": prompt_sistema}] + (historial[-8:] if len(historial) > 8 else historial)
    cascada_obediente = [MODELO_RESPALDO_1, MODELO_RESPALDO_2]
    
    return consultar_ia(mensajes, max_tokens=800, modelos_personalizados=cascada_obediente)

# ============================================================
# LÓGICA DE PYTHON (Filtros, Ranking y Fichas)
# ============================================================

def fusionar_filtros(base: dict, nuevos: dict, mensaje: str) -> dict:
    out, txt = dict(base or {}), normalizar_texto(mensaje)
    if nuevos.get("tipo_propiedad"): out["tipo_propiedad"] = normalizar_tipo_propiedad(nuevos["tipo_propiedad"])
    if nuevos.get("tipo_operacion"): out["tipo_operacion"] = normalizar_operacion(nuevos["tipo_operacion"])
    if nuevos.get("zona"): out["zona"] = str(nuevos["zona"]).strip()
    if nuevos.get("habitaciones"): out["habitaciones"] = str(nuevos["habitaciones"]).strip()
    if nuevos.get("caracteristicas"): out["caracteristicas"] = str(nuevos["caracteristicas"]).strip()
    
    if nuevos.get("presupuesto") not in [None, "", []]:
        try: out["presupuesto"] = float(nuevos["presupuesto"])
        except Exception: pass
    
    if any(k in txt for k in ["townhouse", "towhouse"]): out["tipo_propiedad"] = "townhouse"
    elif "casa" in txt: out["tipo_propiedad"] = "casa"
    elif "apartamento" in txt or "apto" in txt: out["tipo_propiedad"] = "apartamento"
    if any(k in txt for k in ["comprar", "venta"]): out["tipo_operacion"] = "venta"
    elif any(k in txt for k in ["alquiler", "renta"]): out["tipo_operacion"] = "alquiler"
    if out.get("presupuesto") is None:
        p = parsear_presupuesto_texto(mensaje)
        if p: out["presupuesto"] = p
    return out

def fusionar_lead(base: dict, nuevos: dict, mensaje: str, sender: str) -> dict:
    out = dict(base or {})
    if nuevos.get("nombre"):
        cand = capitalizar_nombre(str(nuevos["nombre"]).strip())
        if es_nombre_persona_valido(cand): out["nombre"] = cand
    if nuevos.get("correo"):
        correo = str(nuevos["correo"]).strip().lower()
        if "@" in correo and "." in correo: out["correo"] = correo
    if nuevos.get("whatsapp"):
        w = limpiar_telefono(str(nuevos["whatsapp"]))
        if len(w) >= 10: out["whatsapp"] = w

    texto = mensaje or ""
    m_mail = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", texto, re.IGNORECASE)
    if m_mail: out["correo"] = m_mail.group(0).strip().lower()
    m_tel = re.search(r"(\+?\d[\d\s\-\(\)]{7,}\d)", texto)
    if m_tel: out["whatsapp"] = limpiar_telefono(m_tel.group(1))
    
    if not out.get("nombre"):
        palabras = re.findall(r"[A-Za-zÀ-ÖØ-ÿ'´-]+", texto)
        limpias = [p for p in palabras if normalizar_texto(p) not in STOPWORDS_NOMBRE_EXTRA and len(p) > 1]
        if 2 <= len(limpias) <= 4:
            cand = capitalizar_nombre(" ".join(limpias))
            if es_nombre_persona_valido(cand): out["nombre"] = cand
            
    if not out.get("whatsapp") and len(limpiar_telefono(sender)) >= 7 and limpiar_telefono(sender).isdigit():
        out["whatsapp"] = limpiar_telefono(sender)
    return out

def coincide_tipo_propiedad(propiedad: dict, tipo_filtro: str) -> bool:
    tipo = normalizar_tipo_propiedad(tipo_filtro)
    titulo = normalizar_texto(propiedad.get("titulo", ""))
    tipo_wasi = normalizar_texto(propiedad.get("tipo_propiedad_wasi", ""))
    if not tipo: return True
    if tipo == "townhouse": return "townhouse" in titulo
    if tipo == "apartoquinta": return any(k in titulo for k in ["apartoquinta", "aparto quinta", "apartoquita"])
    if tipo == "casa": return any(k in titulo or k in tipo_wasi for k in ["casa", "townhouse", "apartoquinta", "aparto quinta", "apartoquita"])
    if tipo == "penthouse": return "penthouse" in titulo
    if tipo == "apartamento": return any(k in titulo or k in tipo_wasi for k in ["apartamento", "penthouse"])
    return (tipo in tipo_wasi) or (tipo in titulo)

def elegir_top_n_propiedades(inventario: list, filtros: dict, n=3, excluir_ids=None):
    excluir_ids = set(str(x) for x in (excluir_ids or []))
    tipo_prop, tipo_op, zona = filtros.get("tipo_propiedad", ""), filtros.get("tipo_operacion", ""), filtros.get("zona", "")
    presupuesto = filtros.get("presupuesto")
    
    props = [p for p in inventario if str(p.get("id")) not in excluir_ids]
    if tipo_op == "venta": props = [p for p in props if p.get("precio_venta_float", 0) > 0]
    elif tipo_op == "alquiler": props = [p for p in props if p.get("precio_renta_float", 0) > 0]
    if tipo_prop: props = [p for p in props if coincide_tipo_propiedad(p, tipo_prop)]
    if zona: props = [p for p in props if zona_coincide(zona, p.get("zona", ""), p.get("ciudad", ""))]
    if presupuesto is not None:
        props = [p for p in props if 0 < (p.get("precio_venta_float", 0) if tipo_op == "venta" else p.get("precio_renta_float", 0)) <= presupuesto]
    
    def score(p):
        pts = 0
        if zona and zona_coincide(zona, p.get("zona", ""), p.get("ciudad", "")): pts += 8
        if tipo_prop and coincide_tipo_propiedad(p, tipo_prop): pts += 6
        return pts

    ordenadas = sorted(props, key=score, reverse=True)[:n]
    for p in ordenadas: p["operacion_buscada"] = tipo_op
    return ordenadas

def formatear_ficha_propiedad(propiedad: dict, es_colega=False) -> str:
    op = propiedad.get("operacion_buscada", "venta")
    precio_val = propiedad.get("precio_renta_float", 0) if op == "alquiler" else propiedad.get("precio_venta_float", 0)
    precio_str = formato_moneda(precio_val) if precio_val > 0 else propiedad.get("renta" if op == "alquiler" else "venta", "N/D")
    area = propiedad.get("area", "N/D")
    area_txt = f"{area} m²" if str(area).strip() not in {"", "N/D", "None"} else "N/D"
    
    enlace = propiedad.get('enlace', '#')

    lineas = [
        f"*{propiedad.get('titulo', 'Propiedad')}*",
        f"📍 Zona: {propiedad.get('zona', 'N/D')}, {propiedad.get('ciudad', 'N/D')}",
        f"💰 Precio: {precio_str}",
        f"📐 Área: {area_txt} | 🛏️ Habs: {propiedad.get('habitaciones', 'N/D')} | 🛁 Baños: {propiedad.get('banos', 'N/D')}",
        f"🔗 {enlace}"
    ]
    if es_colega:
        captador = propiedad.get("captador_propiedad", "N/D")
        tel = obtener_telefono_captador_de_sheet(captador)
        lineas.extend([f"👤 Captador: {captador}", f"📲 WhatsApp: {tel if tel != 'N/D' else propiedad.get('telefono_captador_wasi', 'N/D')}"])
    return "\n".join(lineas)

def construir_resumen_necesidad(filtros: dict) -> str:
    p = []
    if filtros.get("tipo_propiedad"): p.append(f"- Tipo: {filtros['tipo_propiedad']}")
    if filtros.get("tipo_operacion"): p.append(f"- Operación: {filtros['tipo_operacion']}")
    if filtros.get("zona"): p.append(f"- Zona de interés: {filtros['zona']}")
    if filtros.get("presupuesto"): p.append(f"- Presupuesto máximo: {formato_moneda(filtros['presupuesto'])}")
    if filtros.get("habitaciones"): p.append(f"- Habitaciones/Baños: {filtros['habitaciones']}")
    if filtros.get("caracteristicas"): p.append(f"- Características Especiales: {filtros['caracteristicas']}")
    return "\n".join(p) if p else "- Búsqueda general o amplia"

# ============================================================
# TELEGRAM NOTIFICACIÓN
# ============================================================

def enviar_notificaciones_telegram(agente, lead: dict, resumen_necesidad: str):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not telegram_token: return
    admin_ids = [x.strip() for x in os.getenv("TELEGRAM_ADMIN_IDS", os.getenv("TELEGRAM_ADMIN_ID", "")).split(",") if x.strip()]
    nombre_cliente = capitalizar_nombre(lead.get("nombre", "")) or "Cliente Interesado"
    correo, whatsapp = lead.get("correo", "N/D"), limpiar_telefono(lead.get("whatsapp", ""))
    link_wa = f"https://wa.me/{whatsapp}" if whatsapp else "N/D"
    nombre_agente = agente.get("nombre", "Sin asignar") if agente else "Asesor de Turno"

    base_msg = f"Cliente: {nombre_cliente}\nCorreo: {correo}\nWhatsApp: {whatsapp}\nEnlace directo: {link_wa}\n\n📋 RESUMEN DE NECESIDAD:\n{resumen_necesidad}"
    url_tg = f"https://api.telegram.org/bot{telegram_token}/sendMessage"

    def enviar(chat_id: str, msg: str):
        try: requests.post(url_tg, json={"chat_id": chat_id, "text": msg}, timeout=8)
        except Exception as e: logger.error(f"Error Telegram: {e}")

    if agente and agente.get("telegram_id"): enviar(str(agente["telegram_id"]).strip(), f"👤 ¡Tienes un nuevo lead asignado!\n\n{base_msg}")
    for admin_id in admin_ids:
        if admin_id.lstrip("-").isdigit(): enviar(admin_id, f"👁️ REPORTE GLOBAL ADMIN\n🏢 Asesor Asignado: {nombre_agente}\n\n{base_msg}")

# ============================================================
# FASTAPI WEBHOOK CORE - ENRUTADOR PRINCIPAL
# ============================================================

@app.on_event("startup")
async def startup():
    await garantizar_inventario_actualizado(force=True)
    await asyncio.to_thread(sincronizar_google_sheet)

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        if request.headers.get("x-api-key") not in [k.strip() for k in os.getenv("API_KEYS_AGENTES", "").split(",") if k.strip()]:
            raise HTTPException(status_code=403, detail="Acceso denegado")

        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = str(payload.get("sender", "")).strip()
        mensaje_cliente = str(payload.get("message", "")).strip()

        if not mensaje_cliente: return {"replies": []}

        # Detección Determinística Pre-IA (Protección para Colegas)
        palabras_colega = ["colega", "agente", "inmobiliaria", "inmobiliario", "broker", "asesor", "corredor", "realtor", "remax", "rentahouse"]
        es_colega = any(palabra in mensaje_cliente.lower() for palabra in palabras_colega)

        lock = obtener_lock_usuario(sender)
        async with lock:
            estado = obtener_estado_usuario(sender)
            
            if es_colega:
                estado["rol"] = "colega_inmobiliario"
            elif not estado.get("rol"):
                estado["rol"] = "cliente"

            ahora_ts = time.time()

            if mensaje_cliente == estado.get("mensaje_previo", "") and (ahora_ts - float(estado.get("ultimo_mensaje_ts", 0.0))) <= VENTANA_MENSAJE_DUPLICADO_SEGUNDOS:
                return {"replies": []}

            estado["mensaje_previo"] = mensaje_cliente
            estado["ultimo_mensaje_ts"] = ahora_ts

            if not cache["inventario"]: await garantizar_inventario_actualizado(force=True)
            elif necesita_actualizar_inventario(): asyncio.create_task(garantizar_inventario_actualizado(force=False))
            sincronizar_google_sheet()

            # Capa 1: Extractor Analítico (Detecta intención, lead y códigos de Ads)
            analisis = await asyncio.to_thread(analizar_mensaje_ia, mensaje_cliente, estado, memoria_conversaciones.get(sender, []))
            intencion = analisis.get("intencion", "otro")
            codigo_inmueble = analisis.get("codigo_inmueble", "")

            estado["lead"] = fusionar_lead(estado["lead"], analisis.get("lead", {}), mensaje_cliente, sender)
            if estado["estado"] != ESTADO_CAPTURANDO_LEAD and not mensaje_parece_contacto(mensaje_cliente):
                estado["filtros"] = fusionar_filtros(estado["filtros"], analisis.get("filtros", {}), mensaje_cliente)

            contexto_sistema = {
                "rol_detectado": estado["rol"], "faltantes_busqueda": [],
                "propiedades_encontradas_texto": "", "faltantes_lead": [], "agente_asignado": "",
                "mensaje_interno": ""
            }

            # =====================================================
            # RUTAS DE FLUJO (ENRUTAMIENTO PRINCIPAL)
            # =====================================================
            
            # Búsqueda de Inmueble Específico (Ads / Mercadolibre / Código)
            propiedad_especifica = None
            if codigo_inmueble or "mercadolibre.com.ve" in mensaje_cliente.lower():
                id_buscado = re.sub(r"\D", "", str(codigo_inmueble)) if codigo_inmueble else None
                # Si el usuario mandó enlace crudo, buscamos el ID en el texto
                if not id_buscado: 
                    match_ml = re.search(r"MLV-?(\d+)", mensaje_cliente.upper())
                    if match_ml: id_buscado = match_ml.group(1)
                
                if id_buscado:
                    for p in cache["inventario"]:
                        # Wasi IDs o ML IDs simulados
                        if id_buscado in p["id"] or id_buscado in p.get("enlace", ""):
                            propiedad_especifica = p
                            break

            # --- RUTA 1: ADS / INMUEBLE ESPECÍFICO ---
            if propiedad_especifica and estado["estado"] == ESTADO_DIAGNOSTICO_IA:
                # Saltar embudo diagnóstico, mostrar directo
                estado["propiedades_enviadas"].append(propiedad_especifica["id"])
                contexto_sistema["propiedades_encontradas_texto"] = formatear_ficha_propiedad(propiedad_especifica, estado["rol"] == "colega_inmobiliario")
                estado["estado"] = ESTADO_MOSTRANDO_PROPIEDADES
                contexto_sistema["mensaje_interno"] = "El cliente preguntó por un inmueble específico. Se encontró y se muestra arriba. Ofrécele más información o agendar visita."

            # --- RUTA 2: DIAGNÓSTICO (CLIENTE GENÉRICO O COLEGA) ---
            elif estado["estado"] == ESTADO_DIAGNOSTICO_IA:
                filtros_requeridos = ["tipo_operacion", "tipo_propiedad", "zona", "presupuesto", "habitaciones", "caracteristicas"]
                faltantes = [k for k in filtros_requeridos if not estado["filtros"].get(k)]
                
                if faltantes:
                    # Obligamos a pedir paso a paso el embudo
                    contexto_sistema["faltantes_busqueda"] = [faltantes[0]]
                else:
                    # Todos los datos recopilados -> Buscar propiedades
                    props = elegir_top_n_propiedades(cache["inventario"], estado["filtros"], n=3, excluir_ids=estado["propiedades_enviadas"])
                    if props:
                        estado["propiedades_enviadas"].extend([p.get("id") for p in props if p.get("id")])
                        contexto_sistema["propiedades_encontradas_texto"] = "\n\n".join([formatear_ficha_propiedad(p, estado["rol"] == "colega_inmobiliario") for p in props])
                        estado["estado"] = ESTADO_MOSTRANDO_PROPIEDADES
                    else:
                        contexto_sistema["mensaje_interno"] = "No se encontraron propiedades con estos filtros exactos. Pide amablemente ajustar los requerimientos (ej. otra zona o mayor presupuesto)."

            # --- ESTADO: MOSTRANDO PROPIEDADES ---
            elif estado["estado"] == ESTADO_MOSTRANDO_PROPIEDADES:
                if intencion in ["mas_opciones", "ajustar_busqueda"]:
                    props = elegir_top_n_propiedades(cache["inventario"], estado["filtros"], n=3, excluir_ids=estado["propiedades_enviadas"])
                    if props:
                        estado["propiedades_enviadas"].extend([p.get("id") for p in props if p.get("id")])
                        contexto_sistema["propiedades_encontradas_texto"] = "\n\n".join([formatear_ficha_propiedad(p, estado["rol"] == "colega_inmobiliario") for p in props])
                elif (intencion in ["interes_propiedad", "enviar_datos"] or "interesa" in normalizar_texto(mensaje_cliente)):
                    # RUTA 3: COLEGA NUNCA PASA A LEAD. CLIENTE SÍ.
                    if estado["rol"] == "cliente":
                        estado["estado"] = ESTADO_CAPTURANDO_LEAD
                    else:
                        contexto_sistema["mensaje_interno"] = "El colega mostró interés. Ofrécele coordinar una visita con el captador o enviarle más opciones. NO LE PIDAS DATOS PERSONALES."

            # --- ESTADO: CAPTURANDO LEAD (SOLO CLIENTES) ---
            if estado["estado"] == ESTADO_CAPTURANDO_LEAD and estado["rol"] == "cliente":
                faltante_lead = None
                if not es_nombre_persona_valido(estado["lead"].get("nombre", "")): faltante_lead = "Nombre Completo"
                elif not estado["lead"].get("correo"): faltante_lead = "Correo electrónico"
                elif not estado["lead"].get("whatsapp"): faltante_lead = "Número de WhatsApp"
                
                if faltante_lead:
                    contexto_sistema["faltantes_lead"] = [faltante_lead]
                else:
                    agente = asignar_agente_round_robin()
                    enviar_notificaciones_telegram(agente, estado["lead"], construir_resumen_necesidad(estado["filtros"]))
                    contexto_sistema["agente_asignado"] = agente["nombre"] if agente else "un asesor experto"
                    clientes_procesados.add(sender)
                    estado["estado"] = ESTADO_LEAD_COMPLETO

            # Capa 3: PATY CONVERSACIONAL (Motor de Generación de Respuesta)
            respuesta_paty = await asyncio.to_thread(generar_respuesta_conversacional_paty, mensaje_cliente, contexto_sistema, memoria_conversaciones.get(sender, []))

            actualizar_memoria(sender, mensaje_cliente, respuesta_paty)
            return {"replies": [{"message": respuesta_paty.replace("**", "*")}]}

    except HTTPException: raise
    except Exception as e:
        logger.error(f"Error crítico webhook: {e}", exc_info=True)
        return {"replies": [{"message": "Lo siento, mi sistema está procesando tu solicitud... 🙏"}]}
