from __future__ import annotations

import asyncio
from typing import Any

from .engine import MettrycAIEngine
from .schemas import ConversationState, EngineResult


class ConversationService:
    """Keep one independent conversation state per channel sender.

    This layer is deliberately provider/channel agnostic. WhatsApp, Facebook
    Messenger, or another adapter only needs to provide a stable ``sender`` and
    the incoming text message.
    """

    def __init__(
        self,
        engine: MettrycAIEngine,
        *,
        max_sessions: int = 5000,
    ) -> None:
        self.engine = engine
        self.max_sessions = max(1, max_sessions)
        self._states: dict[str, ConversationState] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def normalize_sender(sender: str) -> str:
        """Normalize a sender identifier without changing its identity semantics."""

        normalized = str(sender or "").strip()
        if not normalized:
            raise ValueError("El sender no puede estar vacío.")
        return normalized

    async def process(
        self,
        sender: str,
        message: str,
    ) -> EngineResult:
        """Process one message and persist the resulting state for that sender."""

        sender_key = self.normalize_sender(sender)
        text = str(message or "").strip()
        if not text:
            raise ValueError("El mensaje no puede estar vacío.")

        # Reading and writing the state are protected so simultaneous webhook
        # deliveries cannot overwrite one another with stale conversation state.
        async with self._lock:
            state = self._states.get(sender_key)
            result = await self.engine.process(text, state)
            self._states[sender_key] = result.state
            self._trim_sessions_locked()
            return result

    async def get_state(self, sender: str) -> ConversationState | None:
        """Return a copy of the current state for a sender, if it exists."""

        sender_key = self.normalize_sender(sender)
        async with self._lock:
            state = self._states.get(sender_key)
            return state.model_copy(deep=True) if state else None

    async def clear_session(self, sender: str) -> bool:
        """Clear one sender's conversation state."""

        sender_key = self.normalize_sender(sender)
        async with self._lock:
            return self._states.pop(sender_key, None) is not None

    async def clear_all(self) -> None:
        """Clear all in-memory conversation states."""

        async with self._lock:
            self._states.clear()

    def session_count(self) -> int:
        """Return the number of active in-memory sessions."""

        return len(self._states)

    def _trim_sessions_locked(self) -> None:
        if len(self._states) <= self.max_sessions:
            return
        # Dicts preserve insertion order. Remove the oldest sessions first.
        overflow = len(self._states) - self.max_sessions
        for sender_key in list(self._states)[:overflow]:
            self._states.pop(sender_key, None)


__all__ = ["ConversationService"]
