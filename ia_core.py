import os
import json
import logging
import re
import httpx
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, ValidationError


from utils import (
    extraer_codigo_inmueble,
    detectar_posicion,
    pide_mas_opciones,
    menciona_anuncio_sin_codigo,
    detectar_rol_explicito,
    limpiar_json_modelo
)

# Configuramos el logger
logger = logging.getLogger("mettryc-chatbot")

# ============================================================
# CONFIGURACIÓN DE OPENROUTER
# ============================================================

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

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

OPENROUTER_TIMEOUT = float(os.getenv("OPENROUTER_TIMEOUT", "30"))


class ActualizacionesConversacion(BaseModel):
    tipo_operacion: Optional[
        Literal["venta", "alquiler", "compra"]
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


async def llamar_openrouter_json(
    modelo_pydantic,
    mensajes: List[dict],
    temperatura: float = 0.2,
    client: Optional[httpx.AsyncClient] = None
):
    if not OPENROUTER_API_KEY:
        return None

    if client is None:
        from main import http_client
        client = http_client

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
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
                "max_tokens": 900,
                "response_format": response_format,
            }

            try:
                respuesta = await client.post(
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

                logger.info(f"🤖 [IA RAW RESPONSE] Éxito con {modelo}: {contenido[:200]}...")

                contenido = limpiar_json_modelo(contenido)

                return modelo_pydantic.model_validate_json(contenido)

            except ValidationError as exc:
                logger.warning(f"🤖 [IA ERROR FORMATO] JSON de la IA inválido: {exc}")
                
            except Exception as exc:
                logger.warning(f"🤖 [IA ERROR RED/API] Falla con {modelo}: {type(exc).__name__} - {str(exc)[:150]}")

    return None


def construir_estado_para_ia(estado: dict) -> dict:
    propiedad_interes = estado.get("propiedad_interes")

    return {
        "rol": estado.get("rol"),
        "confianza_rol": estado.get("confianza_rol"),
        "objetivo": estado.get("objetivo"),
        "filtros": estado.get("filtros"),
        "sin_preferencia": estado.get("sin_preferencia"),
        "esperando_codigo": estado.get("esperando_codigo"),
        "ultimo_lote": estado.get("ultimo_lote"),
        "propiedad_interes": (
            {
                "id": propiedad_interes.get("id"),
                "titulo": propiedad_interes.get("titulo"),
            }
            if propiedad_interes
            else None
        ),
        "lead": {
            "nombre": estado["lead"].get("nombre"),
            "correo": estado["lead"].get("correo"),
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
    client: Optional[httpx.AsyncClient] = None
) -> DecisionAgente:
    contexto = {
        "estado_comercial": construir_estado_para_ia(estado),
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
        client=client
    )

    if decision:
        return decision

    return decision_fallback(mensaje, estado)


def decision_fallback(mensaje: str, estado: dict) -> DecisionAgente:
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


async def redactar_resultado_ia(
    estado: dict,
    cantidad: int,
    aproximadas: int,
    especifica: bool = False,
    client: Optional[httpx.AsyncClient] = None
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
        client=client
    )

    if resultado:
        return resultado

    if especifica:
        introduccion = "Claro, esta es la propiedad que consultaste:"
    elif aproximadas:
        introduccion = (
            "Encontré estas opciones. Algunas son aproximadas, "
            "pero pueden valer la pena:"
        )
    else:
        introduccion = "Encontré estas opciones que encajan muy bien:"

    if rol == "colega_inmobiliario":
        cierre = (
            "Puedes contactar al captador indicado en cada ficha "
            "o pedirme más opciones."
        )
    else:
        cierre = "Dime cuál te interesa o escribe “más opciones”."

    return TextoResultado(
        introduccion=introduccion,
        cierre=cierre,
    )


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


async def humanizar_texto_con_ia(
    estado: dict, 
    instruccion_cruda: str, 
    mensaje_usuario: str,
    client: Optional[httpx.AsyncClient] = None
) -> str:
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
    
    if client is None:
        from main import http_client
        client = http_client

    try:
        resp = await client.post(
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
