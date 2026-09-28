"""Reglas compartidas para descubrir necesidades antes de listar inventario."""
import re
import unicodedata


PROFILE_FIELDS = {"tipo_operacion", "tipo_propiedad", "ciudad", "zona",
                  "presupuesto_max", "preferencias_busqueda"}


def normalized(text):
    return "".join(c for c in unicodedata.normalize("NFD", str(text).lower())
                   if unicodedata.category(c) != "Mn")


def missing_profile(state):
    filters = state.get("filtros") or {}
    open_fields = set(state.get("sin_preferencia") or [])
    missing = [key for key in ("tipo_operacion", "tipo_propiedad", "ciudad")
               if not filters.get(key)]
    for key in ("zona", "presupuesto_max"):
        if not filters.get(key) and key not in open_fields:
            missing.append(key)
    return missing


def profile_question(state):
    missing = missing_profile(state)
    filters = state.get("filtros") or {}
    questions = {
        "tipo_operacion": "¿La propiedad sería para comprar o alquilar?",
        "tipo_propiedad": "¿Qué tipo de inmueble necesitas: casa, apartamento, local u otro?",
        "ciudad": "📍 ¿En qué ciudad y zona te gustaría buscar?",
        "zona": "📍 ¿Qué zona de {} prefieres? También puedes indicar cualquier zona.".format(filters.get("ciudad")),
        "presupuesto_max": "💰 ¿Cuál es tu presupuesto máximo aproximado para el precio total, en dólares? Si aún no lo has definido, dímelo.",
    }
    question = ""
    if missing:
        key = missing[0]
        state["pregunta_pendiente"] = key
        question = questions[key]
        if key in {"ciudad", "zona"} and "presupuesto_max" in missing:
            question += " ¿Y qué presupuesto aproximado manejas?"

    residential = filters.get("tipo_propiedad") in {"casa", "apartamento", "townhouse", "quinta", "penthouse"}
    preferences = any(filters.get(k) is not None for k in
                      ("habitaciones_min", "banos_min", "garajes_min")) or bool(filters.get("caracteristicas"))
    if not state.get("perfil_preferencias_consultadas") and not preferences and (not missing or missing[0] in {"ciudad", "zona", "presupuesto_max"}):
        question += ("\n\nSi tienes preferencias de habitaciones, baños, estacionamiento o alguna característica indispensable, cuéntamelas también. Son opcionales."
                     if residential else "\n\n¿Hay alguna superficie o característica indispensable? Si no, podemos empezar con esos criterios.")
        state["perfil_preferencias_consultadas"] = True
        if not missing:
            state["pregunta_pendiente"] = "preferencias_busqueda"
    if question:
        state["estado_conversacion"] = "perfilando_busqueda"
        state["objetivo"] = "perfilar_busqueda"
        state["ultima_intencion"] = "busqueda_propiedad"
        state["ultima_senal_comercial"] = "interesado" if state.get("rol") == "cliente" else "ninguna"
        state["siguiente_paso_comercial"] = "perfilar_busqueda"
    else:
        state["pregunta_pendiente"] = None
    return question.strip()


NUMBER = r"(?:\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)(?:\s*(?:mil|k))?"
AMOUNT = r"(?:\$\s*|usd\s*)?(?P<amount>" + NUMBER + r")(?:\s*(?:usd|dolares|\$))?"
INITIAL_PATTERNS = [
    re.compile(AMOUNT + r"\s*(?:disponibles?\s*)?(?:(?:de|para)\s+(?:la\s+)?)?inicial\b"),
    re.compile(r"\binicial\s*(?:(?:disponible|de|es|hasta)\s*)?[:=]?\s*" + AMOUNT),
]


def split_down_payment(text):
    """Devuelve texto sin el importe de inicial y ese importe, sin confundir el precio total."""
    clean = normalized(text)
    initial = None
    spans = []
    for pattern in INITIAL_PATTERNS:
        for match in pattern.finditer(clean):
            raw = match.group("amount").replace(" ", "")
            factor = 1000 if raw.endswith(("mil", "k")) else 1
            raw = re.sub(r"(?:mil|k)$", "", raw)
            if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
                raw = raw.replace(".", "").replace(",", "")
            else:
                raw = raw.replace(",", ".")
            initial = float(raw) * factor
            spans.append(match.span())
    for start, end in sorted(spans, reverse=True):
        clean = clean[:start] + " " * (end - start) + clean[end:]
    return clean, initial


def operational_reply(state, message):
    text = normalized(message)
    if (re.search(r"\b(?:graba(?:me|rme|r)|agrega(?:me|rme|r)|guarda(?:me|rme|r))\b", text)
            and any(word in text for word in ("agenda", "contacto", "numero"))):
        return "Puedo atenderte por aquí, pero no tengo una función para agregar números a la agenda de WhatsApp. ¿Qué información necesitas compartir o consultar?"
    if re.fullmatch(r"[\W_]*(?:envio una foto|envio una imagen|multimedia_sin_texto)[\W_]*", text):
        return "Recibí el aviso de una imagen, pero no puedo identificar el inmueble a partir de ese aviso. Envíame el código o enlace de la propiedad, o descríbeme qué necesitas."
    if any(p in text for p in ("no me abre el link", "no abre el enlace", "no abre el link", "no funciona el enlace")):
        prop = state.get("propiedad_interes") or {}
        code = prop.get("id") or state.get("propiedad_activa_id") or state.get("ultima_propiedad_consultada_id")
        if code:
            return (f"Puedes intentar abrir este enlace directamente en el navegador:\nhttps://www.mettryc.com/inmueble/{code}\n\n"
                    f"El código es {code}. Si sigue fallando, puedo darte la información por aquí o ayudarte a contactar a un asesor.")
        return "¿Qué enlace no abre? Compárteme el enlace o el código para ubicar la propiedad y darte la información por aquí."
    if state.get("pregunta_pendiente") == "preferencias_busqueda" and text.strip() in {"?", "??", "???", "¿?", "como", "que", "no entiendo"}:
        return "Me refiero a lo que necesitas del inmueble: por ejemplo, habitaciones, baños, estacionamiento o alguna característica indispensable. Puedes decirme que no tienes preferencias y te muestro opciones."
    if state.get("pregunta_pendiente") == "confirmar_rol" and text.strip() in {"?", "??", "???", "¿?", "como", "que", "no entiendo"}:
        return "Te lo pregunto para adaptar la atención: ¿la propiedad es para ti o estás ayudando a un cliente como asesor inmobiliario?"
    return None


def pending_visit_invitation(state):
    last = next((item.get("content", "") for item in reversed(state.get("historial", []))
                 if item.get("role") == "assistant"), "")
    text = normalized(last)
    return bool((state.get("ultimo_lote") or state.get("propiedad_interes"))
                and any(p in text for p in ("coordinamos una visita", "quieres agendar una visita", "te gustaria agendar una visita"))
                and " o " not in text)
