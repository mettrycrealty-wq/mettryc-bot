from __future__ import annotations

import json
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

Devuelve SOLO JSON con la estructura del esquema solicitado.
""".strip()


RESPONSE_PROMPT = """
Eres el agente virtual de Mettryc Realty. Respondes por mensajería como una persona
profesional, cercana y natural.

Tu respuesta será enviada directamente al usuario.

Comportamiento conversacional:
- Habla con naturalidad, sin menús y sin frases de robot.
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

        if geo_zone:
            analysis = analysis.model_copy(
                update={
                    "zone": geo_zone,
                    "city": geo_city if geo_city else None,
                }
            )

        if explicit_role:
            analysis = analysis.model_copy(update={"role": explicit_role})

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
            elif state.get("pregunta_pendiente") == "confirmar_ciudad_zona":
                opciones_texto = " y ".join(pending_geo.get("ciudades") or [])
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

        response = await self._generate_response(
            text,
            state,
            analysis,
            business_results,
            legacy.construir_contexto_conocimiento(),
        )

        return await self._finalize(sender, state, text, response)

    async def _process_pending_transaction(
        self,
        text: str,
        state: dict,
        analysis: TurnAnalysis,
    ) -> str | None:
        legacy = self.bridge.load()

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
