from __future__ import annotations

import asyncio

from .engine import AgenteVirtualEngine


class AgenteVirtualService:
    """Capa de servicio que reutiliza el almacenamiento y locks de main.py."""

    def __init__(self, engine: AgenteVirtualEngine | None = None) -> None:
        self.engine = engine or AgenteVirtualEngine()

    async def process(
        self,
        sender: str,
        message: str,
        image_source: str | None = None,
    ) -> str:
        sender_key = str(sender or "").strip()
        if not sender_key:
            raise ValueError("El sender no puede estar vacío.")

        legacy = self.engine.bridge.load()
        lock = legacy.locks_usuarios.setdefault(
            sender_key,
            asyncio.Lock(),
        )

        async with lock:
            return await self.engine.process(
                sender_key,
                message,
                image_source=image_source,
            )

    async def close(self) -> None:
        legacy = self.engine.bridge.legacy
        client = getattr(legacy, "http_client", None) if legacy else None

        # main.py controla normalmente este cliente con FastAPI. Solo se cierra
        # aquí si el servicio lo creó por separado y se usa como proceso autónomo.
        if client is not None and not client.is_closed:
            await client.aclose()


__all__ = ["AgenteVirtualService"]
