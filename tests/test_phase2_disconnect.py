"""Synthetic D01 SSE/file fixtures only; no network, Store, credentials or process."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import phase2_disconnect as tool
import phase2_resume as resume


class Client:
    class Reply(io.BytesIO): status = 200
    timeout = 1
    def __init__(self, state, home, metrics, mode='running'):
        self.state, self.home, self.metrics, self.mode = state, home, metrics, mode
        self.posts = self.closed_connections = 0
        self.calls = []; self.operation = None; self.active = 0; self.blocked = None
        self.pool = [{'id': f'synthetic-account-{i}', 'enabled': True} for i in range(9)]
    def persist(self):
        resume.common.atomic_json(self.home / 'data/protocol_operations.json', {self.operation['id']: self.operation})
    def closed(self):
        self.closed_connections += 1
        if self.mode != 'completed':
            if self.mode != 'stuck':
                self.operation.update(state='unknown', delivery='unknown'); self.persist()
                self.blocked = self.operation['id']; self.active = 0
    def open(self, path, body=None, headers=None):
        self.calls.append((path, body is not None))
        if body is None:
            if path == '/admin/backend': value = {'backend': 'protocol', 'running': True, 'active_operations': self.active}
            elif path == '/admin/accounts':
                value = {'accounts': [{**a, 'protocol_blocked': self.blocked if self.operation and a['id'] == self.operation['account_id'] else None} for a in self.pool]}
            else: raise AssertionError('No unplanned read or recovery')
            return self.Reply(json.dumps(value).encode())
        if path != tool.ENDPOINT: raise AssertionError('No recovery or other POST')
        self.posts += 1
        assert (self.state.root / 'phase2-resume-run.lock').exists()
        record = self.state.load()['cases'][tool.CASE]
        assert record['status'] == 'SENDING' and record['generation_post_count'] == 1
        assert self.state.summary()['consumed']['text'] == 4
        payload = json.loads(body)
        assert payload['stream'] is True and payload['messages'][0]['content'] == tool.PROMPT
        assert 'account_id' not in payload and '300' in tool.PROMPT
        if self.mode == 'timeout': raise TimeoutError('Synthetic unknown delivery with private identifiers')
        account = 'unapproved-account' if self.mode == 'foreign' else 'synthetic-account-7'
        self.operation = {'id': 'synthetic-operation', 'account_id': account, 'kind': 'chat', 'prompt': tool.PROMPT,
            'session_key': 'operation:synthetic-operation', 'session_id': 'synthetic-session', 'vm_id': 'synthetic-vm',
            'state': 'completed' if self.mode == 'completed' else 'running',
            'delivery': 'completed' if self.mode == 'completed' else 'unknown'}
        self.active = 0 if self.mode == 'completed' else 1; self.persist()
        directory = hashlib.sha256(account.encode()).hexdigest()[:20]
        resume.common.atomic_json(self.home / 'data/protocol' / directory / 'chat_sessions.json',
            {'operation:synthetic-operation': {'session_id': 'synthetic-session', 'node_id': 'synthetic-node'}})
        # No gateway.json: the application adapter intentionally does not write it.
        rows = [{'event': 'noise_handshake_finished', 'connection_id': 17, 'vm_id': 'synthetic-vm', 'monotonic_ns': 1},
                {'event': 'gateway_write_started', 'generation_post': True, 'method': 'POST', 'route': 'chat.stream',
                 'operation_id': 'synthetic-operation', 'account_id': account, 'operation_session_id': 'synthetic-session',
                 'session_id': 'synthetic-session', 'node_id': 'synthetic-node', 'connection_id': 17,
                 'vm_id': 'foreign-vm' if self.mode == 'vm-mismatch' else 'synthetic-vm', 'monotonic_ns': 2}]
        self.metrics.write_bytes(b''.join(json.dumps(row).encode() + b'\n' for row in rows) + b'{"event":"partial')
        frames = (b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
                  b'data: {"choices":[{"delta":{"content":"001. A small blue circle appears."}}]}\n\n'
                  b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        client = self
        class Stream(self.Reply):
            def close(self):
                if not self.closed: client.closed()
                return super().close()
        return Stream(frames)


class DisconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='muse-resume-disconnect-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.home = self.root / 'test-home'
        self.original = self.root / 'original/phase2-ledger.json'; self.original.parent.mkdir()
        old = {'schema_version': 2, 'limits': resume.legacy.LIMITS, 'rounds': resume.legacy.ROUNDS,
            'budget_slot_aliases': resume.legacy.BUDGET_ALIASES, 'batches': {},
            'cases': {str(i): {'category': 'text' if i < 3 else 'image', 'units': 1, 'automatic_replay': False} for i in range(4)}}
        self.original.write_text(json.dumps(old)); self.original_bytes = self.original.read_bytes()
        self.state = resume.State(self.root / 'resumed', self.original, resume.digest(self.original_bytes), resume.BASELINE)
        resume.common.atomic_json(self.home / 'data/protocol_operations.json', {})
        self.preflight = self.root / 'preflight.json'
        self.preflight.write_text(json.dumps({'platform': 'linux', 'backend': 'protocol', 'upstream_identity_verified': True,
            'accounts': [{'alias': f'A{i:02d}', 'account_id': f'synthetic-account-{i}',
                'upstream_identity_sha256': hashlib.sha256(f'principal-{i}'.encode()).hexdigest(),
                'identity_hash_scheme': 'round-private HMAC-SHA256'} for i in range(9)]}))
        self.metrics = self.root / 'metrics.jsonl'; self.metrics.write_text('')
    def execute(self, mode='running', cleanup_timeout=.2):
        client = Client(self.state, self.home, self.metrics, mode)
        with patch.object(tool.sys, 'platform', 'linux'):
            result = tool.execute(self.state, client, self.home, self.preflight, self.metrics, cleanup_timeout)
        return client, result
    def private(self):
        return json.loads((self.state.folder(tool.CASE) / 'disconnect-receipt.json').read_text())

    def test_one_post_binds_actual_selected_account_closes_and_records_unknown(self):
        client, result = self.execute()
        self.assertEqual((client.posts, client.closed_connections), (1, 1))
        self.assertEqual((result['status'], result['observation_status']), ('UNKNOWN', 'PASS'))
        self.assertTrue(result['disconnect_observed']); self.assertTrue(result['active_cleanup_observed'])
        self.assertTrue(result['recovery_required']); self.assertFalse(result['lease_directly_observed'])
        row = self.state.load()['cases'][tool.CASE]
        self.assertEqual(row['account_id'], 'synthetic-account-7')  # No first-account preselection.
        self.assertTrue(row['operation_binding_verified'])
        self.assertEqual(row['operation_state'], 'unknown')
        self.assertEqual(row['generation_post_count'], 1)
        receipt = self.private()
        self.assertEqual(receipt['session_before_disconnect']['node_id'], 'synthetic-node')
        self.assertEqual(receipt['native_before_disconnect']['generation_write_started']['connection_id'], 17)
        self.assertEqual(receipt['operation_after_disconnect']['state'], 'unknown')
        self.assertEqual(result['consumed'], {'text': 4, 'image': 1, 'video': 0})
        for private in ('synthetic-operation', 'synthetic-account-7', 'synthetic-session', 'synthetic-vm', 'synthetic-node'):
            self.assertNotIn(private, json.dumps(result))
        with patch.object(tool.sys, 'platform', 'linux'):
            with self.assertRaises(resume.BatchError): tool.execute(self.state, client, self.home, self.preflight, self.metrics, .1)
        self.assertEqual(client.posts, 1)
        self.assertEqual(self.original.read_bytes(), self.original_bytes)

    def test_completed_fast_is_not_observed_and_never_repeated(self):
        client, result = self.execute('completed')
        self.assertEqual((result['status'], result['observation_status']), ('NOT_OBSERVED', 'NOT_OBSERVED'))
        self.assertFalse(result['disconnect_observed']); self.assertFalse(result['recovery_required'])
        self.assertTrue(result['natural_terminal'])
        with patch.object(tool.sys, 'platform', 'linux'):
            with self.assertRaises(resume.BatchError): tool.execute(self.state, client, self.home, self.preflight, self.metrics, .1)
        self.assertEqual(client.posts, 1)

    def test_unknown_timeout_stays_charged_without_recovery_or_retry(self):
        client, result = self.execute('timeout')
        self.assertEqual(result['status'], 'UNKNOWN'); self.assertTrue(result['recovery_required'])
        self.assertEqual(result['cases'][0]['error_class'] if 'error_class' in result['cases'][0] else self.private()['error_class'], 'TimeoutError')
        self.assertEqual(client.posts, 1)
        self.assertFalse(result['disconnect_observed'])
        self.assertEqual(self.state.summary()['consumed']['text'], 4)
        with patch.object(tool.sys, 'platform', 'linux'):
            with self.assertRaises(resume.BatchError): tool.execute(self.state, client, self.home, self.preflight, self.metrics, .1)
        self.assertEqual(client.posts, 1)
        self.assertFalse(any('/recover' in path for path, _ in client.calls))

    def test_foreign_account_is_rejected_and_not_recorded_as_verified(self):
        client, result = self.execute('foreign')
        self.assertEqual((result['status'], result['observation_status']), ('UNKNOWN', 'FAIL'))
        self.assertEqual(result['cases'][0]['failure_class'], 'CROSS_ACCOUNT')
        self.assertNotIn('operation_binding_verified', self.state.load()['cases'][tool.CASE])
        self.assertFalse(result['disconnect_observed']); self.assertEqual(client.posts, 1)

    def test_native_vm_conflict_is_rejected_without_claiming_disconnect(self):
        client, result = self.execute('vm-mismatch')
        self.assertEqual(result['observation_status'], 'FAIL')
        self.assertEqual(result['cases'][0]['failure_class'], 'IDENTITY_CONFLICT')
        self.assertFalse(result['disconnect_observed']); self.assertEqual(client.posts, 1)
        self.assertNotIn('native_before_disconnect', self.private())

    def test_linux_identity_pool_and_execution_lock_gates_before_send(self):
        client = Client(self.state, self.home, self.metrics)
        with patch.object(tool.sys, 'platform', 'win32'):
            with self.assertRaises(resume.BatchError): tool.execute(self.state, client, self.home, self.preflight, self.metrics)
        self.assertEqual(client.calls, []); self.assertFalse(self.state.path.exists())
        self.state.initialize()
        with resume.execution_lock(self.state), patch.object(tool.sys, 'platform', 'linux'):
            with self.assertRaises(resume.BatchError): tool.execute(self.state, client, self.home, self.preflight, self.metrics)
        self.assertEqual(client.calls, [])
        client.pool.pop()
        with patch.object(tool.sys, 'platform', 'linux'):
            with self.assertRaises(resume.BatchError): tool.execute(self.state, client, self.home, self.preflight, self.metrics)
        self.assertEqual(client.posts, 0); self.assertNotIn('D01', self.state.load()['batches'])


if __name__ == '__main__': unittest.main()
