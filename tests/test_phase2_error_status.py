"""Real installed SDK error types at the Backend boundary; no network or service."""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('phase2_error_network_guard', ROOT/'tests/protocol/network_guard/sitecustomize.py')
guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
sys.path.insert(0, str(ROOT))


class RealSDKErrorBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.import_home = tempfile.TemporaryDirectory(prefix='phase2-error-import-')
        cls.environment = mock.patch.dict(os.environ, {'MUSE2API_HOME': cls.import_home.name,
            'MUSE_BACKEND':'protocol','MUSE2API_KEY':'synthetic-error-key'})
        cls.environment.start()
        cls.backend_module = importlib.import_module('protocol_backend')
        cls.store_module = importlib.import_module('store')
        cls.gateway_module = importlib.import_module('muse2api.gateway')
        cls.RpcError = cls.gateway_module.RpcError
        cls.ProtocolError = cls.gateway_module.ProtocolOperationError
        if not Path(cls.gateway_module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
            raise RuntimeError('The worktree venv installed SDK, not a copied stub, is required')

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()
        cls.import_home.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='phase2-error-store-')
        self.addCleanup(self.directory.cleanup)
        from config import Config
        cfg = Config(base_dir=self.directory.name, backend='protocol', api_key='synthetic-error-key')
        cfg.ensure_dirs()
        self.store = self.store_module.Store(cfg)
        account = self.store.add_account({'hatch_sess':'synthetic-only'}, 'controlled-account')
        self.backend = self.backend_module.ProtocolBackend(self.store, cfg)
        op = self.store.begin_protocol_operation('controlled-rpc-operation', account['id'], 'chat', 'synthetic prompt')
        self.store.update_protocol_operation(op['id'], state='running', phase='history',
            delivery='accepted', session_id='synthetic-session', message_id='synthetic-message')
        self.operation = self.store.get_protocol_operation(op['id'])

    def rpc(self, status):
        return self.RpcError('upstream https://fixture.invalid/?token=NEVER_LEAK_URL',
            status=status, body=b'{"secret":"NEVER_LEAK_BODY","quota":"not a classification"}')

    def test_real_rpc_429_has_machine_rate_limit_code_and_status(self):
        error = self.backend._map_error(self.rpc(429), self.operation)
        self.assertEqual(error.code, 'upstream_rate_limited')
        self.assertEqual(error.public().get('upstream_status'), 429)

    def test_recorded_rate_limit_keeps_original_identity_and_status(self):
        error = self.backend._record_error(self.operation, self.rpc(429))
        record = self.store.get_protocol_operation(self.operation['id'])
        self.assertEqual(record['code'], 'upstream_rate_limited')
        self.assertEqual(record.get('upstream_status'), 429)
        self.assertEqual(record['account_id'], self.operation['account_id'])
        self.assertEqual(record['session_id'], self.operation['session_id'])
        self.assertEqual(record['state'], 'unknown')
        self.assertEqual(record['delivery'], 'accepted')
        self.assertEqual(len(self.store.protocol_operations), 1)
        self.assertEqual(self.store.get_account(record['account_id'])['protocol_blocked'], record['id'])
        self.assertIs(self.backend._map_error(error), error)

    def test_real_rpc_401_and_403_remain_distinct_without_quota_guess(self):
        for status, code in ((401,'session_unauthorized'),(403,'upstream_access_denied')):
            with self.subTest(status=status):
                error = self.backend._map_error(self.rpc(status), self.operation)
                self.assertEqual(error.code, code)
                self.assertEqual(error.public().get('upstream_status'), status)
                self.assertNotIn('quota', error.code)

    def test_status_is_not_invented_from_generic_error_text_or_default_zero(self):
        for original in (RuntimeError('HTTP 429 quota'), self.RpcError('status unknown')):
            with self.subTest(original=type(original).__name__):
                error = self.backend._map_error(original, self.operation)
                self.assertEqual(error.code, 'protocol_error')
                self.assertNotIn('upstream_status', error.public())

    def test_retry_delivery_and_sanitization_contract_are_unchanged(self):
        original = self.ProtocolError('connect error',code='connect_unavailable',phase='connect',
            delivery='not_sent',retryable=True)
        mapped = self.backend._map_error(original)
        self.assertEqual(mapped.code,'connect_unavailable')
        self.assertTrue(mapped.retryable)
        self.assertEqual(mapped.delivery,'not_sent')
        for status in (429,401,403):
            error = self.backend._record_error(self.operation, self.rpc(status))
            self.assertFalse(error.retryable)
            self.assertEqual(error.delivery,'accepted')
            self.assertEqual(error.task_status,'unknown')
            visible=json.dumps({'public':error.public(),'message':str(error),
                'persisted':self.store.get_protocol_operation(self.operation['id'])},ensure_ascii=False)
            self.assertNotIn('NEVER_LEAK_URL',visible)
            self.assertNotIn('NEVER_LEAK_BODY',visible)


    def sdk_connection(self, *, ack_status=None, history_status=None, registration_status=None):
        test = self
        class Connection:
            is_closed = False
            def __init__(self):
                self.submissions = self.history_reads = self.registration_reads = 0
            async def request(self, method, path, params=None, **kwargs):
                if path.endswith('/nodes/register'):
                    self.registration_reads += 1
                    if registration_status:
                        raise test.rpc(registration_status)
                if path.endswith('/chat/history'):
                    self.history_reads += 1
                    if history_status:
                        raise test.rpc(history_status)
                return SimpleNamespace(raise_for_status=lambda:None, json=lambda:{'chat_events':[]})
            async def ack(self, method, path, params=None, **kwargs):
                self.submissions += 1
                if ack_status:
                    raise test.rpc(ack_status)
                return {'message_id':'synthetic-message'}
        return Connection()

    def run_real_sdk(self, connection):
        from muse2api.chat import ChatService, ChatSession
        guard = getattr(self.backend_module, '_guard_rate_limited_connection', lambda c:c)
        self.assertIs(guard(connection), connection)
        async def run():
            async def connect():
                return None, None, connection
            settings=SimpleNamespace(state_dir=Path(self.directory.name)/'sdk',
                approval_policy='off',approval_ask='allow_once',timezone='UTC')
            service=ChatService(settings,SimpleNamespace(connect=connect))
            try:
                await service._run_thread('synthetic prompt',
                    ChatSession('synthetic-session','synthetic-node'),on_delta=None,timeout=0.12)
            except Exception as exc:
                return self.backend._map_error(exc,self.operation)
            self.fail('Controlled rejection unexpectedly succeeded')
        return asyncio.run(run())

    def test_real_sdk_ack_429_stops_before_any_history_poll(self):
        conn=self.sdk_connection(ack_status=429)
        error=self.run_real_sdk(conn)
        print(json.dumps({'sdk_scenario':'ack429','code':error.code,
            'status':error.public().get('upstream_status'),'submissions':conn.submissions,
            'history_reads':conn.history_reads}))
        self.assertEqual(error.code,'upstream_rate_limited')
        self.assertEqual(error.public().get('upstream_status'),429)
        self.assertEqual(conn.submissions,1)
        self.assertEqual(conn.history_reads,0)
        self.assertFalse(error.retryable)

    def test_real_sdk_optional_registration_429_cannot_continue_to_generation(self):
        conn=self.sdk_connection(registration_status=429)
        error=self.run_real_sdk(conn)
        print(json.dumps({'sdk_scenario':'registration429','code':error.code,
            'submissions':conn.submissions,'history_reads':conn.history_reads}))
        self.assertEqual(error.code,'upstream_rate_limited')
        self.assertEqual(conn.submissions,0)
        self.assertEqual(conn.history_reads,0)

    def test_real_sdk_history_429_has_one_read_and_preserved_status(self):
        conn=self.sdk_connection(history_status=429)
        error=self.run_real_sdk(conn)
        self.assertEqual(error.code,'upstream_rate_limited')
        self.assertEqual(error.public().get('upstream_status'),429)
        self.assertEqual(conn.submissions,1)
        self.assertEqual(conn.history_reads,1)

    def test_real_sdk_media_download_429_remains_delivery_failed_with_status(self):
        from muse2api.media import MediaRef
        test=self
        class MediaConnection:
            def __init__(self): self.reads=0
            async def iter_bytes(self,*args,**kwargs):
                self.reads+=1
                raise test.rpc(429)
                yield b''
        connection=MediaConnection()
        operation=self.store.begin_protocol_operation('controlled-media-operation',
            self.operation['account_id'],'image','synthetic completed media')
        self.store.update_protocol_operation(operation['id'],state='running',delivery='completed',
            session_id='synthetic-session',message_id='synthetic-message')
        operation=self.store.get_protocol_operation(operation['id'])
        runtime=SimpleNamespace(conn=connection,proxy=None,settings=SimpleNamespace(impersonate='chrome'))
        async def run():
            try:
                await self.backend._deliver(runtime,operation,MediaRef(kind='image',path='fixture.png'),1)
            except Exception as exc:
                return self.backend._record_error(operation,exc)
            self.fail('Controlled media rejection unexpectedly succeeded')
        error=asyncio.run(run())
        record=self.store.get_protocol_operation(operation['id'])
        print(json.dumps({'sdk_scenario':'media429','code':error.code,
            'status':error.public().get('upstream_status'),'read_attempts':connection.reads}))
        self.assertEqual(error.code,'media_delivery_failed')
        self.assertEqual(error.public().get('upstream_status'),429)
        self.assertEqual(record['upstream_status'],429)
        self.assertEqual(record['state'],'delivery_failed')
        self.assertEqual(record['delivery'],'completed')
        self.assertFalse(error.retryable)
        self.assertEqual(connection.reads,2,'Existing SDK two-route read fallback is not a new generation')
        self.assertEqual(len(self.store.protocol_operations),2)
        self.assertFalse(list(Path(self.backend.cfg.media_dir).glob('*.pending')))

    def test_adapter_guard_retains_normal_returns_and_non429_exception_identity(self):
        connection=self.sdk_connection()
        guard=getattr(self.backend_module,'_guard_rate_limited_connection',lambda c:c)
        self.assertIs(guard(connection),connection)
        async def run():
            response=await connection.request('GET','/model')
            self.assertEqual(response.json(),{'chat_events':[]})
            self.assertEqual(await connection.ack('POST','/chat/stream'),{'message_id':'synthetic-message'})
        asyncio.run(run())
        error=self.rpc(403)
        other=self.sdk_connection()
        async def reject(*args,**kwargs): raise error
        other.request=reject
        self.assertIs(guard(other),other)
        async def unchanged():
            with self.assertRaises(self.RpcError) as caught:
                await other.request('GET','/chat/history')
            self.assertIs(caught.exception,error)
        asyncio.run(unchanged())


    def test_adapter_guard_is_idempotent_and_does_not_poison_later_recovery_task(self):
        connection=self.sdk_connection(ack_status=429)
        guard=self.backend_module._guard_rate_limited_connection
        self.assertIs(guard(connection),connection)
        first=connection.ack
        self.assertIs(guard(connection).ack,first)
        async def limited_operation():
            with self.assertRaises(self.backend_module.BackendOperationError):
                await connection.ack('POST','/chat/stream')
            with self.assertRaises(self.backend_module.BackendOperationError):
                await connection.request('GET','/chat/history')
        asyncio.run(limited_operation())
        self.assertEqual(connection.submissions,1)
        self.assertEqual(connection.history_reads,0)
        async def explicit_later_read():
            result=await connection.request('GET','/chat/history')
            self.assertEqual(result.json(),{'chat_events':[]})
        asyncio.run(explicit_later_read())
        self.assertEqual(connection.submissions,1)
        self.assertEqual(connection.history_reads,1)


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(RealSDKErrorBoundaryTests))
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
