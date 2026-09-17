from __future__ import annotations

from typing import Any

from .service import ConversationService


MULTIMEDIA_MARKER = "[multimedia_sin_texto]"


class ChannelAdapter:
    """Translate generic webhook payloads into the channel-agnostic AI service.

    The current legacy webhook already sends ``sender``, ``message`` and optionally
    ``message_id``. This adapter keeps those names as the preferred contract while
    accepting a few common aliases for future WhatsApp/Messenger adapters.
    """

    def __init__(self, conversation_service: ConversationService) -> None:
        self.conversation_service = conversation_service

    async def handle_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        sender, message, message_id = self.extract_message(payload)
        result = await self.conversation_service.process(sender, message)
        return {
            "ok": True,
            "sender": sender,
            "message_id": message_id,
            "reply": result.reply,
            "state": result.state.model_dump(mode="json"),
            "tool_results": [item.model_dump(mode="json") for item in result.tool_results],
        }

    @staticmethod
    def extract_message(payload: dict[str, Any]) -> tuple[str, str, str | None]:
        if not isinstance(payload, dict):
            raise ValueError("El payload del webhook debe ser un objeto JSON.")

        sender = ChannelAdapter._first_non_empty(
            payload.get("sender"),
            payload.get("from"),
            payload.get("phone"),
            payload.get("wa_id"),
            payload.get("user_id"),
        )
        if not sender:
            raise ValueError("El webhook no contiene un sender identificable.")

        message = ChannelAdapter._first_non_empty(
            payload.get("message"),
            payload.get("text"),
            payload.get("body"),
        )

        if not message:
            if ChannelAdapter._contains_media(payload):
                message = MULTIMEDIA_MARKER
            else:
                raise ValueError("El webhook no contiene un mensaje de texto.")

        message_id = ChannelAdapter._first_non_empty(
            payload.get("message_id"),
            payload.get("id_message"),
            payload.get("id"),
        )

        return sender, message, message_id

    @staticmethod
    def _first_non_empty(*values: Any) -> str | None:
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @staticmethod
    def _contains_media(payload: dict[str, Any]) -> bool:
        media_keys = {
            "image",
            "audio",
            "document",
            "video",
            "media",
            "attachment",
            "attachments",
        }
        return any(payload.get(key) for key in media_keys)


__all__ = ["ChannelAdapter", "MULTIMEDIA_MARKER"]
