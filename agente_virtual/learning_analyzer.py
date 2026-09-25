from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from typing import Any

import httpx

from .router import AgentModelRouter


class PatyLearningAnalyzer:
    """
    Analiza las conversaciones registradas por Paty.

    Importante:
    - OBSERVA; no modifica el comportamiento de Paty.
    - No escribe en Google Sheets.
    - Resume patrones de conversión, abandono y señales comerciales.
    - Puede usar el modelo de Paty para convertir métricas en recomendaciones,
      pero las recomendaciones quedan como informe para revisión humana.
    """

    def __init__(self, router: AgentModelRouter | None = None) -> None:
        self.router = router or AgentModelRouter()
        self.read_url = os.getenv("PATY_LEARNING_READ_URL", "").strip()
        self.read_key = os.getenv("PATY_LEARNING_READ_KEY", "").strip()
        self.timeout = float(os.getenv("PATY_LEARNING_READ_TIMEOUT", "20"))

    async def fetch_events(self, limit: int = 1000) -> list[dict[str, Any]]:
        if not self.read_url:
            raise RuntimeError(
                "Falta PATY_LEARNING_READ_URL. Configura la URL de lectura "
                "de Google Apps Script."
            )

        params = {"limit": str(max(1, min(limit, 5000)))}
        if self.read_key:
            params["key"] = self.read_key

        # Google Apps Script suele responder con 302 hacia
        # script.googleusercontent.com. Render puede ejecutar la app detrás
        # de proxies, por lo que dejamos que httpx siga 301/302/303/307/308
        # y desactivamos variables proxy del entorno para evitar saltos
        # inesperados por la infraestructura de ejecución.
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                trust_env=False,
            ) as client:
                response = await client.get(self.read_url, params=params)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            location = exc.response.headers.get("location", "")
            suffix = f" redirect={location[:180]}" if location else ""
            raise RuntimeError(
                f"Google Apps Script respondió HTTP {exc.response.status_code}.{suffix}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(
                "No se pudo conectar con Google Apps Script: "
                f"{type(exc).__name__}: {str(exc)[:220]}"
            ) from exc

        # No asumimos que el servidor siempre entregue application/json:
        # Apps Script/proxies pueden devolver texto aunque el cuerpo contenga
        # JSON válido. Primero intentamos el parser nativo y luego un fallback
        # controlado sobre el texto.
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            body = response.text.strip()
            raise RuntimeError(
                "Google Apps Script no devolvió JSON válido. "
                f"HTTP {response.status_code}; cuerpo={body[:300]!r}"
            ) from exc

        if isinstance(payload, dict):
            events = payload.get("events", [])
        else:
            events = payload

        if not isinstance(events, list):
            raise RuntimeError(
                "Google Apps Script devolvió un formato inválido: "
                "se esperaba una lista de eventos o un objeto con 'events'."
            )

        return [item for item in events if isinstance(item, dict)]

    @staticmethod
    def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
        turns = [
            e
            for e in events
            if e.get("event_type", e.get("event")) == "conversation_turn"
        ]

        conversations: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for event in events:
            cid = str(event.get("conversation_id") or "").strip()

            if cid:
                conversations[cid].append(event)

        total_conversations = len(conversations)

        def truth(value: Any) -> bool:
            return str(value).strip().lower() in {
                "true",
                "1",
                "yes",
                "si",
                "sí",
            }

        converted = set()
        assigned = set()
        abandoned = set()

        for cid, rows in conversations.items():

            if any(truth(r.get("lead_captured")) for r in rows):
                converted.add(cid)

            if any(truth(r.get("lead_assigned")) for r in rows):
                assigned.add(cid)

            if any(truth(r.get("possible_abandonment")) for r in rows):
                abandoned.add(cid)

        intents = Counter(
            str(e.get("intent") or "sin_intencion")
            for e in turns
        )

        signals = Counter(
            str(e.get("sales_signal") or "ninguna")
            for e in turns
        )

        next_steps = Counter(
            str(e.get("sales_next_step") or "ninguno")
            for e in turns
        )

        origins = Counter(
            str(e.get("origin") or "sin_origen")
            for e in turns
        )

        roles = Counter(
            str(e.get("role") or "sin_rol")
            for e in turns
        )

        turns_with_conversation_id = sum(
            1
            for e in turns
            if str(e.get("conversation_id") or "").strip()
        )
        turns_with_intent = sum(
            1
            for e in turns
            if str(e.get("intent") or "").strip()
        )
        turns_with_origin = sum(
            1
            for e in turns
            if str(e.get("origin") or "").strip()
        )
        turns_with_role = sum(
            1
            for e in turns
            if str(e.get("role") or "").strip()
        )
        turns_with_sales_signal = sum(
            1
            for e in turns
            if str(e.get("sales_signal") or "").strip()
            and str(e.get("sales_signal")).strip().lower() != "ninguna"
        )
        turns_with_sales_next_step = sum(
            1
            for e in turns
            if str(e.get("sales_next_step") or "").strip()
            and str(e.get("sales_next_step")).strip().lower() != "ninguno"
        )

        def coverage(count: int) -> float:
            return round(count / len(turns), 4) if turns else 0.0

        data_quality = {
            "turnos_evaluados": len(turns),
            "turnos_con_conversation_id": turns_with_conversation_id,
            "turnos_con_intencion": turns_with_intent,
            "turnos_con_origen": turns_with_origin,
            "turnos_con_rol": turns_with_role,
            "turnos_con_senal_comercial": turns_with_sales_signal,
            "turnos_con_siguiente_paso": turns_with_sales_next_step,
            "cobertura_conversation_id": coverage(turns_with_conversation_id),
            "cobertura_intencion": coverage(turns_with_intent),
            "cobertura_origen": coverage(turns_with_origin),
            "cobertura_rol": coverage(turns_with_role),
            "cobertura_senal_comercial": coverage(turns_with_sales_signal),
            "cobertura_siguiente_paso": coverage(turns_with_sales_next_step),
        }

        by_origin: dict[str, dict[str, int]] = defaultdict(
            lambda: {
                "conversaciones": 0,
                "convertidas": 0,
                "asignadas": 0,
            }
        )

        for cid, rows in conversations.items():

            turn = next(
                (
                    r
                    for r in rows
                    if r.get(
                        "event_type",
                        r.get("event")
                    ) == "conversation_turn"
                ),
                {},
            )

            origin = str(turn.get("origin") or "sin_origen")

            by_origin[origin]["conversaciones"] += 1

            if cid in converted:
                by_origin[origin]["convertidas"] += 1

            if cid in assigned:
                by_origin[origin]["asignadas"] += 1

        conversion_rate = (
            len(converted) / total_conversations
            if total_conversations
            else 0.0
        )

        assignment_rate = (
            len(assigned) / total_conversations
            if total_conversations
            else 0.0
        )

        return {
            "periodo_desde": min(
                (
                    str(e.get("timestamp"))
                    for e in events
                    if e.get("timestamp")
                ),
                default=None,
            ),
            "periodo_hasta": max(
                (
                    str(e.get("timestamp"))
                    for e in events
                    if e.get("timestamp")
                ),
                default=None,
            ),
            "eventos": len(events),
            "turnos": len(turns),
            "conversaciones": total_conversations,
            "conversaciones_con_lead": len(converted),
            "conversaciones_asignadas": len(assigned),
            "posibles_abandonos": len(abandoned),
            "tasa_captura_lead": round(conversion_rate, 4),
            "tasa_asignacion": round(assignment_rate, 4),
            "intenciones": dict(intents),
            "senales_comerciales": dict(signals),
            "siguientes_pasos": dict(next_steps),
            "origenes": dict(origins),
            "roles": dict(roles),
            "calidad_datos": data_quality,
            "por_origen": dict(by_origin),
        }

    async def analyze(
        self,
        *,
        limit: int = 1000,
        include_ai: bool = True,
    ) -> dict[str, Any]:

        events = await self.fetch_events(limit=limit)

        summary = self.summarize(events)

        result: dict[str, Any] = {
            "ok": True,
            "summary": summary,
            "recommendations": [],
            "note": (
                "Las recomendaciones son observaciones para revisión humana. "
                "Este análisis no cambia automáticamente el comportamiento de Paty."
            ),
        }

        if not include_ai or not events:
            return result

        prompt_data = json.dumps(
            summary,
            ensure_ascii=False,
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "Eres analista de conversaciones para Mettryc Realty. "
                    "Analiza métricas agregadas de Paty y propone mejoras "
                    "conversacionales basadas únicamente en los datos. "
                    "No inventes causas. Distingue hechos de hipótesis. "
                    "No cambies reglas, no programes nada y no recomiendes "
                    "un cuestionario rígido."
                ),
            },
            {
                "role": "user",
                "content": prompt_data,
            },
        ]

        raw = ""

        try:
            raw = await self.router.completion(
                messages,
                temperature=0.1,
                max_tokens=1200,
            )

            ai_report = self._parse_json_with_fallback(raw)

            if isinstance(ai_report, dict):
                result["ai_analysis"] = ai_report
                result["ai_analysis_format"] = "json"
            elif str(raw).strip():
                # La IA puede devolver un informe Markdown/texto aunque se le
                # pida una estructura JSON. Ese resultado sigue siendo útil:
                # lo conservamos como informe legible sin tratarlo como error.
                result["ai_analysis_text"] = str(raw).strip()
                result["ai_analysis_format"] = "text"
            else:
                result["ai_analysis_error"] = (
                    "La IA no devolvió contenido para el análisis."
                )

        except Exception as exc:
            result["ai_analysis_error"] = (
                type(exc).__name__
                + ": "
                + str(exc)[:240]
            )
            if raw:
                # Conservamos la respuesta original para diagnóstico/revisión
                # humana. Nunca hacemos que un JSON malformado rompa el endpoint.
                result["ai_analysis_fallback"] = raw

        return result

    @staticmethod
    def _parse_json_with_fallback(content: str) -> dict[str, Any] | None:
        """Extrae el primer objeto JSON válido de una respuesta imperfecta."""
        text = str(content or "").strip()
        if not text:
            return None

        candidates = [text]
        fence = chr(96) * 3
        if fence in text:
            parts = text.split(fence)
            candidates.extend(part.strip() for part in parts if part.strip())

        # Busca objetos candidatos sin depender de que el modelo haya puesto
        # JSON limpio al principio. json.JSONDecoder.raw_decode permite ignorar
        # texto introductorio y detectar exactamente dónde termina el objeto.
        decoder = json.JSONDecoder()
        for candidate in candidates:
            starts = [i for i, char in enumerate(candidate) if char == "{"]
            for start in starts:
                try:
                    value, _ = decoder.raw_decode(candidate[start:])
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    return value

        return None
