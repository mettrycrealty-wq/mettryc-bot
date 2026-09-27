"""Sesiones compartidas: dos clientes independientes y fallos del almacén."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import fakeredis
import fakeredis.aioredis
from redis.exceptions import ConnectionError

from conversation_store import RedisConversationStore, ConversationStoreUnavailable


class ConversationStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server = fakeredis.FakeServer()
        self.clients = [fakeredis.aioredis.FakeRedis(server=server, decode_responses=True) for _ in range(2)]
        self.stores = [RedisConversationStore(client, ttl=60, turn_timeout=2) for client in self.clients]
        self.sessions = [{}, {}]
        self.sender = "584120000099"

    async def asyncTearDown(self):
        for store in self.stores:
            await store.close()

    async def test_new_worker_recovers_property_batch_and_lead(self):
        async def first():
            self.sessions[0][self.sender] = {
                "ultimo_lote": ["1000001", "1000002"],
                "propiedad_activa_id": "1000002",
                "filtros": {"tipo_operacion": "venta"},
                "lead": {"nombre": "Persona Ejemplo"},
            }
            return "primera respuesta"
        await self.stores[0].run(self.sender, "turn-1", 180, self.sessions[0], first)
        self.assertEqual(self.sessions[0], {})
        async def second():
            recovered = self.sessions[1][self.sender]
            self.assertEqual(recovered["ultimo_lote"][1], "1000002")
            self.assertEqual(recovered["propiedad_activa_id"], "1000002")
            self.assertEqual(recovered["lead"]["nombre"], "Persona Ejemplo")
            return "segunda respuesta"
        self.assertEqual(await self.stores[1].run(self.sender, "turn-2", 180, self.sessions[1], second), "segunda respuesta")

    async def test_duplicate_is_not_executed_by_another_worker(self):
        calls = []
        async def first():
            calls.append(1)
            self.sessions[0][self.sender] = {"turn": 1}
            return "ok"
        await self.stores[0].run(self.sender, "same-id", 180, self.sessions[0], first)
        other = AsyncMock()
        self.assertEqual(await self.stores[1].run(self.sender, "same-id", 180, self.sessions[1], other), "")
        other.assert_not_awaited()
        self.assertEqual(calls, [1])

    async def test_concurrent_turns_are_serialized_across_workers(self):
        async def run(index):
            async def callback():
                state = self.sessions[index].setdefault(self.sender, {"turns": []})
                before = list(state["turns"])
                await asyncio.sleep(0.02)
                state["turns"] = before + [index]
                return "ok"
            return await self.stores[index].run(self.sender, f"msg-{index}", 180, self.sessions[index], callback)
        await asyncio.gather(run(0), run(1))
        state = json.loads(await self.clients[0].get(self.stores[0].key(self.sender, "state")))
        self.assertEqual(sorted(state["turns"]), [0, 1])

    async def test_failed_turn_can_retry_without_storing_partial_state(self):
        async def failing():
            self.sessions[0][self.sender] = {"partial": True}
            raise RuntimeError("failure")
        with self.assertRaises(RuntimeError):
            await self.stores[0].run(self.sender, "retry-id", 180, self.sessions[0], failing)
        async def recovered():
            self.assertNotIn(self.sender, self.sessions[1])
            self.sessions[1][self.sender] = {"ok": True}
            return "ok"
        self.assertEqual(await self.stores[1].run(self.sender, "retry-id", 180, self.sessions[1], recovered), "ok")

    async def test_outage_never_processes_with_empty_context(self):
        callback = AsyncMock()
        with patch.object(self.clients[0], "set", new=AsyncMock(side_effect=ConnectionError("offline"))):
            with self.assertRaises(ConversationStoreUnavailable):
                await self.stores[0].run(self.sender, "message", 180, self.sessions[0], callback)
        callback.assert_not_awaited()

    async def test_timeout_releases_lock_and_does_not_mark_message_done(self):
        self.stores[0].turn_timeout = 0.01
        async def slow():
            await asyncio.sleep(1)
        with self.assertRaises(ConversationStoreUnavailable):
            await self.stores[0].run(self.sender, "timed", 180, self.sessions[0], slow)
        async def retry():
            self.sessions[1][self.sender] = {"ok": True}
            return "retry"
        self.assertEqual(await self.stores[1].run(self.sender, "timed", 180, self.sessions[1], retry), "retry")

    async def test_reset_and_ttl_do_not_expose_phone_in_keys(self):
        async def callback():
            self.sessions[0][self.sender] = {"ok": True}
            return "ok"
        await self.stores[0].run(self.sender, "message", 180, self.sessions[0], callback)
        keys = await self.clients[0].keys("*")
        self.assertTrue(all(self.sender not in key for key in keys))
        self.assertGreater(await self.clients[0].ttl(self.stores[0].key(self.sender, "state")), 0)
        await self.stores[1].reset(self.sender)
        self.assertIsNone(await self.clients[0].get(self.stores[0].key(self.sender, "state")))


if __name__ == "__main__":
    unittest.main()
