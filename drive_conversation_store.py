"""Memoria en Drive mediante una aplicación web de Apps Script privada por token."""
import asyncio
import hashlib
import os
import uuid
from urllib.parse import urlparse

import httpx
from conversation_store import ConversationStoreUnavailable


class DriveConversationStore:
    backend_name = "google_drive"

    def __init__(self, url, token, *, client=None, ttl=604800, turn_timeout=240):
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.netloc != "script.google.com"
                or not parsed.path.startswith("/macros/s/") or not parsed.path.endswith("/exec")
                or parsed.query or parsed.fragment or len(token) < 32):
            raise ValueError("Configuración de memoria Drive inválida")
        self.url, self.token = url, token
        self.ttl, self.turn_timeout = ttl, turn_timeout
        self.client = client or httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False)

    @classmethod
    def from_environment(cls):
        return cls(os.getenv("PATY_DRIVE_URL", "").strip(), os.getenv("PATY_DRIVE_TOKEN", "").strip(),
                   ttl=max(60, int(os.getenv("PATY_SESSION_TTL_SECONDS", "604800"))),
                   turn_timeout=min(240, max(5, int(os.getenv("PATY_TURN_TIMEOUT_SECONDS", "240")))))

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    async def request(self, action, **payload):
        # Repetir sólo errores de transporte: acquire y commit son idempotentes.
        for attempt in range(2):
            try:
                response = await self.client.post(self.url, json={"token": self.token, "action": action, **payload})
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise ConversationStoreUnavailable("drive_operacion_rechazada")
                return result
            except httpx.TransportError:
                if attempt == 0:
                    continue
                raise ConversationStoreUnavailable("drive_transporte") from None
            except (httpx.HTTPError, ValueError):
                raise ConversationStoreUnavailable("drive_respuesta_invalida") from None

    async def run(self, sender, message_key, duplicate_ttl, sessions, callback):
        args = dict(sender=self.digest(sender), owner=uuid.uuid4().hex)
        message = self.digest(message_key) if message_key else ""
        try:
            result = await self.request("acquire", **args, message=message, lease=self.turn_timeout + 120)
            if result.get("duplicate"):
                return ""
            state = result.get("state")
            if state is not None and not isinstance(state, dict):
                raise ConversationStoreUnavailable("drive_sesion_invalida")
            sessions.pop(sender, None)
            if state is not None:
                sessions[sender] = state
            try:
                response = await asyncio.wait_for(callback(), timeout=self.turn_timeout)
            except asyncio.TimeoutError:
                raise ConversationStoreUnavailable("drive_turno_agotado") from None
            await self.request("commit", **args, state=sessions.get(sender, {}),
                               message=message, duplicate_ttl=duplicate_ttl, ttl=self.ttl)
            return response
        finally:
            sessions.pop(sender, None)
            try:
                await self.request("release", **args)
            except ConversationStoreUnavailable:
                pass  # La concesión vence; nunca se libera la de otro proceso.

    async def reset(self, sender):
        await self.request("reset", sender=self.digest(sender))

    async def available(self):
        try:
            return (await self.request("ping"))["ok"]
        except ConversationStoreUnavailable:
            return False

    async def close(self):
        await self.client.aclose()
