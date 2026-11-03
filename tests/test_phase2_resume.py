"""Resume ledger safety with synthetic files/clients only; no Store or network."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import phase2_resume as resume


class NeverSend:
    def __init__(self): self.calls = []
    def open(self, path, body=None, headers=None):
        self.calls.append((path, body is not None))
        raise TimeoutError('Synthetic unknown result; never repeat')


class RecoveryClient:
    class Reply(io.BytesIO): status = 200
    def __init__(self, operation='original-op'):
        self.operation, self.calls = operation, []
    def open(self, path, body=None, headers=None):
        self.calls.append((path, body is not None))
        if path != '/admin/protocol/operations/original-op/recover':
            raise AssertionError('Only the original operation recovery is permitted')
        return self.Reply(json.dumps({'ok': True, 'operation_id': self.operation,
            'resubmitted': False, 'result': {'text': 'PHASE2_L2_001'}}).encode())


class ResumeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='muse-phase2-resume-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.root / 'original' / 'phase2-ledger.json'
        self.original.parent.mkdir()
        rows = {f'L1-{i:03d}': {'case_id': f'L1-{i:03d}', 'round': 'L1',
                    'category': 'text' if i < 4 else 'image', 'units': 1,
                    'automatic_replay': False, 'generation_post_count': 1,
                    'status': 'PASS' if i < 4 else 'FAIL'} for i in range(1, 5)}
        value = {'schema_version': 2, 'limits': resume.legacy.LIMITS, 'rounds': resume.legacy.ROUNDS,
                 'budget_slot_aliases': resume.legacy.BUDGET_ALIASES, 'batches': {'L1': {'status': 'INCOMPLETE'}}, 'cases': rows}
        self.original.write_text(json.dumps(value), encoding='utf-8')
        self.original_bytes = self.original.read_bytes()
        self.sha = hashlib.sha256(self.original_bytes).hexdigest()
        self.state = self.new_state()

    def new_state(self, root=None):
        return resume.State(root or self.root / 'resume', self.original, self.sha, dict(resume.BASELINE))

    def args(self, name='L2'):
        operations = self.root / 'synthetic-operations.json'
        operations.write_text('{}', encoding='utf-8')
        return SimpleNamespace(round=name, diagnostic_kind=None, text_count=100,
            preflight=self.root / 'preflight.json', operations_file=operations,
            task_timeout=1, ffprobe=None, ffmpeg=None, case=None, command='recover')

    def preflight(self, count):
        value = {'platform': 'linux', 'backend': 'protocol', 'upstream_identity_verified': True,
                 'accounts': [{'alias': f'A{i:02d}', 'account_id': f'account-{i}',
                   'upstream_identity_sha256': hashlib.sha256(f'synthetic-identity-{i}'.encode()).hexdigest(),
                   'identity_hash_scheme': 'round-private HMAC-SHA256'} for i in range(count)]}
        path = self.root / 'preflight.json'; path.write_text(json.dumps(value), encoding='utf-8')
        return path, value

    def reserve(self, case='L2-001', category='text'):
        self.state.reserve(case, category, '/v1/chat/completions', '1' * 64)

    def test_readonly_status_and_explicit_baseline_validation(self):
        self.assertEqual(self.state.summary()['consumed'], {'text': 3, 'image': 1, 'video': 0})
        self.assertEqual(self.state.summary()['remaining'], {'text': 557, 'image': 34, 'video': 5})
        self.assertFalse(self.state.root.exists())
        self.assertFalse(self.state.authority.exists())
        with self.assertRaises(resume.BatchError):
            resume.State(self.root / 'wrong', self.original, self.sha, {'text': 0, 'image': 0, 'video': 0})
        with self.assertRaises(resume.BatchError):
            resume.State(self.root / 'wrong', self.original, '0' * 64, resume.BASELINE)
        self.assertEqual(self.original.read_bytes(), self.original_bytes)

    def test_authority_prevents_empty_root_fork_and_original_stays_unchanged(self):
        self.state.initialize()
        self.state.begin('L2', '2' * 64)
        self.reserve()
        with self.assertRaises(resume.BatchError): self.new_state(self.root / 'empty-fork')
        recovered = self.new_state()
        self.assertEqual(recovered.summary('L2')['consumed']['text'], 4)
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        self.assertEqual(json.loads(self.state.authority.read_text())['resume_ledger'], str(self.state.path))

    def test_missing_authorized_journal_cannot_reset_same_root(self):
        self.state.begin('L2', '2' * 64); self.reserve()
        self.state.path.unlink()  # Synthetic damage; production files are never touched.
        with self.assertRaises(resume.BatchError): self.new_state().initialize()
        with self.assertRaises(resume.BatchError): self.new_state().summary()
        self.assertTrue(self.state.authority.exists())
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        with self.assertRaises(resume.BatchError): resume.external(resume.ROOT / 'private-must-not-exist')

    def test_nine_account_partials_keep_ten_gate_and_share_soak_slot(self):
        path, data = self.preflight(8)
        for name in ('SOAK9', 'SOAK9TEXT'):
            with self.subTest(name=name):
                with self.assertRaises(resume.BatchError): resume.identities(path, resume.ROUNDS[name])
        path, data = self.preflight(9)
        for name in ('SOAK9', 'SOAK9TEXT'):
            with self.subTest(name=name):
                self.assertEqual(len(resume.identities(path, resume.ROUNDS[name])), 9)
                self.assertEqual(resume.offsets(name, len(resume.plan(name)))[-1], 7200)
        for name in ('C5R', 'SOAKR'):
            with self.subTest(name=name):
                with self.assertRaises(resume.BatchError): resume.identities(path, resume.ROUNDS[name])
        self.state.begin('SOAK9TEXT', '2' * 64)
        for name in ('SOAK9', 'SOAKR'):
            with self.assertRaises(resume.BatchError): self.state.begin(name, '2' * 64)
        self.assertTrue(self.state.summary('SOAK9TEXT')['partial_scope'])
        self.assertEqual(self.state.summary('SOAK9TEXT')['original_target_round'], 'SOAKR')

    def test_fixed_plans_old_policy_unchanged_and_partial_c3_is_not_mixed_pass(self):
        old_rounds, old_limits = copy.deepcopy(resume.legacy.ROUNDS), dict(resume.legacy.LIMITS)
        self.assertEqual(sum(resume.LIMITS.values()), 600)
        for name, spec in resume.ROUNDS.items():
            items = resume.plan(name)
            self.assertEqual(Counter(i['category'] for i in items), {k: v for k, v in spec.items() if k in resume.LIMITS and v})
            self.assertEqual(len(items), len({i['case_id'] for i in items}))
        self.assertEqual({'image', 'edit', 'video'}, {i['kind'] for i in resume.plan('L2') if i['category'] != 'text'})
        self.state.begin('C3TEXT', '2' * 64, resume.spec_for('C3TEXT', text_count=5))
        with self.assertRaises(resume.BatchError): self.state.begin('C3R', '2' * 64)
        data = self.state.load()
        data['cases'] = {f'C3TEXT-{i:03d}': {'case_id': f'C3TEXT-{i:03d}', 'round': 'C3TEXT',
                          'category': 'text', 'status': 'PASS', 'account_alias': f'A{i}', 'generation_post_count': 1}
                         for i in range(5)}
        def finish(event, value): data['batches'][value['round']].update(value['changes'])
        with patch.object(self.state, 'load', return_value=data), patch.object(self.state, '_record', side_effect=finish):
            result = self.state.finish('C3TEXT', 10, 3, {3: 1})
        self.assertEqual((result['status'], result['mixed_target_status']), ('PARTIAL_PASS', 'NOT_RUN'))
        with self.assertRaises(resume.BatchError): resume.spec_for('C3TEXT', text_count=101)
        self.assertEqual((resume.legacy.ROUNDS, resume.legacy.LIMITS), (old_rounds, old_limits))

    def test_concurrent_global_reservations_include_baseline(self):
        # Charge all 525 planned text intents without any network or generation.
        for name in ('L2', 'C3R', 'C5R', 'SOAKR'):
            self.state.begin(name, '2' * 64)
            for i in range(1, resume.ROUNDS[name]['text'] + 1): self.reserve(f'{name}-{i:03d}')
        for i in range(1, 37):
            self.state.begin(f'D{i:02d}', '2' * 64, resume.spec_for(f'D{i:02d}', diagnostic_kind='chat'))
        def reserve(i):
            try: self.reserve(f'D{i:02d}-001'); return True
            except resume.BatchError: return False
        with ThreadPoolExecutor(max_workers=12) as pool: accepted = list(pool.map(reserve, range(1, 37)))
        self.assertEqual(sum(accepted), 32)
        result = self.state.summary()
        self.assertEqual(result['consumed']['text'], 560)
        self.assertEqual(result['remaining']['text'], 0)
        self.assertEqual(result['diagnostic_cases_consumed'], 32)
        self.assertEqual(self.original.read_bytes(), self.original_bytes)

    def test_diagnostics_are_explicit_single_case_and_bounded(self):
        with self.assertRaises(resume.BatchError): resume.spec_for('D01')
        with self.assertRaises(resume.BatchError): resume.spec_for('D37', diagnostic_kind='chat')
        for name, kind in (('D01', 'chat'), ('D02', 'edit'), ('D03', 'video')):
            spec = resume.spec_for(name, diagnostic_kind=kind)
            self.assertEqual(len(resume.plan(name, spec)), 1)
            self.state.begin(name, '2' * 64, spec)
            self.reserve(name + '-001', resume.KINDS[kind])
            with self.assertRaises(resume.BatchError): self.reserve(name + '-002', resume.KINDS[kind])
        self.assertEqual(self.state.summary()['diagnostic_cases_consumed'], 3)

    def test_single_diagnostic_passes_with_one_selected_account_but_needs_two_pool_identities(self):
        args = self.args('D02'); args.diagnostic_kind = 'chat'
        self.preflight(1); client = NeverSend()
        with patch.object(resume.sys, 'platform', 'linux'):
            with self.assertRaisesRegex(resume.BatchError, 'identity threshold'):
                resume.run_batch(self.state, client, args)
        self.assertEqual(client.calls, [])
        self.assertFalse(self.state.path.exists())
        spec = resume.spec_for('D02', diagnostic_kind='chat')
        self.assertEqual(spec['accounts'], 2)
        path, _ = self.preflight(2)
        self.assertEqual(len(resume.identities(path, spec)), 2)
        self.state.begin('D02', '2' * 64, spec)
        self.reserve('D02-001'); self.state.mark_send('D02-001')
        self.state.update('D02-001', status='PASS', account_alias='A00')
        result = self.state.finish('D02', 1, 1, {1: 1})
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['accounts'], {'A00': 1})
        self.assertEqual(self.state.load()['batches']['D02']['spec']['accounts'], 2)
        self.assertEqual(result['consumed']['text'], 4)
        self.assertEqual(self.original.read_bytes(), self.original_bytes)

    def test_fsync_failure_prevents_send_and_complete_intent_remains_charged(self):
        self.state.begin('L2', '2' * 64)
        client = NeverSend(); runner = resume.Runner(self.state, client, {}, self.args())
        with patch.object(resume.os, 'fsync', side_effect=OSError('synthetic fsync failure')):
            with self.assertRaises(OSError): runner.execute(resume.plan('L2')[0], [], 0)
        self.assertEqual(client.calls, [])
        result = self.new_state().summary('L2')
        self.assertEqual(result['consumed']['text'], 4)
        self.assertEqual(result['generation_post_intents'], 0)

    def test_unknown_is_charged_once_and_no_new_round_or_case_replay(self):
        self.state.begin('L2', '2' * 64)
        client = NeverSend(); runner = resume.Runner(self.state, client, {}, self.args())
        runner.execute(resume.plan('L2')[0], [], 0)
        runner.execute(resume.plan('L2')[1], [], 0)
        result = self.state.summary('L2')
        self.assertEqual(result['consumed']['text'], 4)
        self.assertEqual(result['states'], {'UNKNOWN': 1})
        self.assertEqual(result['generation_post_intents'], 1)
        self.assertEqual(sum(post for _, post in client.calls), 1)
        with self.assertRaises(resume.BatchError): self.state.mark_send('L2-001')
        with self.assertRaises(resume.BatchError): self.reserve()
        with self.assertRaises(resume.BatchError): self.state.begin('L2', '2' * 64)
        self.assertTrue(runner.stop.is_set())

    def test_identity_ten_hmac_linux_and_media_gates_precede_network(self):
        path, data = self.preflight(9)
        with self.assertRaises(resume.BatchError): resume.identities(path, resume.ROUNDS['C5R'])
        path, data = self.preflight(10)
        self.assertEqual(len(resume.identities(path, resume.ROUNDS['C5R'])), 10)
        data['accounts'][1]['upstream_identity_sha256'] = data['accounts'][0]['upstream_identity_sha256']
        path.write_text(json.dumps(data))
        with self.assertRaises(resume.BatchError): resume.identities(path, resume.ROUNDS['SOAKR'])
        path, data = self.preflight(10)
        data['accounts'][0]['identity_hash_scheme'] = 'Cookie hash'
        path.write_text(json.dumps(data))
        with self.assertRaises(resume.BatchError): resume.identities(path, resume.ROUNDS['SOAKR'])
        self.preflight(5); client = NeverSend()
        with patch.object(resume.sys, 'platform', 'linux'):
            with self.assertRaisesRegex(resume.BatchError, 'decode gate'): resume.run_batch(self.state, client, self.args('C3R'))
        with patch.object(resume.sys, 'platform', 'win32'):
            with self.assertRaisesRegex(resume.BatchError, 'Linux'): resume.run_batch(self.state, client, self.args())
        self.assertEqual(client.calls, [])
        self.assertFalse(self.state.path.exists())
        self.assertFalse(self.state.authority.exists())

    def test_soak_real_dispatch_span_and_coverage_guard_not_idle_elapsed(self):
        for name, count, identities, passing in (('SOAKR', 300, 10, 'PASS'),
                ('SOAK9', 300, 9, 'PARTIAL_PASS'), ('SOAK9TEXT', 288, 9, 'PARTIAL_PASS')):
            with self.subTest(name=name):
                items = resume.plan(name); times = resume.offsets(name, len(items))
                self.assertEqual((len(items), times[0], times[-1]), (count, 0, 7200))
                waves = [times[i:i + 3] for i in range(0, count, 3)]
                self.assertTrue(all(len(set(wave)) == 1 for wave in waves))
                self.assertTrue(all(waves[i][0] < waves[i + 1][0] for i in range(len(waves) - 1)))
                self.assertEqual(waves[-1], [7200] * 3)
                self.assertLess(max(waves[i + 1][0] - waves[i][0] for i in range(len(waves) - 1)), 76)
                data = {'batches': {name: {'spec': resume.ROUNDS[name], 'status': 'RUNNING'}},
                        'cases': {item['case_id']: {**item, 'round': name, 'status': 'PASS',
                            'account_alias': f'A{i % identities}', 'generation_post_count': 1,
                            'dispatch_elapsed_seconds': times[i]} for i, item in enumerate(items)}}
                def finish(event, value): data['batches'][value['round']].update(value['changes'])
                with patch.object(self.state, 'load', return_value=data), patch.object(self.state, '_record', side_effect=finish):
                    self.assertEqual(self.state.finish(name, 7199, 3, {})['status'], 'INCOMPLETE')
                    self.assertEqual(self.state.finish(name, 7200, 3, {0: 90, 3: 10})['status'], passing)
                    self.assertAlmostEqual(self.state.summary(name)['client_generation_http_active_fraction'], .1)
                    for row in data['cases'].values(): row['dispatch_elapsed_seconds'] = 0
                    data['cases'][items[-1]['case_id']]['dispatch_elapsed_seconds'] = 7200
                    result = self.state.finish(name, 7200, 3, {})
                    self.assertEqual(result['status'], 'INCOMPLETE')
                    self.assertEqual(len(result['actual_dispatch_600s_buckets']), 2)

    def original_binding(self):
        self.state.begin('L2', '2' * 64); self.reserve(); self.state.mark_send('L2-001')
        self.state.update('L2-001', status='UNKNOWN', marker='PHASE2_L2_001', kind='chat',
            operation_id='original-op', account_id='account-0', account_alias='A00',
            session_id='original-session', message_id='original-message', vm_id='original-vm', operation_binding_verified=True)
        op = {'id': 'original-op', 'account_id': 'account-0', 'prompt': 'PHASE2_L2_001', 'kind': 'chat',
              'state': 'completed', 'delivery': 'completed', 'session_id': 'original-session',
              'message_id': 'original-message', 'vm_id': 'original-vm', 'result': {'text': 'PHASE2_L2_001'}}
        args = self.args(); args.case = 'L2-001'
        args.operations_file.write_text(json.dumps({'original-op': op}))
        return args, op

    def test_recovery_reads_only_original_operation_without_new_budget_or_send(self):
        args, op = self.original_binding(); client = RecoveryClient()
        before = self.state.summary('L2')['consumed']
        result = resume.diagnose(self.state, client, args)
        self.assertEqual(client.calls, [('/admin/protocol/operations/original-op/recover', True)])
        self.assertEqual(result['consumed'], before)
        row = self.state.load()['cases']['L2-001']
        self.assertEqual(row['generation_post_count'], 1)
        self.assertEqual((row['status'], row['recovery_status']), ('UNKNOWN', 'RECOVERED'))
        self.assertTrue(row['diagnostic_read_verified'])
        self.assertEqual(self.original.read_bytes(), self.original_bytes)

    def test_recovery_foreign_vm_or_response_operation_is_rejected(self):
        args, op = self.original_binding()
        op['vm_id'] = 'foreign-vm'; args.operations_file.write_text(json.dumps({'original-op': op}))
        client = RecoveryClient()
        with self.assertRaises(resume.SafetyError): resume.diagnose(self.state, client, args)
        self.assertEqual(client.calls, [])
        op['vm_id'] = 'original-vm'; args.operations_file.write_text(json.dumps({'original-op': op}))
        with self.assertRaises(resume.SafetyError): resume.diagnose(self.state, RecoveryClient('foreign-op'), args)
        self.assertEqual(self.state.load()['cases']['L2-001']['vm_id'], 'original-vm')

    def test_execution_interrupt_stops_waiting_workers_before_executor_wait(self):
        # __exit__ forces synthetic cleanup even for the broken implementation,
        # so the red proof never waits sixty seconds or sends any real request.
        for mode in ('submission', 'collection'):
            with self.subTest(mode=mode):
                original = self.root / mode / 'phase2-ledger.json'
                original.parent.mkdir(); original.write_bytes(self.original_bytes)
                state = resume.State(self.root / ('resume-' + mode), original, self.sha, resume.BASELINE)
                args = self.args('SOAKR'); self.preflight(10)
                started = threading.Event(); entries = []; runners = []; exit_stops = []
                entry_lock = threading.Lock()
                class WaitingRunner(resume.Runner):
                    def __init__(self, *values):
                        super().__init__(*values); runners.append(self)
                    def execute(self, *values):
                        with entry_lock:
                            entries.append(True)
                            if len(entries) >= 3: started.set()
                        return super().execute(*values)
                class ObservedExecutor(ThreadPoolExecutor):
                    submitted = 0
                    def submit(self, *values, **keywords):
                        self.submitted += 1
                        if mode == 'submission' and self.submitted == 6:
                            raise KeyboardInterrupt()
                        return super().submit(*values, **keywords)
                    def __exit__(self, *values):
                        exit_stops.append(runners[0].stop.is_set())
                        runners[0].stop.set()  # Test-only red-run cleanup.
                        return super().__exit__(*values)
                def interrupted_collection(futures):
                    self.assertTrue(started.wait(2))
                    raise KeyboardInterrupt()
                class Client:
                    calls = []
                    def open(inner, path, body=None, headers=None):
                        inner.calls.append((path, body is not None))
                        if path == '/admin/backend': value = {'backend': 'protocol', 'running': True}
                        elif path == '/admin/accounts': value = [{'id': f'account-{i}'} for i in range(10)]
                        else: raise AssertionError('No generation permitted in interrupt proof')
                        return RecoveryClient.Reply(json.dumps(value).encode())
                client = Client(); pil = ModuleType('PIL'); image = ModuleType('PIL.Image'); pil.Image = image
                with patch.object(resume.sys, 'platform', 'linux'), \
                        patch.object(resume, 'media_gate', return_value=True), \
                        patch.object(resume.shutil, 'which', return_value='/synthetic/decoder'), \
                        patch.dict(sys.modules, {'PIL': pil, 'PIL.Image': image}), \
                        patch.object(resume.legacy, 'fixtures', return_value=[]), \
                        patch.object(resume, 'offsets', side_effect=lambda name, count: [60] * count), \
                        patch.object(resume, 'Runner', WaitingRunner), \
                        patch.object(resume, 'ThreadPoolExecutor', ObservedExecutor), \
                        patch.object(resume, 'as_completed', side_effect=interrupted_collection):
                    with self.assertRaises(KeyboardInterrupt): resume.run_batch(state, client, args)
                self.assertEqual(exit_stops, [True], 'stop must precede Executor.__exit__(wait=True)')
                self.assertEqual(state.load()['cases'], {})
                self.assertFalse(any(post for _, post in client.calls))
                self.assertFalse((state.root / 'phase2-resume-run.lock').exists())
                self.assertEqual(original.read_bytes(), self.original_bytes)

    def test_execution_diagnostics_exclude_other_diagnostics_and_run(self):
        args, operation = self.original_binding()
        entered, release = threading.Event(), threading.Event()
        failures = []
        class BlockingClient(RecoveryClient):
            def open(inner, *values, **keywords):
                entered.set()
                if not release.wait(3): raise AssertionError('Synthetic diagnostic release missing')
                return super().open(*values, **keywords)
        def diagnostic():
            try: resume.diagnose(self.state, BlockingClient(), args)
            except BaseException as error: failures.append(error)
        thread = threading.Thread(target=diagnostic, daemon=True); thread.start()
        self.assertTrue(entered.wait(2))
        try:
            second = RecoveryClient()
            with self.assertRaises(resume.BatchError): resume.diagnose(self.state, second, args)
            self.assertEqual(second.calls, [])
            self.preflight(2)
            run_args = copy.copy(args); run_args.round = 'D01'; run_args.diagnostic_kind = 'chat'
            client = NeverSend(); pil = ModuleType('PIL'); image = ModuleType('PIL.Image'); pil.Image = image
            with patch.object(resume.sys, 'platform', 'linux'), \
                    patch.object(resume.shutil, 'which', return_value='/synthetic/decoder'), \
                    patch.dict(sys.modules, {'PIL': pil, 'PIL.Image': image}):
                with self.assertRaises(resume.BatchError): resume.run_batch(self.state, client, run_args)
            self.assertEqual(client.calls, [])
        finally:
            release.set(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertFalse((self.state.root / 'phase2-resume-run.lock').exists())
        self.assertEqual(self.original.read_bytes(), self.original_bytes)

    def test_truncated_journal_and_changed_original_fail_closed(self):
        self.state.begin('L2', '2' * 64); self.reserve()
        with self.state.path.open('ab') as stream: stream.write(b'{"seq":')
        with self.assertRaises(resume.BatchError): self.state.load()
        with self.assertRaises(resume.BatchError): self.new_state().load()
        self.original.write_bytes(self.original_bytes + b' ')
        with self.assertRaises(resume.BatchError): self.state.summary()


if __name__ == '__main__': unittest.main()
