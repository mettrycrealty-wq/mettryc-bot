from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


class PatyLearningRecorder:
    """
    Instrumentación de aprendizaje de Paty.

    Esta capa OBSERVA y registra; no modifica la lógica comercial ni decide
    cómo conversar. Su función es crear evidencia para que posteriormente
    podamos detectar patrones de conversión y abandono.
    """

    def __init__(self) -> None:
        default_path = "./data/paty_learning.jsonl"
        self.path = Path(
            os.getenv("PATY_LEARNING_PATH", default_path)
        )
        self.pause_minutes = int(
            os.getenv("PATY_LEARNING_PAUSE_MINUTES", "720")
        )
        self.enabled = (
            os.getenv("PATY_LEARNING_ENABLED", "true").lower()
            in {"1", "true", "yes", "si", "sí"}
        )

    @staticmethod
    def _conversation_key(sender: str) -> str:
        return hashlib.sha256(
            str(sender or "").encode("utf-8")
        ).hexdigest()[:16]

    @staticmethod
    def _sanitize_text(value: Any) -> str:
        text = str(value or "")
        text = EMAIL_RE.sub("[EMAIL]", text)
        text = PHONE_RE.sub("[TELEFONO]", text)
        return text[:4000]

    @staticmethod
    def _iso_now() -> str:
        return datetime.utcnow().isoformat()

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record_turn(
        self,
        *,
        sender: str,
        state: dict,
        user_message: str,
        assistant_response: str,
    ) -> None:
        if not self.enabled:
            return

        now = datetime.utcnow()
        learning = state.setdefault(
            "paty_learning",
            {
                "conversation_id": self._conversation_key(sender),
                "turn_count": 0,
                "user_turns": 0,
                "assistant_turns": 0,
                "started_at": now.isoformat(),
                "last_turn_at": None,
                "last_intent": None,
                "last_sales_signal": None,
                "lead_captured": False,
                "lead_assigned": False,
                "pause_detected": False,
                "possible_abandonment": False,
            },
        )

        previous = learning.get("last_turn_at")
        pause_detected = False
        if previous:
            try:
                previous_dt = datetime.fromisoformat(str(previous))
                pause_detected = (
                    now - previous_dt
                    >= timedelta(minutes=self.pause_minutes)
                )
            except (TypeError, ValueError):
                pause_detected = False

        lead = state.get("lead") or {}
        lead_captured = bool(
            lead.get("nombre")
            or lead.get("correo")
            or lead.get("whatsapp")
        )
        lead_assigned = bool(state.get("agente_asignado"))

        learning["turn_count"] = int(learning.get("turn_count") or 0) + 1
        learning["user_turns"] = int(learning.get("user_turns") or 0) + 1
        learning["assistant_turns"] = int(learning.get("assistant_turns") or 0) + (
            1 if assistant_response else 0
        )
        learning["last_turn_at"] = now.isoformat()
        learning["last_intent"] = state.get("ultima_intencion")
        learning["last_sales_signal"] = state.get("ultima_senal_comercial")
        learning["lead_captured"] = lead_captured
        learning["lead_assigned"] = lead_assigned
        learning["pause_detected"] = pause_detected

        # Solo marcamos posible abandono cuando existía una conversación
        # comercial abierta y el cliente reaparece después de una pausa larga.
        # No afirmamos que sea abandono real.
        if pause_detected and not lead_assigned:
            learning["possible_abandonment"] = True

        event = {
            "event": "conversation_turn",
            "timestamp": now.isoformat(),
            "conversation_id": learning["conversation_id"],
            "turn_number": learning["turn_count"],
            "user_message": self._sanitize_text(user_message),
            "assistant_response": self._sanitize_text(assistant_response),
            "role": state.get("rol"),
            "intent": state.get("ultima_intencion"),
            "conversation_status": state.get("estado_conversacion"),
            "goal": state.get("objetivo"),
            "sales_signal": state.get("ultima_senal_comercial"),
            "sales_next_step": state.get("siguiente_paso_comercial"),
            "pending": state.get("pregunta_pendiente"),
            "origin": state.get("origen_anuncio"),
            "property_id": str(
                (state.get("propiedad_interes") or {}).get("id")
                or state.get("propiedad_activa_id")
                or ""
            ),
            "filters": {
                key: value
                for key, value in (state.get("filtros") or {}).items()
                if key != "caracteristicas"
            },
            "lead_captured": lead_captured,
            "lead_assigned": lead_assigned,
            "pause_detected": pause_detected,
            "possible_abandonment": bool(
                learning.get("possible_abandonment")
            ),
        }

        # Registramos por separado los hitos de conversión observables.
        events = [event]
        if lead_captured and not learning.get("_lead_event_written"):
            events.append(
                {
                    "event": "lead_contact_captured",
                    "timestamp": now.isoformat(),
                    "conversation_id": learning["conversation_id"],
                }
            )
            learning["_lead_event_written"] = True

        if lead_assigned and not learning.get("_assignment_event_written"):
            events.append(
                {
                    "event": "lead_assigned",
                    "timestamp": now.isoformat(),
                    "conversation_id": learning["conversation_id"],
                    "agent_present": True,
                }
            )
            learning["_assignment_event_written"] = True

        self._append(events)

    def _append(self, events: list[dict]) -> None:
        try:
            self._ensure_parent()
            with self.path.open("a", encoding="utf-8") as handle:
                for event in events:
                    handle.write(
                        json.dumps(
                            event,
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
        except Exception:
            # El aprendizaje jamás debe interrumpir una conversación,
            # una búsqueda ni la asignación de un lead.
            return
