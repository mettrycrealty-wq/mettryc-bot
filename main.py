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
# CONFIGURACIÓN Y MODELOS EN CASCADA
# ============================================================

HORARIOS_ACTUALIZACION_INVENTARIO = [0, 12]

MODELO_PRINCIPAL = os.getenv("MODELO_PRINCIPAL", "google/gemini-2.5-flash-lite")
MODELO_RESPALDO_1 = os.getenv("MODELO_RESPALDO_1", "anthropic/claude-3-haiku")
MODELO_RESPALDO_2 = os.getenv("MODELO_RESPALDO_2", "openai/gpt-4o-mini")
MAX_TOKENS_IA = int(os.getenv("MAX_TOKENS_IA", "600"))

INTERVALO_ACTUALIZACION_SHEETS = timedelta(hours=1)
VENTANA_MENSAJE_DUPLICADO_SEGUNDOS = int(os.getenv("VENTANA_MENSAJE_DUPLICADO_SEGUNDOS", "25"))

# ============================================================
# CACHÉ Y MEMORIA PERSISTENTE
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

# BASE DE DATOS SIMULADA PARA CLIENTES
clientes_db: Dict[str, dict] = {}
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

TOKENS_DIRECCION_ZONA = {"norte", "sur", "este", "oeste", "centro"}
SINONIMOS_TIPO = {"tohouse": "townhouse", "towhouse": "townhouse", "twhouse": "townhouse", "town house": "townhouse", "aparto quinta": "apartoquinta", "aptoquinta": "apartoquinta", "apartoquita": "apartoquinta"}

def coincide_tipo_propiedad(propiedad: dict, tipo_buscado: str) -> bool:
    tipo_wasi = normalizar_tipo_propiedad(propiedad.get("tipo_propiedad_wasi", ""))
    tipo_buscado_norm = normalizar_tipo_propiedad(tipo_buscado)
    return tipo_buscado_norm in tipo_wasi or tipo_wasi in tipo_buscado_norm

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
    palabras = [p for p in re.findall(r"[A-Za-zÀ-ÖØ-ÿ'´-]+", nombre) if normalizar_texto(p) not in {"la", "el", "que", "hola", "soy", "me", "llamo", "mi", "nombre", "es"} and len(p) >= 2]
    return 2 <= len(palabras) <= 4

def parsear_presupuesto_texto(texto: str):
    if not texto: return None
    t = normalizar_texto(texto)
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
    return {t for t in normalizar_texto(texto).split() if len(t) > 2}

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
        nombre_guardado = clientes_db.get(sender, {}).get("nombre", "")
        estado_usuarios[sender] = {
            "estado": ESTADO_DIAGNOSTICO_IA, "rol": None,
            "filtros": {"tipo_propiedad": "", "tipo_operacion": "", "zona": "", "presupuesto": None, "habitaciones": "", "caracteristicas": ""},
            "propiedades_enviadas": [], 
            "lead": {"nombre": nombre_guardado, "correo": "", "whatsapp": sender},
            "ultima_respuesta": "", "mensaje_previo": "", "ultimo_mensaje_ts": 0.0
        }
    return estado_usuarios[sender]

def obtener_lock_usuario(sender: str) -> asyncio.Lock:
    if sender not in locks_usuarios: locks_usuarios[sender] = asyncio.Lock()
    return locks_usuarios[sender]

# ============================================================
# INVENTARIO WASI Y GOOGLE SHEETS (Con protección Anti-Timeout)
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
    propiedades, take, skip = [], 50, 0 # Modificado a 50 para evitar errores 502
    intentos_maximos = 3
    while True:
        params = {"wasi_token": os.getenv("WASI_TOKEN"), "id_company": os.getenv("WASI_COMPANY_ID"), "take": take, "skip": skip, "status": 1}
        exito = False
        
        for intento in range(intentos_maximos):
            try:
                response = requests.get("https://api.wasi.co/v1/property/search", params=params, timeout=40)
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
                exito = True
                if contador < take: return propiedades
                skip += take
                time.sleep(1.5)
                break
            except Exception as e:
                logger.warning(f"Intento {intento+1} fallido para Wasi: {e}")
                time.sleep(2 * (intento + 1))
                
        if not exito:
            logger.error("Error definitivo tras varios intentos con Wasi.")
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
    return "N/D"

# ============================================================
# OPENROUTER IA 
# ============================================================

def consultar_ia(mensajes: list, max_tokens: int = MAX_TOKENS_IA, fallback: str = "", modelos_personalizados: list = None) -> str:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key: return fallback
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "HTTP-Referer": "https://www.mettryc.com", "X-Title": "Mettryc Bot"}
    modelos_en_cascada = modelos_personalizados if modelos_personalizados else [MODELO_PRINCIPAL, MODELO_RESPALDO_1, MODELO_RESPALDO_2]
    for modelo in modelos_en_cascada:
        try:
            resp = requests.post(url, headers=headers, json={"model": modelo, "messages": mensajes, "max_tokens": max_tokens, "temperature": 0.2}, timeout=15)
            resp.raise_for_status()
            c = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            if c: return c.strip()
        except Exception: continue
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
# ARQUITECTURA: IA ANALÍTICA + IA CONVERSACIONAL
# ============================================================

def analizar_mensaje_ia(mensaje_usuario: str, estado: dict, historial: list) -> dict:
    historial_breve = [{"role": h.get("role", "user"), "content": h.get("content", "")[:220]} for h in historial[-8:]]
    system = "Eres un analista experto. Responde UNICAMENTE con un objeto JSON válido según el esquema."
    user_prompt = f"""
    Estado actual: {json.dumps({"filtros": estado.get("filtros"), "lead": estado.get("lead"), "rol": estado.get("rol")}, ensure_ascii=False)}
    Mensaje actual del usuario: "{mensaje_usuario}"

    Reglas:
    1. intencion: "saludo"|"buscar"|"mas_opciones"|"ajustar_busqueda"|"interes_propiedad"|"enviar_datos"|"objecion"|"consulta_inmueble_especifico"
    2. rol: "colega_inmobiliario" si se identifica como colega. De lo contrario "cliente".
    3. codigo_inmueble: Extrae el código si el usuario pregunta por un anuncio o inmueble específico.
    4. lead: extrae nombre, correo, whatsapp de forma precisa. NO inventes nombres basados en verbos.

    Devuelve EXACTAMENTE este JSON:
    {{ "intencion": "...", "rol": "cliente|colega_inmobiliario", "codigo_inmueble": "", "filtros": {{"tipo_propiedad": "", "tipo_operacion": "", "zona": "", "presupuesto": null, "habitaciones": "", "caracteristicas": ""}}, "lead": {{"nombre": "", "correo": "", "whatsapp": ""}} }}
    """
    
    respuesta_cruda = consultar_ia([{"role": "system", "content": system}] + historial_breve + [{"role": "user", "content": user_prompt}], max_tokens=350, fallback="{}")
    json_extraido = extraer_json_de_texto(respuesta_cruda)
    
    # MODO RAYOS X (Logs Analíticos para Render)
    logger.info("\n" + "="*40)
    logger.info("🧠 ANALÍTICA IA (CAPA 1) 🧠")
    logger.info(f"Mensaje del Cliente: {mensaje_usuario}")
    logger.info(f"JSON Extraído:\n{json.dumps(json_extraido, ensure_ascii=False, indent=2)}")
    logger.info("="*40 + "\n")
    
    return json_extraido

CONOCIMIENTO_METTRYC = """
- Empresa: Mettryc Realty, Primera Tecnoinmobiliaria de Venezuela.
- Ubicación: Valencia, Carabobo, CC Patio Trigal Local 300-6. GPS: https://maps.app.goo.gl/dSofuNmF89vNLv7X8
- Honorarios: 5% venta, 1 mes alquiler (Según Cámara Inmobiliaria de Venezuela).
- Reclutamiento: El ingreso cuesta 50$ (curso+credenciales). Formulario: https://forms.gle/SbLtHrey69fhf3Xt8
- Negociación: Si preguntan si es negociable, responde: "Sería cuestión de que haga su mejor oferta y se la presentaremos al propietario".
- Contacto Directo: NUNCA des información de contacto del propietario. Di que un agente le contactará.
- Colegas Inmobiliarios: Si el rol es "colega_inmobiliario", NO pidas datos de sus clientes.
- Agradecimientos: Si el usuario dice "gracias", responde "¡Siempre a la orden! ☺️".
"""

# ============================================================
# SOLUCIÓN FINAL: Paty, la Asistente VIP Inteligente
# ============================================================

# [PEGA TODO TU CÓDIGO AQUÍ, PERO REEMPLAZA LA FUNCIÓN GENERAR_RESPUESTA_CONVERSACIONAL_PATY CON ESTA:]

def generar_respuesta_conversacional_paty(mensaje_usuario: str, contexto_sistema: dict, historial: list) -> str:
    # Preparamos los datos para la IA
    faltantes = contexto_sistema.get('faltantes_busqueda', [])
    faltantes_lead = contexto_sistema.get('faltantes_lead', [])
    dato_a_preguntar = faltantes[0] if faltantes else (faltantes_lead[0] if faltantes_lead else "Ninguno")
    
    prompt_sistema = f"""
    Eres Paty, asistente VIP de Mettryc Realty (Valencia).
    
    ESTADO DE LA CONVERSACIÓN:
    - Cliente: {contexto_sistema.get('nombre_cliente', 'Desconocido')}
    - Datos Confirmados: {', '.join(contexto_sistema.get('datos_confirmados', []))}
    - DATO A PREGUNTAR AHORA: {dato_a_preguntar}
    - Propiedades disponibles: {contexto_sistema.get('propiedades_encontradas_texto', 'Ninguna')}

    REGLAS:
    1. RESPUESTA LÓGICA: Si el usuario acaba de escribir algo (ej: "3 habitaciones"), confirma brevemente (Ej: "¡Entendido, 3 habitaciones!") antes de pedir el siguiente dato.
    2. BREVEDAD: Máximo 30 palabras. No uses plantillas.
    3. PROHIBIDO REPETIR: NUNCA preguntes por datos que ya están en "Datos Confirmados".
    4. UNA PREGUNTA: Solo pregunta por el "DATO A PREGUNTAR AHORA".
    5. INFO COMERCIAL: Si preguntan honorarios di 5% venta/1 mes alquiler. Dirección: CC Patio Trigal.
    6. FORMATEO: Enlaces web en texto plano (sin corchetes).
    """
    
    # Reducimos el historial para que la IA no se confunda con mensajes de hace 10 minutos
    mensajes = [{"role": "system", "content": prompt_sistema}] + (historial[-4:] if len(historial) > 4 else historial)
    
    return consultar_ia(mensajes, max_tokens=600, modelos_personalizados=[MODELO_RESPALDO_1, MODELO_RESPALDO_2])


# ============================================================
# LÓGICA DE PYTHON (Integridad de Datos)
# ============================================================

def fusionar_filtros(base: dict, nuevos: dict, mensaje: str) -> dict:
    out = dict(base or {})
    if nuevos.get("tipo_propiedad") and not out.get("tipo_propiedad"): out["tipo_propiedad"] = normalizar_tipo_propiedad(nuevos["tipo_propiedad"])
    if nuevos.get("tipo_operacion") and not out.get("tipo_operacion"): out["tipo_operacion"] = normalizar_operacion(nuevos["tipo_operacion"])
    if nuevos.get("zona") and not out.get("zona"): out["zona"] = str(nuevos["zona"]).strip()
    if nuevos.get("habitaciones") and not out.get("habitaciones"): out["habitaciones"] = str(nuevos["habitaciones"]).strip()
    if nuevos.get("caracteristicas") and not out.get("caracteristicas"): out["caracteristicas"] = str(nuevos["caracteristicas"]).strip()
    
    precio_txt = parsear_presupuesto_texto(mensaje)
    if precio_txt and not out.get("presupuesto"):
        out["presupuesto"] = precio_txt
    elif nuevos.get("presupuesto") and not out.get("presupuesto"):
        try: out["presupuesto"] = float(nuevos["presupuesto"])
        except Exception: pass
        
    return out

def fusionar_lead(base: dict, nuevos: dict, mensaje: str, sender: str) -> dict:
    out = dict(base or {})
    
    if nuevos.get("nombre") and not out.get("nombre"):
        cand = capitalizar_nombre(str(nuevos["nombre"]).strip())
        if len(cand) > 2:
            out["nombre"] = cand
            clientes_db[sender] = {"nombre": cand}
            
    if nuevos.get("correo") and not out.get("correo"):
        correo = str(nuevos["correo"]).strip().lower()
        if "@" in correo and "." in correo: out["correo"] = correo
        
    if not out.get("whatsapp"):
        out["whatsapp"] = sender

    m_mail = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", mensaje, re.IGNORECASE)
    if m_mail and not out.get("correo"): out["correo"] = m_mail.group(0).strip().lower()

    return out

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
# DIRECTOR DE TRÁFICO (Webhook)
# ============================================================

@app.on_event("startup")
async def startup():
    await garantizar_inventario_actualizado(force=False) # Modificado a False para evitar timeouts al reiniciar Render
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

            # 1. ANÁLISIS DE IA (Modo Rayos X incluido)
            analisis = await asyncio.to_thread(analizar_mensaje_ia, mensaje_cliente, estado, memoria_conversaciones.get(sender, []))
            intencion = analisis.get("intencion", "otro")
            codigo_inmueble = analisis.get("codigo_inmueble", "")

            # 2. BLOQUEO DE DATOS (Fusionamos protegiendo lo ya existente)
            estado["lead"] = fusionar_lead(estado["lead"], analisis.get("lead", {}), mensaje_cliente, sender)
            if estado["estado"] != ESTADO_CAPTURANDO_LEAD and not mensaje_parece_contacto(mensaje_cliente):
                estado["filtros"] = fusionar_filtros(estado["filtros"], analisis.get("filtros", {}), mensaje_cliente)

            # 3. LISTADO DE DATOS CONFIRMADOS PARA LA IA
            datos_confirmados_lista = [k for k, v in estado["filtros"].items() if v]
            if estado["lead"].get("nombre"): datos_confirmados_lista.append("nombre")
            
            contexto_sistema = {
                "rol_detectado": estado["rol"], "faltantes_busqueda": [],
                "propiedades_encontradas_texto": "", "faltantes_lead": [], "agente_asignado": "",
                "mensaje_interno": "", "nombre_cliente": estado["lead"].get("nombre", ""),
                "datos_confirmados": datos_confirmados_lista
            }

            # =====================================================
            # ENRUTADOR PRINCIPAL
            # =====================================================
            
            propiedad_especifica = None
            if codigo_inmueble or "mercadolibre.com.ve" in mensaje_cliente.lower():
                id_buscado = re.sub(r"\D", "", str(codigo_inmueble)) if codigo_inmueble else None
                if not id_buscado: 
                    match_ml = re.search(r"MLV-?(\d+)", mensaje_cliente.upper())
                    if match_ml: id_buscado = match_ml.group(1)
                
                if id_buscado:
                    for p in cache["inventario"]:
                        if id_buscado in p["id"] or id_buscado in p.get("enlace", ""):
                            propiedad_especifica = p
                            break

            # --- RUTA 1: MODO INTERRUPCIÓN (ADS / CÓDIGO) ---
            if propiedad_especifica and estado["estado"] == ESTADO_DIAGNOSTICO_IA:
                estado["propiedades_enviadas"].append(propiedad_especifica["id"])
                contexto_sistema["propiedades_encontradas_texto"] = formatear_ficha_propiedad(propiedad_especifica, estado["rol"] == "colega_inmobiliario")
                estado["estado"] = ESTADO_MOSTRANDO_PROPIEDADES
                contexto_sistema["mensaje_interno"] = "MODO INTERRUPCIÓN: El cliente preguntó por un inmueble específico. Se muestra arriba. Ofrécele más información o agendar visita de inmediato."

            # --- RUTA 2: MODO DIAGNÓSTICO (EMBUDO) ---
            elif estado["estado"] == ESTADO_DIAGNOSTICO_IA:
                filtros_requeridos = ["tipo_operacion", "tipo_propiedad", "zona", "presupuesto", "habitaciones", "caracteristicas"]
                faltantes = [k for k in filtros_requeridos if not estado["filtros"].get(k)]
                
                if faltantes:
                    contexto_sistema["faltantes_busqueda"] = [faltantes[0]]
                else:
                    props = elegir_top_n_propiedades(cache["inventario"], estado["filtros"], n=3, excluir_ids=estado["propiedades_enviadas"])
                    if props:
                        estado["propiedades_enviadas"].extend([p.get("id") for p in props if p.get("id")])
                        contexto_sistema["propiedades_encontradas_texto"] = "\n\n".join([formatear_ficha_propiedad(p, estado["rol"] == "colega_inmobiliario") for p in props])
                        estado["estado"] = ESTADO_MOSTRANDO_PROPIEDADES
                    else:
                        contexto_sistema["mensaje_interno"] = "No se encontraron propiedades con estos filtros exactos. Pide amablemente ajustar los requerimientos (ej. otra zona o mayor presupuesto)."

            # --- RUTA 3: MODO MOSTRANDO PROPIEDADES ---
            elif estado["estado"] == ESTADO_MOSTRANDO_PROPIEDADES:
                if intencion in ["mas_opciones", "ajustar_busqueda"]:
                    props = elegir_top_n_propiedades(cache["inventario"], estado["filtros"], n=3, excluir_ids=estado["propiedades_enviadas"])
                    if props:
                        estado["propiedades_enviadas"].extend([p.get("id") for p in props if p.get("id")])
                        contexto_sistema["propiedades_encontradas_texto"] = "\n\n".join([formatear_ficha_propiedad(p, estado["rol"] == "colega_inmobiliario") for p in props])
                elif (intencion in ["interes_propiedad", "enviar_datos"] or "interesa" in normalizar_texto(mensaje_cliente)):
                    if estado["rol"] == "cliente":
                        estado["estado"] = ESTADO_CAPTURANDO_LEAD
                    else:
                        contexto_sistema["mensaje_interno"] = "El colega mostró interés. Ofrécele coordinar una visita con el captador o enviarle más opciones. NO LE PIDAS DATOS PERSONALES."

            # --- RUTA 4: MODO CAPTURANDO LEAD ---
            if estado["estado"] == ESTADO_CAPTURANDO_LEAD and estado["rol"] == "cliente":
                faltante_lead = None
                if not estado["lead"].get("nombre") or len(estado["lead"].get("nombre", "")) < 3: faltante_lead = "Nombre Completo"
                elif not estado["lead"].get("correo"): faltante_lead = "Correo electrónico"
                
                if faltante_lead:
                    contexto_sistema["faltantes_lead"] = [faltante_lead]
                else:
                    agente = asignar_agente_round_robin()
                    enviar_notificaciones_telegram(agente, estado["lead"], construir_resumen_necesidad(estado["filtros"]))
                    contexto_sistema["agente_asignado"] = agente["nombre"] if agente else "un asesor experto"
                    clientes_procesados.add(sender)
                    estado["estado"] = ESTADO_LEAD_COMPLETO

            # 4. RESPUESTA FINAL
            respuesta_paty = await asyncio.to_thread(generar_respuesta_conversacional_paty, mensaje_cliente, contexto_sistema, memoria_conversaciones.get(sender, []))

            actualizar_memoria(sender, mensaje_cliente, respuesta_paty)
            return {"replies": [{"message": respuesta_paty.replace("**", "*")}]}

    except HTTPException: raise
    except Exception as e:
        logger.error(f"Error crítico webhook: {e}", exc_info=True)
        return {"replies": [{"message": "Lo siento, mi sistema está procesando tu solicitud... 🙏"}]}
