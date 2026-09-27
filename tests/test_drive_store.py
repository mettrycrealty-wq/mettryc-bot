import json
import os
import unittest
from unittest.mock import patch
import httpx
from conversation_store import ConversationStoreUnavailable, conversation_store_from_environment
from drive_conversation_store import DriveConversationStore

URL = 'https://script.google.com/macros/s/test/exec'

class DriveTests(unittest.IsolatedAsyncioTestCase):
    def store(self, handler):
        return DriveConversationStore(URL, 'x'*64, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def test_restore_commit_and_clear_local_state(self):
        calls = []
        def handler(req):
            q = json.loads(req.content); calls.append(q)
            return httpx.Response(200, json={'ok': True, 'state': {'ultimo_lote': ['123'], 'filtros': {'ciudad': 'Valencia'}}})
        store = self.store(handler); sessions = {}
        async def callback():
            self.assertEqual(sessions['user']['ultimo_lote'], ['123'])
            sessions['user']['propiedad_activa_id'] = '123'
            return 'respuesta'
        self.assertEqual(await store.run('user', 'message', 180, sessions, callback), 'respuesta')
        self.assertEqual(calls[1]['state']['propiedad_activa_id'], '123')
        self.assertEqual(calls[0]['owner'], calls[1]['owner'])
        self.assertNotEqual(calls[0]['sender'], 'user')
        self.assertEqual(sessions, {})
        await store.close()

    async def test_duplicate_does_not_execute(self):
        store = self.store(lambda req: httpx.Response(200, json={'ok': True, 'duplicate': True}))
        async def callback(): self.fail('duplicate executed')
        self.assertEqual(await store.run('user', 'm', 180, {}, callback), '')
        await store.close()

    async def test_unavailable_never_runs_empty_session(self):
        for response in [httpx.Response(200, text='<html>Login</html>'), httpx.Response(200, json={'ok': False})]:
            store = self.store(lambda req: response)
            async def callback(): self.fail('storage unavailable')
            with self.assertRaises(ConversationStoreUnavailable):
                await store.run('user', 'm', 180, {}, callback)
            self.assertFalse(await store.available())
            await store.close()

    async def test_retry_commit_keeps_owner_and_does_not_repeat_callback(self):
        calls = []; callbacks = []
        def handler(req):
            q = json.loads(req.content); calls.append(q)
            if q['action'] == 'commit' and len([x for x in calls if x['action']=='commit']) == 1:
                raise httpx.ReadTimeout('lost response')
            return httpx.Response(200, json={'ok': True, 'state': None})
        store = self.store(handler); sessions = {}
        async def callback():
            callbacks.append(1); sessions['user'] = {'x': 1}; return 'ok'
        await store.run('user', 'm', 180, sessions, callback)
        commits = [x for x in calls if x['action']=='commit']
        self.assertEqual(commits[0], commits[1]); self.assertEqual(len(callbacks), 1)
        await store.close()

    async def test_failed_callback_releases_without_commit(self):
        calls=[]
        def handler(req):
            calls.append(json.loads(req.content)['action'])
            return httpx.Response(200, json={'ok': True, 'state': None})
        store=self.store(handler)
        async def callback(): raise RuntimeError('callback')
        with self.assertRaises(RuntimeError): await store.run('user', 'm', 180, {}, callback)
        self.assertEqual(calls, ['acquire', 'release'])
        await store.close()

    def test_configuration_fails_closed(self):
        with patch.dict(os.environ, {'PATY_MEMORY_BACKEND': 'drive'}, clear=True):
            with self.assertRaises(ValueError): conversation_store_from_environment()
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(conversation_store_from_environment())
        with self.assertRaises(ValueError): DriveConversationStore('http://other/exec', 'x'*64)
