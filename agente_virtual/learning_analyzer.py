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

        # FIX:
        # Google Apps Script devuelve 302 hacia script.googleusercontent.com.
        # httpx necesita follow_redirects=True para continuar la petición.
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
        ) as client:
            response = await client.get(
                self.read_url,
                params=params,
            )

            response.raise_for_status()
            payload = response.json()

        if isinstance(payload, dict):
            events = payload.get("events", [])
        else:
            events = payload

        if not isinstance(events, list):
            raise RuntimeError(
                "Google Apps Script devolvió un formato inválido."
            )

        return [
            item
            for item in events
            if isinstance(item, dict)
        ]

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

        try:

            raw = await self.router.completion(
                messages,
                temperature=0.1,
                max_tokens=1200,
            )

            cleaned = self.router._clean_json(raw)

            ai_report = json.loads(cleaned)

            if isinstance(ai_report, dict):
                result["ai_analysis"] = ai_report

        except Exception as exc:

            result["ai_analysis_error"] = (
                type(exc).__name__
                + ": "
                + str(exc)[:240]
            )

        return result
