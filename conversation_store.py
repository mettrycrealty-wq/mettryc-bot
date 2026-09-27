"""Persistencia opcional de conversaciones y deduplicación entre procesos."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os

from redis.asyncio import Redis
from redis.exceptions import RedisError


class ConversationStoreUnavailable(RuntimeError):
    pass


class RedisConversationStore:
    backend_name = "redis"
    def __init__(self, client, *, prefix="mettryc:paty:v1", ttl=604800, turn_timeout=240):
        self.client = client
        self.prefix = prefix
        self.ttl = ttl
        self.turn_timeout = turn_timeout

    @classmethod
    def from_environment(cls):
        url = os.getenv("PATY_REDIS_URL", "").strip()
        if not url:
            return None
        return cls(
            Redis.from_url(url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3),
            prefix=os.getenv("PATY_REDIS_PREFIX", "mettryc:paty:v1"),
            ttl=max(60, int(os.getenv("PATY_SESSION_TTL_SECONDS", "604800"))),
            turn_timeout=max(5, int(os.getenv("PATY_TURN_TIMEOUT_SECONDS", "240"))),
        )

    def key(self, sender, kind):
        # No se guardan números de teléfono en nombres de claves.
        digest = hashlib.sha256(sender.encode()).hexdigest()
        return f"{self.prefix}:{{{digest}}}:{kind}"

    async def run(self, sender, message_key, duplicate_ttl, sessions, callback):
        lock = self.client.lock(
            self.key(sender, "lock"), timeout=self.turn_timeout + 60,
            blocking_timeout=10, thread_local=False,
        )
        acquired = False
        try:
            acquired = await lock.acquire()
            if not acquired:
                raise ConversationStoreUnavailable("conversacion_ocupada")
            message_digest = hashlib.sha256(message_key.encode()).hexdigest()
            duplicate_key = self.key(sender, "message:" + message_digest)
            if message_key and await self.client.exists(duplicate_key):
                return ""
            saved = await self.client.get(self.key(sender, "state"))
            if saved:
                state = json.loads(saved)
                if not isinstance(state, dict):
                    raise ConversationStoreUnavailable("sesion_invalida")
                sessions[sender] = state
            else:
                sessions.pop(sender, None)
            # La ejecución termina antes del vencimiento del lock compartido.
            response = await asyncio.wait_for(callback(), timeout=self.turn_timeout)
            state_json = json.dumps(sessions[sender], ensure_ascii=False)
            async with self.client.pipeline(transaction=True) as pipe:
                pipe.set(self.key(sender, "state"), state_json, ex=self.ttl)
                if message_key:
                    pipe.set(duplicate_key, "done", ex=duplicate_ttl)
                await pipe.execute()
            return response
        except (RedisError, ValueError, asyncio.TimeoutError) as exc:
            # Con Redis configurado no se crea una conversación vacía si falla.
            raise ConversationStoreUnavailable(type(exc).__name__) from exc
        finally:
            sessions.pop(sender, None)
            if acquired:
                try:
                    await lock.release()
                except RedisError:
                    # Ya tiene vencimiento; nunca liberar un lock de otro proceso.
                    pass

    async def reset(self, sender):
        try:
            async with self.client.lock(
                self.key(sender, "lock"), timeout=30, blocking_timeout=10,
            ):
                await self.client.delete(self.key(sender, "state"))
        except RedisError as exc:
            raise ConversationStoreUnavailable(type(exc).__name__) from exc

    async def available(self):
        try:
            return bool(await self.client.ping())
        except RedisError:
            return False

    async def close(self):
        await self.client.aclose()


def conversation_store_from_environment():
    backend = os.getenv("PATY_MEMORY_BACKEND", "").strip().lower()
    if not backend:
        backend = "drive" if os.getenv("PATY_DRIVE_URL") else "redis" if os.getenv("PATY_REDIS_URL") else "memory"
    if backend == "drive":
        from drive_conversation_store import DriveConversationStore
        return DriveConversationStore.from_environment()
    if backend == "redis":
        store = RedisConversationStore.from_environment()
        if store is None:
            raise ValueError("Falta PATY_REDIS_URL")
        return store
    if backend == "memory":
        return None
    raise ValueError("PATY_MEMORY_BACKEND debe ser drive, redis o memory")
