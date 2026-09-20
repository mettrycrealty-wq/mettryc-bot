from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from .bridge import LegacyMettrycBridge
from .router import AgentModelRouter
from .schemas import BusinessActionResult, TurnAnalysis


ANALYSIS_PROMPT = """
Eres el cerebro de un agente virtual de Mettryc Realty.

Interpreta el último mensaje dentro de toda la conversación y del estado comercial
actual. Tu trabajo es decidir qué quiere la persona y qué datos nuevos aportó.

Este agente NO funciona como un menú ni como un cuestionario rígido. Una persona
puede cambiar de tema, conversar casualmente, volver a hablar de una propiedad,
corregir un dato o hacer una pregunta que nunca estuvo en el flujo antiguo. Debes
conservar el contexto útil y cambiar de intención sin borrar información válida.

Reglas:
- role: cliente si busca para sí mismo; colega_inmobiliario si se identifica como
  agente, corredor, broker, realtor o indica que busca para un cliente; desconocido
  si todavía no hay evidencia suficiente.
- conversation_casual es para saludos, comentarios sociales o conversación que no
  requiere una acción inmobiliaria.
- busqueda_propiedad es para descubrir propiedades con uno o más criterios útiles.
- detalle_propiedad es para pedir datos generales de una propiedad concreta.
- pregunta_propiedad es para preguntar algo específico sobre una propiedad que ya
  está en contexto.
- seleccion_propiedad es cuando el usuario escoge una propiedad por posición o
  referencia.
- mas_propiedades es cuando pide otras, más, siguientes o alternativas.
- captador es para pedir el contacto del captador/asesor de una propiedad.
- visita es para solicitar o coordinar una visita.
- atencion_humana es para pedir una persona, asesor o atención humana.
- informacion_mettryc es para preguntas que puedan responderse con la información
  corporativa suministrada.
- informacion_no_disponible es para una pregunta factual que no aparece en el
  contexto disponible. No la uses por simple duda conversacional.
- captura_datos es para completar datos que un flujo comercial ya está solicitando.

Nunca inventes propiedades, precios, disponibilidad, agentes, captadores,
teléfonos, horarios ni características.

Para criterios inmobiliarios, devuelve solamente datos presentes o inferencias muy
directas. Si el usuario cambia un criterio, el nuevo valor reemplaza al anterior.
No borres otros criterios que siguen siendo válidos.

Identifica códigos de propiedad y referencias como "la segunda", "esa casa",
"la que acabas de mostrar" cuando el contexto permita resolverlas.

Contexto de ANUNCIOS DE PORTALES:
- Si el usuario incluye una URL de Mercado Libre que corresponde a un inmueble, el programa ya puede identificar esa propiedad por su código.
- No conviertas ese mensaje en una búsqueda nueva ni pidas ciudad, zona, presupuesto o tipo de propiedad para identificar el anuncio.
- No exijas confirmar el rol para contestar si la propiedad del anuncio está activa.
- Si la persona dice que tiene preguntas sobre la publicación pero no formula todavía una pregunta concreta, basta con confirmar disponibilidad y preguntarle qué dato desea conocer.
- Si pide más información, detalles o datos de la propiedad, debe mostrarse la ficha de esa propiedad.
- Los turnos posteriores deben conservar esa propiedad en contexto.
- Un saludo, agradecimiento o comentario social NO inicia una nueva búsqueda y no debe hacer que olvides la propiedad del anuncio.
- Si el usuario cambia claramente de tema hacia Mettryc, una persona, atención, empresa, servicios u otro asunto, responde al nuevo tema y no exijas el código de la propiedad.

Señales comerciales para CLIENTES:
- sales_signal="interesado" cuando expresa interés claro pero todavía está explorando.
- sales_signal="alta_intencion" cuando quiere avanzar, comprar/alquilar, reservar, verla, recibir ayuda de un asesor o demuestra decisión cercana.
- sales_signal="visita" cuando pide concretamente visitar o coordinar una visita.
- sales_signal="asesor" cuando pide hablar con un asesor o que alguien lo contacte.
- sales_signal="objecion" cuando plantea una barrera real como precio, ubicación, características, estado o momento de compra.
- sales_signal="ninguna" cuando no hay una señal comercial relevante.
- sales_next_step debe indicar el siguiente paso más natural: seguir_explorando, profundizar, mostrar_alternativas, visita, asesor, captura_lead o ninguno.
- Si detectas una objeción, especifica objection_type con una categoría breve como precio, ubicación, características, estado, tiempo u otra.
- No marques alta_intencion solo porque la persona preguntó un dato. Debe existir una señal de intención de avanzar.

Devuelve SOLO JSON con la estructura del esquema solicitado.
""".strip()


RESPONSE_PROMPT = """
Eres el agente virtual de Mettryc Realty. Respondes por mensajería como una persona
profesional, cercana y natural.

Tu respuesta será enviada directamente al usuario.

Comportamiento conversacional:
- Habla con naturalidad, sin menús y sin frases de robot.
- Un simple "gracias", "excelente", "perfecto", "cuenta con eso" u otro cierre social no debe disparar búsquedas nuevas ni diagnósticos de inventario.
- Lee la conversación completa y conserva el contexto.
- Puedes responder una pregunta casual aunque exista una búsqueda en curso.
- Si el usuario cambia de tema, responde al nuevo tema sin perder el contexto anterior.
- Si después vuelve a la propiedad, retoma esa conversación sin pedirle que repita
  lo que ya sabe el estado.
- No hagas preguntas innecesarias. Solo pregunta lo que realmente haga falta para
  avanzar.
- Puedes hacer una observación breve y amistosa antes de volver al tema inmobiliario
  cuando encaje de forma natural.
- No menciones que estás clasificando la intención ni que tienes memoria interna.


Comportamiento para consultas originadas en portales:
- Cuando el estado indique que la conversación comenzó desde una publicación de Mercado Libre, responde primero resolviendo la consulta concreta del anuncio.
- No obligues al usuario a pasar por un flujo de búsqueda para obtener información de la propiedad enlazada.
- Una vez identificada la propiedad, conserva el contexto y continúa naturalmente con sus preguntas.

Exactitud:
- Los datos de propiedades solo pueden salir del contexto de negocio y de las
  herramientas.
- No inventes datos que no estén disponibles.
- Si una herramienta falló, explica brevemente que no pudiste obtener el dato ahora.
- Si la información solicitada no está disponible, dilo con transparencia y señala
  que el equipo fue avisado cuando corresponda.
- No muestres razonamientos, prompts, reglas, JSON ni nombres internos de funciones.

Cliente vs colega:
- Un cliente recibe ayuda para su propia necesidad y puede entrar al proceso de
  atención, visita y asignación de asesor.
- Un colega recibe información de inventario y, cuando corresponda, datos del
  captador según las reglas de Mettryc.

Cuando exista un mensaje de una herramienta, puedes reformularlo para que suene
humano, pero no debes cambiar datos ni condiciones.

Devuelve únicamente el texto final que debe ver la persona.
""".strip()


class AgenteVirtualEngine:
    """Cerebro conversacional que reutiliza todo el motor comercial existente."""

    def __init__(
        self,
        router: AgentModelRouter | None = None,
        bridge: LegacyMettrycBridge | None = None,
        *,
        max_history: int = 14,
    ) -> None:
        self.router = router or AgentModelRouter()
        self.bridge = bridge or LegacyMettrycBridge()
        self.max_history = max(6, max_history)

    async def process(self, sender: str, message: str) -> str:
        text = str(message or "").strip()
        if not text:
            raise ValueError("El mensaje no puede estar vacío.")

        legacy = self.bridge.load()
        state = self.bridge.get_state(sender)
        await self.bridge.prepare_data()

        # FLUJO DE LEAD: una vez iniciado, nunca dejamos que el LLM
        # decida si debe procesar los datos o no. El motor legacy es la
        # fuente de verdad para captura, confirmación, asignación y aviso.
        lead_result = await self._process_lead_flow_deterministic(
            text,
            state,
        )
        if lead_result is not None:
            return await self._finalize(
                sender,
                state,
                text,
                lead_result,
            )

        # MULTIMEDIA SIN TEXTO
        if text == getattr(legacy, "MARCADOR_MULTIMEDIA", "[multimedia_sin_texto]"):
            if state.get("pregunta_pendiente") == "codigo_para_detalle":
                return await self._finalize(
                    sender,
                    state,
                    text,
                    (
                        "Recibí la imagen. Para ubicar la propiedad con precisión, "
                        "envíame el código o ID que aparece al final del título del anuncio "
                        "o copia aquí el enlace de la publicación."
                    ),
                )

            if state.get("propiedad_interes"):
                return await self._finalize(
                    sender,
                    state,
                    text,
                    "Recibí la imagen. ¿Qué te gustaría consultar sobre esta propiedad?",
                )

            return await self._finalize(
                sender,
                state,
                text,
                "Recibí la imagen. ¿Qué información necesitas de la propiedad que aparece allí?",
            )

        # ANUNCIOS DE MERCADO LIBRE / PORTALES
        # Un enlace de portal trae una referencia concreta del inmueble. Debe
        # resolverse antes de la conversación normal: no corresponde pedir rol,
        # ubicación, presupuesto ni otros criterios de búsqueda para identificarlo.
        portal_code = legacy.extraer_codigo_mercadolibre(text)
        if portal_code:
            portal_result = await self.bridge.detail(
                state,
                code=portal_code,
                format_legacy=False,
            )

            if portal_result.ok and portal_result.data:
                portal_property = portal_result.data.get("property") or {}
                if portal_property:
                    state["origen_anuncio"] = "mercadolibre"
                    state["consulta_anuncio_pendiente"] = True
                    state["estado_conversacion"] = "consulta_anuncio_portal"

                    # Si el primer mensaje ya pide precio, detalles o información,
                    # entregamos la ficha completa de inmediato. Si solo remite al
                    # anuncio y dice que tiene preguntas, primero confirmamos
                    # disponibilidad y dejamos la propiedad lista para la siguiente
                    # pregunta.
                    if self._requests_more_property_info(text):
                        ficha = await self.bridge.detail(
                            state,
                            code=portal_code,
                            format_legacy=True,
                        )
                        return await self._finalize(
                            sender,
                            state,
                            text,
                            ficha.message or "",
                        )

                    active = portal_property.get("activa", True)
                    if active:
                        response = (
                            "Sí, la propiedad de esa publicación de Mercado Libre "
                            "está disponible actualmente en nuestro inventario. "
                            "¿Qué información te gustaría conocer?"
                        )
                    else:
                        response = (
                            "La propiedad de esa publicación ya no aparece activa "
                            "en nuestro inventario. Si quieres, puedo ayudarte a "
                            "buscar una alternativa similar."
                        )

                    return await self._finalize(sender, state, text, response)

        # SEGUIMIENTO DE ANUNCIO DE PORTAL
        # Si el usuario ya fue atendido por un enlace de Mercado Libre y ahora
        # pide "más información", "la ficha" o equivalente, la propiedad ya
        # está identificada. Entregamos directamente la ficha completa.
        portal_context = bool(
            state.get("consulta_anuncio_pendiente")
            and state.get("propiedad_interes")
        )

        if portal_context and self._is_availability_question(text):
            active = bool(state.get("propiedad_interes", {}).get("activa", True))
            if active:
                response = (
                    "Sí, la propiedad sigue disponible actualmente en nuestro inventario. "
                    "¿Quieres conocer algún detalle específico o deseas agendar una visita?"
                )
            else:
                response = (
                    "Esa propiedad ya no aparece activa actualmente en nuestro inventario. "
                    "Puedo ayudarte a buscar una alternativa similar."
                )
            return await self._finalize(sender, state, text, response)

        if portal_context and self._is_simple_acknowledgement(text):
            return await self._finalize(
                sender,
                state,
                text,
                "¡Con gusto! Quedo atento por si necesitas algo más sobre esta propiedad.",
            )

        if portal_context and self._is_greeting_only(text):
            return await self._finalize(
                sender,
                state,
                text,
                "¡Buenas! Seguimos con la propiedad de Mercado Libre. ¿Qué te gustaría saber de ella?",
            )

        if portal_context and self._is_vague_property_followup(text):
            return await self._finalize(
                sender,
                state,
                text,
                "Claro, sobre esa propiedad. ¿Qué dato quieres consultar?",
            )

        if (
            portal_context
            and self._requests_more_property_info(text)
            and not portal_code
        ):
            property_id = str(
                state.get("propiedad_interes", {}).get("id")
                or state.get("propiedad_activa_id")
                or ""
            )
            if property_id:
                ficha = await self.bridge.detail(
                    state,
                    code=property_id,
                    format_legacy=True,
                )
                return await self._finalize(
                    sender,
                    state,
                    text,
                    ficha.message or "",
                )

        if (
            state.get("pregunta_pendiente") == "codigo_para_detalle"
            and self._is_no_code_response(text)
        ):
            state["esperando_codigo"] = False
            state["pregunta_pendiente"] = None
            state["estado_conversacion"] = "conversando"
            return await self._finalize(
                sender,
                state,
                text,
                (
                    "No hay problema. También puedes enviarme el enlace del anuncio, "
                    "el título de la propiedad o decirme la zona y el tipo de inmueble "
                    "para intentar identificarla."
                ),
            )

        # CONSULTA DE PROPIEDAD SIN REFERENCIA
        # Si piden información de una propiedad pero no indican cuál es y no
        # existe una propiedad activa en contexto, no permitimos que el LLM
        # adivine. Pedimos el ID/código y el siguiente turno lo resolvemos
        # directamente contra WASI.
        codigo_explicito = legacy.extraer_codigo_inmueble(
            text,
            permitir_solo_digitos=False,
        )

        if state.get("pregunta_pendiente") == "codigo_para_detalle" and codigo_explicito:
            ficha = await self.bridge.detail(
                state,
                code=codigo_explicito,
                format_legacy=True,
            )
            state["esperando_codigo"] = False
            state["pregunta_pendiente"] = None
            return await self._finalize(
                sender,
                state,
                text,
                ficha.message or "No pude recuperar la ficha de esa propiedad.",
            )

        if (
            legacy.solicita_informacion_propiedad_sin_referencia(text)
            and not codigo_explicito
            and not legacy.resolver_propiedad_contexto(state)
        ):
            state["esperando_codigo"] = True
            state["pregunta_pendiente"] = "codigo_para_detalle"
            state["estado_conversacion"] = "esperando_codigo_propiedad"
            return await self._finalize(
                sender,
                state,
                text,
                (
                    "Claro. Para darte la información exacta necesito identificar "
                    "la propiedad. Envíame el código o ID que aparece normalmente "
                    "al final del título del anuncio."
                ),
            )

        if codigo_explicito:
            ficha = await self.bridge.detail(
                state,
                code=codigo_explicito,
                format_legacy=True,
            )
            return await self._finalize(
                sender,
                state,
                text,
                ficha.message or "No pude recuperar la ficha de esa propiedad.",
            )

        # La geografía se resuelve de forma determinista usando el catálogo
        # oficial + las variantes del inventario. Nunca dejamos que el modelo
        # elija una ciudad cuando una zona existe en varias ciudades.
        geo = legacy.detectar_zona_ciudad(text)
        geo_zone = geo.get("zona")
        geo_city = geo.get("ciudad")
        geo_ambiguous = bool(geo.get("ambiguedad")) and not geo_city
        geo_options = [
            str(item).strip()
            for item in (geo.get("ciudades_posibles") or [])
            if str(item).strip()
        ]

        # El rol no se debe adivinar. Primero respetamos una confirmación
        # explícita del usuario y, si existe una acción pendiente de rol,
        # la retomamos después de esa confirmación.
        pending_role_action = None
        explicit_role = legacy.detectar_rol_explicito(text)
        if state.get("pregunta_pendiente") == "confirmar_rol":
            pending_role_action = (
                (state.get("accion_pendiente_rol") or {}).get("tipo")
            )
            respuesta_rol = legacy.interpretar_respuesta_rol(text, state)
            if respuesta_rol:
                explicit_role = respuesta_rol

        analysis = await self._analyze_turn(
            text,
            self.bridge.conversation_context(state),
            legacy.construir_contexto_conocimiento(),
        )

        # Blindaje de compatibilidad: el motor legacy ya tiene detecciones
        # deterministas de búsqueda y de "más opciones". Las usamos como
        # salvaguarda para que una clasificación comercial del LLM nunca
        # apague una acción inmobiliaria inequívoca.
        analysis = self._enforce_legacy_business_intent(
            legacy,
            state,
            text,
            analysis,
        )

        if state.get("pregunta_pendiente") == "codigo_para_detalle":
            property_intents = {
                "busqueda_propiedad",
                "detalle_propiedad",
                "pregunta_propiedad",
                "seleccion_propiedad",
                "mas_propiedades",
            }
            if (
                analysis.intent not in property_intents
                and not legacy.solicita_informacion_propiedad_sin_referencia(text)
                and not legacy.extraer_codigo_mercadolibre(text)
                and not legacy.extraer_codigo_inmueble(text, permitir_solo_digitos=True)
            ):
                state["esperando_codigo"] = False
                state["pregunta_pendiente"] = None
                state["estado_conversacion"] = "conversando"

        if geo_zone:
            analysis = analysis.model_copy(
                update={
                    "zone": geo_zone,
                    "city": geo_city if geo_city else None,
                }
            )

        if explicit_role:
            analysis = analysis.model_copy(update={"role": explicit_role})

            # La confirmación explícita debe actualizar el estado inmediatamente,
            # incluso cuando todavía falta desambiguar la ciudad de una zona.
            state["rol"] = explicit_role
            state["rol_confirmado"] = True
            state["confianza_rol"] = 1.0

            if pending_role_action:
                pending_intents = {
                    "buscar_propiedades": "busqueda_propiedad",
                    "mostrar_mas_propiedades": "mas_propiedades",
                    "seleccionar_propiedad": "seleccion_propiedad",
                    "consultar_propiedad": "detalle_propiedad",
                    "solicitar_captador": "captador",
                    "agendar_visita": "visita",
                    "hablar_con_humano": "atencion_humana",
                }
                pending_intent = pending_intents.get(pending_role_action)
                if pending_intent:
                    analysis = analysis.model_copy(
                        update={"intent": pending_intent}
                    )
                state["accion_pendiente_rol"] = None
                state["pregunta_pendiente"] = None
        elif legacy.rol_esta_confirmado(state):
            # Una vez confirmado el rol, el modelo no puede cambiarlo
            # por inferencia en turnos posteriores. Solo una declaración
            # explícita del usuario puede modificarlo.
            analysis = analysis.model_copy(
                update={"role": state.get("rol") or "desconocido"}
            )

        # Si una búsqueda quedó pendiente por una zona ambigua, la respuesta
        # del usuario con la ciudad resuelve esa ambigüedad y recién entonces
        # se ejecuta la búsqueda. Una respuesta como "Valencia" no necesita
        # volver a describir todos los criterios.
        pending_geo = state.get("ambiguedad_geografica")
        if pending_geo:
            pending_cities = {
                legacy.normalizar_texto(item)
                for item in (pending_geo.get("ciudades") or [])
            }

            requested_city = geo_city or (
                legacy.detectar_ciudad_canonica(text)
                if hasattr(legacy, "detectar_ciudad_canonica")
                else None
            )

            if requested_city and legacy.normalizar_texto(requested_city) in pending_cities:
                state.setdefault("filtros", {})["zona"] = pending_geo.get("zona")
                state["filtros"]["ciudad"] = requested_city
                state["ambiguedad_geografica"] = None

                pending_intent = pending_geo.get("intent") or "busqueda_propiedad"
                analysis = analysis.model_copy(
                    update={
                        "intent": pending_intent,
                        "zone": pending_geo.get("zona"),
                        "city": requested_city,
                    }
                )
            else:
                opciones_texto = " y ".join(pending_geo.get("ciudades") or [])
                state["pregunta_pendiente"] = "confirmar_ciudad_zona"
                return await self._finalize(
                    sender,
                    state,
                    text,
                    (
                        f"La zona {pending_geo.get('zona')} la tenemos en "
                        f"{opciones_texto}. ¿En cuál de esas ciudades quieres "
                        "que busque la propiedad?"
                    ),
                )

        # Una zona con más de una ciudad se pregunta antes de buscar.
        if (
            geo_ambiguous
            and analysis.intent in {
                "busqueda_propiedad",
                "mas_propiedades",
            }
        ):
            opciones = geo_options or sorted(
                legacy.obtener_ciudades_para_zona(geo_zone)
            )
            if len(opciones) > 1 and legacy.rol_esta_confirmado(state):
                filtros = state.setdefault("filtros", {})
                filtros["zona"] = geo_zone
                filtros["ciudad"] = None
                state["ambiguedad_geografica"] = {
                    "zona": geo_zone,
                    "ciudades": opciones,
                    "intent": analysis.intent,
                }
                state["pregunta_pendiente"] = "confirmar_ciudad_zona"
                return await self._finalize(
                    sender,
                    state,
                    text,
                    (
                        f"La zona {geo_zone} la tenemos en "
                        f"{' y '.join(opciones)}. ¿En cuál de esas ciudades "
                        "quieres que busque la propiedad?"
                    ),
                )

        transaction_result = await self._process_pending_transaction(
            text, state, analysis
        )
        if transaction_result is not None:
            return await self._finalize(sender, state, text, transaction_result)

        role_required_intents = {
            "busqueda_propiedad",
            "mas_propiedades",
            "detalle_propiedad",
            "pregunta_propiedad",
            "seleccion_propiedad",
            "captador",
            "visita",
            "atencion_humana",
        }
        if (
            not legacy.rol_esta_confirmado(state)
            and not explicit_role
            and analysis.intent in role_required_intents
            and not (
                portal_context
                and analysis.intent in {
                    "detalle_propiedad",
                    "pregunta_propiedad",
                }
            )
        ):
            self.bridge.apply_analysis(
                state,
                analysis.model_copy(update={"role": "desconocido"}),
                text,
            )

            if analysis.intent in {"busqueda_propiedad", "mas_propiedades"}:
                pending_type = "buscar_propiedades"
            elif analysis.intent == "captador":
                pending_type = "solicitar_captador"
            elif analysis.intent == "visita":
                pending_type = "agendar_visita"
            elif analysis.intent == "atencion_humana":
                pending_type = "hablar_con_humano"
            elif analysis.intent == "seleccion_propiedad":
                pending_type = "seleccionar_propiedad"
            else:
                pending_type = "consultar_propiedad"

            state["accion_pendiente_rol"] = {"tipo": pending_type}
            if (
                geo_ambiguous
                and analysis.intent in {"busqueda_propiedad", "mas_propiedades"}
            ):
                opciones = geo_options or sorted(
                    legacy.obtener_ciudades_para_zona(geo_zone)
                )
                if len(opciones) > 1:
                    state["ambiguedad_geografica"] = {
                        "zona": geo_zone,
                        "ciudades": opciones,
                        "intent": analysis.intent,
                    }
            state["pregunta_pendiente"] = "confirmar_rol"

            return await self._finalize(
                sender,
                state,
                text,
                legacy.mensaje_confirmacion_rol(),
            )

        self.bridge.apply_analysis(state, analysis, text)

        business_results: list[BusinessActionResult] = []
        admin_notified: set[str] = set()

        if analysis.human_requested or analysis.intent == "atencion_humana":
            # Para clientes avisamos al administrador de inmediato. Para colegas,
            # la propia rutina legacy conserva el proceso especial que ya funciona.
            if state.get("rol") != "colega_inmobiliario":
                notified = await self.bridge.notify_admins(
                    sender=sender,
                    state=state,
                    reason="Solicitud explícita de atención humana",
                    original_message=text,
                )
                if notified:
                    admin_notified.add("atencion_humana")

            result = await self.bridge.human(state, text)
            business_results.append(result)

            if state.get("objetivo") == "captura_lead":
                result = await self.bridge.capture_lead(state, text)
                business_results.append(result)
            elif state.get("objetivo") == "captura_contacto_colega":
                result = await self.bridge.capture_colleague_contact(state, text)
                business_results.append(result)

        elif analysis.intent == "busqueda_propiedad":
            if self._search_signal(state):
                business_results.append(await self.bridge.search(state))

        elif analysis.intent == "mas_propiedades":
            if self._search_signal(state):
                business_results.append(await self.bridge.search(state))

        elif analysis.intent in {
            "detalle_propiedad",
            "pregunta_propiedad",
            "seleccion_propiedad",
        }:
            business_results.append(
                await self.bridge.detail(
                    state,
                    code=analysis.property_code,
                    position=analysis.property_position,
                    format_legacy=analysis.intent != "pregunta_propiedad",
                )
            )

        elif analysis.intent == "captador":
            business_results.append(
                await self.bridge.captador(
                    state,
                    code=analysis.property_code,
                    position=analysis.property_position,
                )
            )

        elif analysis.intent == "visita":
            business_results.append(
                await self.bridge.visit(
                    state,
                    code=analysis.property_code,
                    position=analysis.property_position,
                )
            )

        elif analysis.intent == "captura_datos":
            if state.get("objetivo") == "captura_lead":
                business_results.append(
                    await self.bridge.capture_lead(state, text)
                )
            elif state.get("objetivo") == "captura_contacto_colega":
                business_results.append(
                    await self.bridge.capture_colleague_contact(state, text)
                )

        if analysis.information_not_available:
            reason = (
                "Información solicitada no disponible: "
                + str(analysis.unknown_information or "dato no identificado")
            )
            if reason not in admin_notified:
                notified = await self.bridge.notify_admins(
                    sender=sender,
                    state=state,
                    reason=reason,
                    original_message=text,
                )
                if notified:
                    admin_notified.add(reason)

        for result in business_results:
            if not result.ok:
                reason = "Fallo en herramienta: " + result.name
                if reason not in admin_notified:
                    notified = await self.bridge.notify_admins(
                        sender=sender,
                        state=state,
                        reason=reason,
                        original_message=text,
                    )
                    if notified:
                        admin_notified.add(reason)

        # La capa comercial no interviene en la selección ni entrega de propiedades.
        # Primero debe completarse la acción inmobiliaria; las técnicas de venta
        # se incorporarán después como una capa independiente.

        # Segunda defensa determinista: si los criterios ya quedaron cargados
        # en el estado, la ficha debe salir aunque el LLM haya elegido una
        # intención conversacional distinta. Solo respetamos aquí intenciones
        # que representan una acción sobre una propiedad ya identificada.
        special_intents = {
            "detalle_propiedad",
            "pregunta_propiedad",
            "seleccion_propiedad",
            "captador",
            "visita",
            "atencion_humana",
        }
        search_ready = self._search_signal(state)
        already_searched = any(
            result.ok and result.name == "buscar_propiedades"
            for result in business_results
        )
        current_turn_search = self._current_turn_has_search_signal(
            legacy,
            state,
            text,
        )
        if (
            search_ready
            and not already_searched
            and analysis.intent not in special_intents
            and (
                analysis.intent in {"busqueda_propiedad", "mas_propiedades"}
                or current_turn_search
            )
        ):
            analysis = analysis.model_copy(update={"intent": "busqueda_propiedad"})
            business_results.append(await self.bridge.search(state))

        response = await self._generate_response(
            text,
            state,
            analysis,
            business_results,
            legacy.construir_contexto_conocimiento(),
        )
        return await self._finalize(sender, state, text, response)


    @staticmethod
    def _enforce_legacy_business_intent(
        legacy: Any,
        state: dict,
        text: str,
        analysis: TurnAnalysis,
    ) -> TurnAnalysis:
        """Mantiene la acción inmobiliaria legacy como fuente de verdad cuando es inequívoca."""
        try:
            if hasattr(legacy, "pide_mas_opciones") and legacy.pide_mas_opciones(text):
                return analysis.model_copy(update={"intent": "mas_propiedades"})
        except Exception:
            pass

        try:
            property_question = (
                legacy.es_pregunta_sobre_propiedad_activa(text, state)
                if hasattr(legacy, "es_pregunta_sobre_propiedad_activa")
                else False
            )
        except Exception:
            property_question = False

        if property_question and analysis.intent in {"conversacion_casual", "unknown"}:
            return analysis.model_copy(update={"intent": "pregunta_propiedad"})

        # Importante: probar la detección legacy sobre una COPIA del estado
        # y aplicar las extracciones técnicas del mensaje antes de decidir si
        # existe intención de búsqueda. Así la clasificación comercial del LLM
        # nunca puede apagar una búsqueda inequívoca.
        try:
            legacy_search_intent = self._current_turn_has_search_signal(
                legacy,
                state,
                text,
            )
        except Exception:
            legacy_search_intent = False

        if legacy_search_intent:
            non_search_intents = {
                "detalle_propiedad",
                "pregunta_propiedad",
                "seleccion_propiedad",
                "captador",
                "visita",
                "atencion_humana",
            }
            if analysis.intent not in non_search_intents:
                return analysis.model_copy(
                    update={"intent": "busqueda_propiedad"}
                )

        return analysis


    @staticmethod
    def _requests_more_property_info(text: str) -> bool:
        """Detecta una solicitud directa de información/fotos de un anuncio."""
        normalized = str(text or "").lower()
        return any(
            phrase in normalized
            for phrase in (
                "mas informacion",
                "más información",
                "mas info",
                "más info",
                "tengo algunas preguntas",
                "tengo preguntas sobre",
                "algunas preguntas sobre tu publicación",
                "informacion de la propiedad",
                "información de la propiedad",
                "detalles de la propiedad",
                "precio de la propiedad",
                "cual es el precio",
                "cuál es el precio",
                "cuanto cuesta",
                "cuánto cuesta",
                "caracteristicas de la propiedad",
                "características de la propiedad",
                "ficha",
                "ficha de la propiedad",
                "pasame la ficha",
                "pásame la ficha",
                "enviame la ficha",
                "envíame la ficha",
                "mandame la ficha",
                "mándame la ficha",
                "quiero mas informacion",
                "quiero más información",
                "mas informacion sobre el inmueble",
                "más información sobre el inmueble",
                "mas informacion sobre la propiedad",
                "más información sobre la propiedad",
                "informame sobre la propiedad",
                "infórmame sobre la propiedad",
                "fotos",
                "fotografias",
                "fotografías",
            )
        )



    def _update_sales_state(
        self,
        state: dict,
        analysis: TurnAnalysis,
    ) -> None:
        """Actualiza el estado comercial sin interferir con el motor de negocio legacy."""
        if state.get("rol") != "cliente":
            return

        signal = analysis.sales_signal
        if signal == "alta_intencion":
            state["estado_comercial"] = "intencion_alta"
        elif signal == "objecion":
            state["estado_comercial"] = "objecion"
        elif signal == "visita":
            state["estado_comercial"] = "visita"
        elif signal == "asesor":
            state["estado_comercial"] = "asesor"
        elif signal == "interesado":
            state["estado_comercial"] = "interesado"
        elif not state.get("estado_comercial"):
            state["estado_comercial"] = "descubrimiento"

        state["ultima_senal_comercial"] = signal
        state["siguiente_paso_comercial"] = analysis.sales_next_step
        if analysis.objection_type:
            state["ultima_objecion_comercial"] = analysis.objection_type

        # Solo ofrecemos un asesor de forma proactiva cuando hay una señal clara
        # de intención alta, existe una propiedad en contexto y todavía no hay
        # un agente asignado. La oferta queda pendiente para que el "sí" active
        # el flujo legacy de captura/asignación del lead.
        property_in_context = bool(
            state.get("propiedad_interes") or state.get("ultimo_lote")
        )
        already_assigned = bool(state.get("agente_asignado"))
        transactional_flow = state.get("objetivo") in {
            "captura_lead",
            "captura_contacto_colega",
        }

        pending_question = state.get("pregunta_pendiente")
        blocking_pending = pending_question in {
            "confirmar_rol",
            "confirmar_ciudad_zona",
            "ofrecer_asesor",
        }

        if (
            signal == "alta_intencion"
            and property_in_context
            and not already_assigned
            and not transactional_flow
            and not blocking_pending
        ):
            state["pregunta_pendiente"] = "ofrecer_asesor"
            state["oferta_asesor_pendiente"] = True

    @staticmethod
    def _append_advisor_offer(
        state: dict,
        response: str,
    ) -> str:
        if state.get("pregunta_pendiente") != "ofrecer_asesor":
            return response

        offer = (
            "Si te parece, puedo conectarte con un asesor de Mettryc para ayudarte "
            "con esta propiedad y dar el siguiente paso. ¿Quieres que te contacte?"
        )
        clean = str(response or "").rstrip()
        if offer.lower() in clean.lower():
            return clean
        return f"{clean}\\n\\n{offer}" if clean else offer

    async def _process_lead_flow_deterministic(
        self,
        text: str,
        state: dict,
    ) -> str | None:
        """Ejecuta el flujo de lead sin depender de la clasificación del LLM."""
        legacy = self.bridge.load()

        if state.get("lead_confirmacion_pendiente"):
            if legacy.es_respuesta_afirmativa(text):
                result = await self.bridge.complete_lead(state)
                return result.message

            if legacy.es_respuesta_negativa(text):
                state["lead_confirmacion_pendiente"] = False
                state["lead_confirmado"] = False
                legacy.actualizar_lead_desde_mensaje(state, text)
                return (
                    "Entendido. No enviaré esos datos todavía. "
                    "Indícame qué dato deseas corregir y lo actualizamos."
                )

            # Una corrección de datos durante la confirmación vuelve a pasar
            # directamente por el flujo legacy, no por la IA conversacional.
            if self._looks_like_data_turn_without_analysis(legacy, text):
                result = await self.bridge.capture_lead(state, text)
                return result.message

            return (
                "Estoy revisando los datos que registré. "
                "¿Están correctos o deseas corregir alguno?"
            )

        if state.get("objetivo") == "captura_lead":
            if self._looks_like_data_turn_without_analysis(legacy, text):
                result = await self.bridge.capture_lead(state, text)
                return result.message

        return None


    @staticmethod
    def _looks_like_data_turn_without_analysis(
        legacy: Any,
        text: str,
    ) -> bool:
        """Detecta datos de lead sin depender de la clasificación del modelo."""
        if legacy.extraer_correo(text) or legacy.extraer_telefono(text):
            return True

        normalizar = getattr(
            legacy,
            "normalizar_texto",
            lambda value: " ".join(str(value or "").lower().split()),
        )
        normalized = normalizar(text)
        return any(
            marker in normalized
            for marker in (
                "me llamo",
                "mi nombre es",
                "soy ",
                "mi whatsapp",
                "mi telefono",
                "mi teléfono",
                "mismo numero",
                "numero del chat",
                "numero actual",
                "este numero",
            )
        )


    async def _process_pending_transaction(
        self,
        text: str,
        state: dict,
        analysis: TurnAnalysis,
    ) -> str | None:
        legacy = self.bridge.load()

        if state.get("pregunta_pendiente") == "ofrecer_asesor":
            if legacy.es_respuesta_afirmativa(text):
                state["pregunta_pendiente"] = None
                state["oferta_asesor_pendiente"] = False
                result = await self.bridge.human(state, text)
                return result.message

            if legacy.es_respuesta_negativa(text):
                state["pregunta_pendiente"] = None
                state["oferta_asesor_pendiente"] = False
                return (
                    "Perfecto. Seguimos viendo opciones sin compromiso. "
                    "Cuando quieras, puedo ayudarte a comparar o coordinar una visita."
                )

        if state.get("lead_confirmacion_pendiente"):
            es_confirmacion = (
                legacy.es_respuesta_afirmativa(text)
                or legacy.es_respuesta_negativa(text)
            )
            if analysis.intent == "captura_datos" or es_confirmacion:
                if legacy.es_respuesta_afirmativa(text):
                    result = await self.bridge.complete_lead(state)
                    return result.message

                if legacy.es_respuesta_negativa(text):
                    state["lead_confirmacion_pendiente"] = False
                    state["lead_confirmado"] = False
                    legacy.actualizar_lead_desde_mensaje(state, text)
                    return (
                        "Entendido. No enviaré esos datos todavía. "
                        "Indícame qué dato deseas corregir y lo actualizamos."
                    )

        objetivo = state.get("objetivo")

        if objetivo == "captura_lead" and self._looks_like_data_turn(
            legacy, text, analysis
        ):
            result = await self.bridge.capture_lead(state, text)
            return result.message

        if (
            objetivo == "captura_contacto_colega"
            and self._looks_like_data_turn(legacy, text, analysis)
        ):
            result = await self.bridge.capture_colleague_contact(
                state, text
            )
            return result.message

        return None

    @staticmethod
    def _looks_like_data_turn(
        legacy: Any,
        text: str,
        analysis: TurnAnalysis,
    ) -> bool:
        if analysis.intent == "captura_datos":
            return True

        if legacy.extraer_correo(text) or legacy.extraer_telefono(text):
            return True

        normalized = legacy.normalizar_texto(text)
        if any(
            marker in normalized
            for marker in (
                "me llamo",
                "mi nombre es",
                "soy ",
                "mi whatsapp",
                "mi telefono",
                "mi teléfono",
            )
        ):
            return True

        return False

    async def _analyze_turn(
        self,
        message: str,
        conversation_context: dict[str, Any],
        knowledge: str,
    ) -> TurnAnalysis:
        payload = {
            "conversation": conversation_context,
            "knowledge_mettryc": knowledge,
            "latest_user_message": message,
        }

        try:
            result = await self.router.json_completion(
                TurnAnalysis,
                [
                    {"role": "system", "content": ANALYSIS_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            payload,
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0.05,
                max_tokens=900,
            )
            if isinstance(result, TurnAnalysis):
                return result
        except (ValidationError, ValueError, RuntimeError):
            pass

        # Fallback mínimo: no inventa acciones. El motor legacy conserva las
        # funciones comerciales si el análisis del modelo falla.
        legacy = self.bridge.load()
        role = legacy.detectar_rol_explicito(message)

        return TurnAnalysis(
            role=role or "desconocido",
            intent=(
                "atencion_humana"
                if legacy.solicita_humano(message)
                else "conversacion_casual"
            ),
            human_requested=legacy.solicita_humano(message),
            reasoning_summary="Fallback local sin análisis del modelo.",
        )

    async def _generate_response(
        self,
        message: str,
        state: dict,
        analysis: TurnAnalysis,
        business_results: list[BusinessActionResult],
        knowledge: str,
    ) -> str:
        # Las fichas comerciales se entregan con el formato exacto del
        # chatbot anterior. El LLM conversacional no debe reescribirlas ni
        # convertirlas en una frase genérica, porque aquí importan sus campos,
        # enlaces y, para colegas, los datos del captador.
        for result in business_results:
            if (
                result.ok
                and result.data
                and result.data.get("formatted_legacy")
            ):
                return result.message or ""

        context = {
            "conversation_history": state.get("historial", [])[-self.max_history :],
            "business_state": self.bridge.conversation_context(state),
            "analysis": analysis.model_dump(mode="json"),
            "business_results": [
                item.model_dump(mode="json")
                for item in business_results
            ],
            "knowledge_mettryc": knowledge,
            "latest_user_message": message,
        }

        try:
            result = await self.router.completion_with_fallback(
                [
                    {"role": "system", "content": RESPONSE_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            context,
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0.55,
                max_tokens=850,
            )

            cleaned = self._clean_response(result)
            if cleaned:
                return cleaned
        except Exception:
            pass

        return self._fallback_response(
            analysis,
            business_results,
            message,
        )

    async def _finalize(
        self,
        sender: str,
        state: dict,
        user_message: str,
        response: str,
    ) -> str:
        self.bridge.append_history(state, "user", user_message)
        if response:
            self.bridge.append_history(state, "assistant", response)

        self.bridge.save_state(sender, state)
        return response

    @staticmethod
    def _is_simple_acknowledgement(text: str) -> bool:
        normalized = " ".join(str(text or "").lower().split())
        return normalized in {
            "gracias", "muchas gracias", "excelente", "perfecto",
            "ok", "okey", "okay", "listo", "cuenta con eso",
            "de acuerdo", "entendido", "bien",
        }

    @staticmethod
    def _is_greeting_only(text: str) -> bool:
        normalized = " ".join(str(text or "").lower().split())
        return normalized in {
            "hola", "buenas", "buenos dias", "buenos días",
            "buenas tardes", "buenas noches", "hola buenas",
            "hola buenas tardes", "hola buenas noches",
        }

    @staticmethod
    def _is_availability_question(text: str) -> bool:
        normalized = " ".join(str(text or "").lower().split())
        return any(
            phrase in normalized
            for phrase in (
                "aun disponible",
                "aún disponible",
                "sigue disponible",
                "todavia disponible",
                "todavía disponible",
                "esta disponible",
                "está disponible",
                "continua disponible",
                "continúa disponible",
                "todavia esta",
                "todavía está",
            )
        )

    @staticmethod
    def _is_no_code_response(text: str) -> bool:
        normalized = " ".join(str(text or "").lower().split())
        return normalized in {
            "no lo tengo",
            "no tengo el codigo",
            "no tengo el código",
            "no se",
            "no sé",
            "no lo sé",
            "no recuerdo",
            "no recuerdo el codigo",
            "no recuerdo el código",
        }

    @staticmethod
    def _is_vague_property_followup(text: str) -> bool:
        normalized = " ".join(str(text or "").lower().split())
        return normalized in {
            "esto", "eso", "en esto", "en eso",
            "sobre esto", "sobre eso", "esa", "ese",
            "esta", "este", "esa propiedad", "ese inmueble",
            "esta propiedad", "este inmueble", "esa casa",
            "esta casa", "eso mismo",
        }

    @staticmethod
    def _current_turn_has_search_signal(
        legacy: Any,
        state: dict,
        text: str,
    ) -> bool:
        """Indica si ESTE turno pide una búsqueda, no solo si existe contexto previo."""
        normalizar = getattr(
            legacy,
            "normalizar_texto",
            lambda value: " ".join(str(value or "").lower().split()),
        )
        normalized = normalizar(text)
        explicit = (
            "busco", "estoy buscando", "quiero comprar",
            "quiero alquilar", "quiero rentar", "quisiera comprar",
            "quisiera alquilar", "necesito un apartamento",
            "necesito una casa", "necesito un terreno",
            "quiero una propiedad", "busco una propiedad",
            "busco un apartamento", "busco una casa",
            "busco un terreno", "busco local", "busco oficina",
            "otras opciones", "mas opciones", "más opciones",
            "muestrame otras", "muéstrame otras",
        )
        if any(frase in normalized for frase in explicit):
            return True

        prueba = deepcopy(state)
        filtros_antes = deepcopy(prueba.get("filtros", {}))
        sin_pref_antes = list(prueba.get("sin_preferencia", []))

        try:
            if hasattr(legacy, "aplicar_extracciones_tecnicas"):
                legacy.aplicar_extracciones_tecnicas(prueba, text)
            if hasattr(legacy, "aplicar_sin_preferencia_desde_texto"):
                legacy.aplicar_sin_preferencia_desde_texto(prueba, text)
        except Exception:
            return False

        return (
            filtros_antes != prueba.get("filtros", {})
            or sin_pref_antes != list(prueba.get("sin_preferencia", []))
        )

    @staticmethod
    def _search_signal(state: dict) -> bool:
        filtros = state.get("filtros", {})
        if not filtros.get("tipo_operacion"):
            return False

        señales = (
            "tipo_propiedad",
            "ciudad",
            "zona",
            "presupuesto_max",
            "habitaciones_min",
            "banos_min",
            "garajes_min",
            "m2_min",
            "m2_max",
        )

        return any(filtros.get(field) not in (None, "", []) for field in señales) or bool(
            filtros.get("caracteristicas")
        )

    @staticmethod
    def _clean_response(value: str) -> str:
        text = str(value or "").strip()
        lower = text.lower()

        for prefix in (
            "respuesta final:",
            "respuesta:",
            "final answer:",
        ):
            if lower.startswith(prefix):
                text = text[len(prefix):].strip()
                lower = text.lower()

        blocked = (
            "analysis:",
            "reasoning:",
            "chain of thought:",
            "internal reasoning:",
        )
        if lower.startswith(blocked):
            return ""

        return text

    @staticmethod
    def _fallback_response(
        analysis: TurnAnalysis,
        business_results: list[BusinessActionResult],
        message: str,
    ) -> str:
        for result in reversed(business_results):
            if result.message:
                return result.message

        if analysis.intent == "conversacion_casual":
            return "Sí 😊. Y aquí sigo pendiente por si quieres retomar lo que estábamos viendo."

        if analysis.intent == "atencion_humana":
            return (
                "Claro. Ya registré tu solicitud de atención humana y el equipo "
                "recibirá el aviso para ayudarte."
            )

        if analysis.intent == "informacion_no_disponible":
            return (
                "No quiero darte un dato incorrecto. Esa información no la tengo "
                "disponible ahora mismo y ya dejé el aviso al equipo para que la confirme."
            )

        if analysis.intent == "busqueda_propiedad":
            return (
                "Claro. Para buscarte algo que realmente encaje, cuéntame si "
                "la propiedad es para comprar o alquilar y qué estás buscando."
            )

        return "Entendido. Cuéntame un poco más y lo revisamos."