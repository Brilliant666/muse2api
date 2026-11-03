#!/usr/bin/env python3
"""Resume phase-2 with an immutable 3/1/0 baseline and fixed 600-generation cap.

Only init/run create a one-time authority beside --original-ledger, binding its
SHA to ONE external resume root. The old ledger is never written. Resume events
are hash-chained, append-only and fsynced before generation POST. A truncated
journal or stale execution lock fails closed; never delete either to retry.

Reuse phase2_batch's transport, exact operation/task/media binding, full decode,
and first serious/unclassified error stop. C3TEXT is explicitly partial and
shares C3R's slot. D01..D36 each require --diagnostic-kind and send one case only.
SOAKR/SOAK9 schedule 100 three-request waves; SOAK9TEXT 96 waves over 7200 seconds. Report actual send time buckets,
not an idle wait or client concurrency as proof of upstream parallelism.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
import urllib.parse

import phase2_batch as legacy

BatchError, SafetyError = legacy.BatchError, legacy.SafetyError
common, ROOT = legacy.common, Path(__file__).resolve().parents[1]
LIMITS, BASELINE = {'text': 560, 'image': 35, 'video': 5}, {'text': 3, 'image': 1, 'video': 0}
ROUNDS = {
    'L2': {'text': 15, 'image': 4, 'video': 1, 'concurrency': 1, 'accounts': 2},
    'C3R': {'text': 111, 'image': 8, 'video': 1, 'concurrency': 3, 'accounts': 5},
    'C5R': {'text': 111, 'image': 8, 'video': 1, 'concurrency': 5, 'accounts': 10},
    'SOAKR': {'text': 288, 'image': 11, 'video': 1, 'concurrency': 3, 'accounts': 10},
    'C3TEXT': {'text': 100, 'image': 0, 'video': 0, 'concurrency': 3, 'accounts': 5},
    'SOAK9': {'text': 288, 'image': 11, 'video': 1, 'concurrency': 3, 'accounts': 9},
    'SOAK9TEXT': {'text': 288, 'image': 0, 'video': 0, 'concurrency': 3, 'accounts': 9},
}
KINDS = {'chat': 'text', 'chat_sse': 'text', 'responses': 'text', 'responses_sse': 'text',
         'image': 'image', 'edit': 'image', 'video': 'video'}
CASE = re.compile(r'^(L2|C3R|C5R|SOAKR|SOAK9TEXT|SOAK9|C3TEXT|D(?:0[1-9]|[12][0-9]|3[0-6]))-[0-9]{3}$')
ROUND_NAMES = tuple(ROUNDS) + tuple(f'D{i:02d}' for i in range(1, 37))
ZERO = '0' * 64
PARTIAL = {'C3TEXT', 'SOAK9', 'SOAK9TEXT'}
SOAK = {'SOAKR', 'SOAK9', 'SOAK9TEXT'}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def external(path):
    resolved = Path(path).expanduser().resolve()
    if resolved.is_relative_to(ROOT) or any((p / '.git').exists() for p in (resolved, *resolved.parents)):
        raise BatchError('Private evidence must be outside every Git worktree')
    return resolved


def sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)


def spec_for(name, *, diagnostic_kind=None, text_count=100):
    if name not in ROUND_NAMES:
        raise BatchError('Unknown bounded resume round')
    if name.startswith('D'):
        if diagnostic_kind not in KINDS:
            raise BatchError('Each diagnostic requires one explicit kind')
        return {**{k: int(KINDS[diagnostic_kind] == k) for k in LIMITS},
                'concurrency': 1, 'accounts': 2, 'diagnostic_kind': diagnostic_kind}
    spec = copy.deepcopy(ROUNDS[name])
    if name == 'C3TEXT':
        if type(text_count) is not int or not 1 <= text_count <= 100:
            raise BatchError('C3TEXT must contain 1..100 text requests')
        spec['text'] = text_count
    elif diagnostic_kind is not None or text_count != 100:
        raise BatchError('Per-round counts are fixed')
    return spec


def slot(name):
    return 'C3R' if name == 'C3TEXT' else 'SOAKR' if name in {'SOAK9', 'SOAK9TEXT'} else name


def plan(name, spec=None):
    spec = spec or spec_for(name)
    if name.startswith('D'):
        items = [{'kind': spec['diagnostic_kind'], 'category': KINDS[spec['diagnostic_kind']]}]
    else:
        items = [{'kind': ('chat', 'chat_sse', 'responses', 'responses_sse')[i % 4], 'category': 'text'}
                 for i in range(spec['text'])]
        media = [{'kind': 'image' if name == 'L2' and i % 2 == 0 else 'edit', 'category': 'image'}
                 for i in range(spec['image'])] + [{'kind': 'video', 'category': 'video'}] * spec['video']
        for i, item in enumerate(media):
            items.insert(min(len(items), (i + 1) * sum(spec[k] for k in LIMITS) // (len(media) + 1)), item)
    return [{**item, 'case_id': f'{name}-{i:03d}'} for i, item in enumerate(items, 1)]


def offsets(name, count):
    if name in SOAK:
        if count < 3 or count % 3:
            raise BatchError('Long-run plan must use complete concurrency-three waves')
        return [7200 * (i // 3) / max(1, count // 3 - 1) for i in range(count)]
    return [0 for _ in range(count)]


class State:
    def __init__(self, root, original_ledger, original_sha256, baseline_consumed):
        self.root, self.original = external(root), external(original_ledger)
        if self.root == self.original.parent or self.root == self.original:
            raise BatchError('Use a separate resume evidence root')
        if baseline_consumed != BASELINE or not re.fullmatch(r'[a-f0-9]{64}', original_sha256):
            raise BatchError('Explicit baseline must remain 3/1/0 and SHA-256 must be exact')
        if self.original.name != 'phase2-ledger.json':
            raise BatchError('Explicit original phase-2 ledger required; phase-1 refused')
        self.path = self.root / 'phase2-resume-ledger.jsonl'
        self.authority = self.original.with_name(self.original.name + '.resume-authority.json')
        self.reference = {'original_sha256': original_sha256, 'resume_ledger': str(self.path)}
        self.genesis = {'schema_version': 1, 'original_ledger': str(self.original),
                        **self.reference, 'baseline_consumed': BASELINE, 'limits': LIMITS,
                        'rounds': ROUNDS, 'c3_slot_alias': {'C3TEXT': 'C3R'},
                        'soak_slot_aliases': {'SOAK9': 'SOAKR', 'SOAK9TEXT': 'SOAKR'}, 'diagnostic_cap': 36}
        self.mutex = threading.RLock()
        self._data, self._offset, self._sequence, self._tail = None, 0, 0, ZERO
        self._verify_original()

    def _verify_original(self):
        raw = self.original.read_bytes()
        if digest(raw) != self.reference['original_sha256']:
            raise BatchError('Original ledger changed; refusing baseline or generation')
        data = json.loads(raw)
        if (data.get('schema_version') != 2 or data.get('limits') != legacy.LIMITS
                or data.get('rounds') != legacy.ROUNDS or data.get('budget_slot_aliases') != legacy.BUDGET_ALIASES):
            raise BatchError('Original phase-2 policy differs; no migration/reset permitted')
        rows = list(data.get('cases', {}).values())
        if (dict(Counter(row.get('category') for row in rows)) != {k: v for k, v in BASELINE.items() if v}
                or any(row.get('units') != 1 or row.get('automatic_replay') is not False for row in rows)):
            raise BatchError('Original charged intents do not equal baseline 3/1/0')
        if self.authority.exists() and json.loads(self.authority.read_text('utf-8')) != self.reference:
            raise BatchError('Original ledger already binds another resume root; never fork the budget')

    @contextmanager
    def transaction(self):
        with self.mutex:
            lock = self.root / 'phase2-resume-ledger.lock'
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                raise BatchError('Ledger locked; inspect without deleting or replaying') from None
            try:
                os.close(fd)
                yield
            finally:
                lock.unlink()

    def initialize(self):
        self._verify_original()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (self.root / 'ledger.json').exists() or (self.root / 'live-cases/ledger.json').exists() or (self.root / 'phase2-ledger.json').exists():
            raise BatchError('Existing acceptance ledger root refused')
        with self.transaction():
            self._verify_original()
            if not self.authority.exists():
                if self.path.exists():
                    raise BatchError('Unanchored journal exists; refusing adoption/reset')
                self._exclusive_json(self.authority, self.reference)
                self._append('GENESIS', self.genesis)
            elif not self.path.exists():
                raise BatchError('Authorized resume journal is missing; never recreate its budget')
            self.load()

    @staticmethod
    def _exclusive_json(path, value):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical(value) + b'\n'); stream.flush(); os.fsync(stream.fileno())
        sync_directory(path.parent)

    def _append(self, event, payload):
        envelope = {'seq': self._sequence + 1, 'previous_sha256': self._tail,
                    'event': event, 'at': common.now(), 'payload': payload}
        envelope['sha256'] = digest(canonical(envelope))
        created = not self.path.exists()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'ab') as stream:
            stream.write(canonical(envelope) + b'\n'); stream.flush(); os.fsync(stream.fileno())
        if created: sync_directory(self.path.parent)
        # Replay only after fsync. An ambiguous failed write remains charged on restart.
        self.load()

    def load(self):
        with self.mutex:
            self._verify_original()
            if not self.path.exists():
                if self.authority.exists():
                    raise BatchError('Authorized resume journal is missing; no reset')
                return {'batches': {}, 'cases': {}}
            if not self.authority.exists():
                raise BatchError('Resume journal has no original authority; no new sends')
            if self._data is None:
                self._data = {'batches': {}, 'cases': {}}
            if self.path.stat().st_size < self._offset:
                raise BatchError('Append-only journal was truncated')
            with self.path.open('rb') as stream:
                stream.seek(self._offset)
                for raw in stream:
                    if not raw.endswith(b'\n'):
                        raise BatchError('Incomplete journal write; inspect without replay')
                    envelope = json.loads(raw); expected = envelope.pop('sha256', None)
                    if (envelope.get('seq') != self._sequence + 1 or envelope.get('previous_sha256') != self._tail
                            or digest(canonical(envelope)) != expected):
                        raise BatchError('Journal hash chain invalid')
                    if self._sequence == 0 and envelope['event'] != 'GENESIS':
                        raise BatchError('Journal must start with original-reference genesis')
                    self._apply(envelope['event'], envelope['payload'])
                    self._sequence, self._tail = envelope['seq'], expected
                    self._offset += len(raw)
            if self._sequence == 0:
                raise BatchError('Empty journal refused; no reset')
            return copy.deepcopy(self._data)

    def _apply(self, event, value):
        data = self._data
        if event == 'GENESIS':
            if self._sequence != 0 or value != self.genesis:
                raise BatchError('Genesis policy/reference differs')
        elif event == 'BEGIN':
            name = value['round']
            if any(slot(other) == slot(name) for other in data['batches']):
                raise BatchError('Round or shared C3 slot already used')
            spec = value['spec']
            if spec != spec_for(name, diagnostic_kind=spec.get('diagnostic_kind'), text_count=spec['text'] if name == 'C3TEXT' else 100):
                raise BatchError('Fixed round specification differs')
            data['batches'][name] = value
        elif event == 'RESERVE':
            case, category, name = value['case_id'], value['category'], value['round']
            if not CASE.fullmatch(case) or case.rsplit('-', 1)[0] != name or case in data['cases'] or category not in LIMITS:
                raise BatchError('Invalid/duplicate charged intent')
            spec = data['batches'][name]['spec']; rows = list(data['cases'].values())
            if (data['batches'][name]['status'] != 'RUNNING'
                    or BASELINE[category] + sum(v['category'] == category for v in rows) >= LIMITS[category]
                    or sum(v['round'] == name and v['category'] == category for v in rows) >= spec[category]
                    or (name.startswith('D') and sum(v['round'].startswith('D') for v in rows) >= 36)):
                raise BatchError('Fixed global/round/diagnostic budget exhausted')
            data['cases'][case] = value
        elif event in {'UPDATE', 'SEND'}:
            row = data['cases'][value['case_id']]
            changes = value['changes']
            immutable = {'case_id', 'round', 'category', 'units', 'intent_at', 'endpoint', 'request_sha256', 'automatic_replay'}
            bound = {'operation_id', 'account_id', 'task_id', 'task_account_id', 'session_id', 'message_id', 'vm_id', 'marker', 'kind'}
            if any(k in immutable or (k in bound and row.get(k) is not None and row[k] != v) for k, v in changes.items()):
                raise SafetyError('IDENTITY_CONFLICT', 'Original intent/binding cannot change')
            if event == 'SEND' and (row['status'] != 'INTENT' or row.get('send_started_at')):
                raise BatchError('Case send already started; recovery only')
            if event == 'UPDATE' and any(k in changes for k in ('send_started_at', 'generation_post_count', 'dispatch_elapsed_seconds')):
                raise BatchError('Send receipt may only be appended once')
            row.update(changes)
        elif event == 'FINISH':
            data['batches'][value['round']].update(value['changes'])
        else:
            raise BatchError('Unknown journal event')

    def _record(self, event, value):
        with self.transaction():
            self.load()
            # Validate a proposed event before appending, then restore the replay cache.
            previous = copy.deepcopy(self._data)
            try:
                self._apply(event, value)
            finally:
                self._data = previous
            self._append(event, value)

    def begin(self, name, preflight_digest, spec=None, started_monotonic=None):
        self.initialize()
        self._record('BEGIN', {'round': name, 'status': 'RUNNING', 'started_at': common.now(),
            'preflight_sha256': preflight_digest, 'spec': spec or spec_for(name),
            'budget_slot': slot(name), 'partial_scope': name in PARTIAL,
            'started_monotonic': time.monotonic() if started_monotonic is None else started_monotonic})

    def reserve(self, case, category, endpoint, request_hash):
        self._record('RESERVE', {'case_id': case, 'round': case.rsplit('-', 1)[0], 'category': category,
            'status': 'INTENT', 'intent_at': common.now(), 'endpoint': endpoint, 'request_sha256': request_hash,
            'units': 1, 'automatic_replay': False, 'generation_post_count': 0})

    def update(self, case, **changes):
        self._record('UPDATE', {'case_id': case, 'changes': {**changes, 'updated_at': common.now()}})

    def mark_send(self, case):
        name = case.rsplit('-', 1)[0]
        self._record('SEND', {'case_id': case, 'changes': {'status': 'SENDING', 'send_started_at': common.now(),
            'generation_post_count': 1, 'dispatch_elapsed_seconds': round(time.monotonic() - self.load()['batches'][name]['started_monotonic'], 6)}})

    def folder(self, case):
        if not CASE.fullmatch(case) or case not in self.load()['cases']:
            raise BatchError('Recorded original resume case required')
        folder = self.root / 'cases' / case; folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        return folder

    def finish(self, name, elapsed, peak, distribution):
        data = self.load(); spec = data['batches'][name]['spec']
        rows = [v for v in data['cases'].values() if v['round'] == name]
        observed_accounts_required = 1 if name.startswith('D') else spec['accounts']
        complete = (len(rows) == sum(spec[k] for k in LIMITS) and all(v['status'] == 'PASS' for v in rows)
                    and len({v.get('account_alias') for v in rows} - {None}) >= observed_accounts_required)
        buckets = Counter(min(11, int(v['dispatch_elapsed_seconds'] // 600)) for v in rows if 'dispatch_elapsed_seconds' in v)
        span = max((v.get('dispatch_elapsed_seconds', 0) for v in rows), default=0) - min((v.get('dispatch_elapsed_seconds', 0) for v in rows), default=0)
        if name in SOAK:
            last = max((v.get('dispatch_elapsed_seconds', 0) for v in rows), default=0)
            complete = complete and elapsed >= 7200 and last >= 7200 and span >= 7190 and all(buckets[i] for i in range(12))
        occupied = sum(distribution.values())
        active = sum(seconds for level, seconds in distribution.items() if int(level) > 0)
        self._record('FINISH', {'round': name, 'changes': {'status': ('PARTIAL_PASS' if name in PARTIAL else 'PASS') if complete else 'INCOMPLETE',
            'finished_at': common.now(), 'elapsed_seconds': round(elapsed, 3), 'client_peak_inflight': peak,
            'client_concurrency_seconds': distribution, 'client_generation_http_active_fraction': active / occupied if occupied else None,
            'load_shape': 'paced_concurrency_three_waves' if name in SOAK else 'bounded_batch',
            'planned_wave_count': len(rows) // 3 if name in SOAK else None, 'full_saturation_claimed': False,
            'client_concurrency_scope': 'HTTP generation POST occupancy only; client peak covers full case lifetime; server metrics establish actual protocol overlap',
            'actual_dispatch_span_seconds': round(span, 3),
            'actual_dispatch_600s_buckets': dict(buckets), 'server_or_upstream_parallelism_not_inferred_from_client_count': True}})
        return self.summary(name)

    def summary(self, name=None):
        data = self.load(); rows = [v for v in data['cases'].values() if not name or v['round'] == name]
        consumed = Counter(v['category'] for v in data['cases'].values())
        values = sorted(v['latency_seconds'] for v in rows if 'latency_seconds' in v)
        percentile = lambda p: values[min(len(values) - 1, int((len(values) - 1) * p))] if values else None
        batch = data['batches'].get(name, {})
        return {'schema_version': 1, 'round': name, 'status': batch.get('status', 'NOT_RUN' if self.path.exists() else 'NOT_INITIALIZED'),
            'original_sha256': self.reference['original_sha256'], 'baseline_consumed': BASELINE,
            'consumed': {k: BASELINE[k] + consumed[k] for k in LIMITS},
            'remaining': {k: LIMITS[k] - BASELINE[k] - consumed[k] for k in LIMITS},
            'new_case_count': len(rows), 'diagnostic_cases_consumed': sum(v['round'].startswith('D') for v in data['cases'].values()),
            'diagnostic_cases_remaining': 36 - sum(v['round'].startswith('D') for v in data['cases'].values()), 'states': dict(Counter(v['status'] for v in rows)),
            'accounts': dict(Counter(v.get('account_alias', 'UNBOUND') for v in rows)),
            'per_account_states': {a: dict(Counter(v['status'] for v in rows if v.get('account_alias', 'UNBOUND') == a)) for a in {v.get('account_alias', 'UNBOUND') for v in rows}},
            'p50_latency_seconds': percentile(.5), 'p95_latency_seconds': percentile(.95),
            'queue_wait_scope': 'Client dispatch delay after scheduled time; server lease wait is in private server metrics',
            'generation_post_intents': sum(v['generation_post_count'] for v in rows),
            'post_count_scope': 'Charged local POST attempts, not provider-accepted generations',
            'partial_scope': name in PARTIAL, 'mixed_target_status': 'NOT_RUN' if name in PARTIAL else None, 'original_target_round': slot(name) if name in PARTIAL else None,
            'cases': [{k: v[k] for k in ('case_id', 'category', 'kind', 'status', 'account_alias', 'failure_class', 'queue_wait_seconds', 'latency_seconds', 'dispatch_elapsed_seconds', 'characters', 'bytes', 'sha256') if k in v} for v in rows],
            **{k: v for k, v in batch.items() if k in {'elapsed_seconds', 'client_peak_inflight', 'client_concurrency_seconds', 'client_generation_http_active_fraction', 'load_shape', 'planned_wave_count', 'full_saturation_claimed', 'client_concurrency_scope', 'actual_dispatch_span_seconds', 'actual_dispatch_600s_buckets', 'server_or_upstream_parallelism_not_inferred_from_client_count'}}}


def identities(path, spec):
    data = json.loads(external(path).read_text('utf-8-sig'))
    if data.get('upstream_identity_verified') is not True or data.get('platform') != 'linux' or data.get('backend') != 'protocol':
        raise BatchError('Real Linux protocol authenticated identity preflight required')
    accounts = [a for a in data.get('accounts', []) if a.get('enabled', True)]
    if not spec['accounts'] <= len(accounts) <= 10:
        raise BatchError('Independent identity threshold not met')
    for field in ('alias', 'account_id', 'upstream_identity_sha256'):
        values = [a.get(field) for a in accounts]
        if any(not isinstance(v, str) or not v for v in values) or len(set(values)) != len(values):
            raise BatchError('Independent upstream identities required; no duplicate Cookie accounts')
    if any(not re.fullmatch(r'[a-f0-9]{64}', a['upstream_identity_sha256']) or a.get('identity_hash_scheme') != 'round-private HMAC-SHA256' for a in accounts):
        raise BatchError('Verified round-private upstream HMAC identity required')
    return {a['account_id']: a['alias'] for a in accounts}


def media_gate(state):
    return {'image', 'edit', 'video'} <= {v.get('kind') for v in state.load()['cases'].values()
        if v.get('status') == 'PASS' and v.get('operation_binding_verified') is True
        and v.get('media_identity_verified') is True and v.get('media_decode', {}).get('status') == 'PASS'}


class Runner(legacy.Runner):
    def __init__(self, *args):
        super().__init__(*args)
        self.metrics_lock = threading.Lock(); self.active = 0; self.previous = time.monotonic(); self.distribution = Counter()

    def bind(self, case, marker, text=None, *, require_completed=False):
        operation = super().bind(case, marker, text, require_completed=require_completed)
        # Grade the explicit marker-only acceptance prompt after retaining the
        # verified original operation/account. Read-only binds have no text arg.
        if text is not None and operation.get('kind') == 'chat' and text.strip() != marker:
            raise SafetyError('UPSTREAM_OR_VALIDATION', 'Returned text does not equal this test expected marker')
        return operation

    def _occupancy(self, delta):
        with self.metrics_lock:
            current = time.monotonic(); self.distribution[self.active] += current - self.previous
            self.previous = current; self.active += delta

    def call(self, *args, **kwargs):
        body = args[2] if len(args) > 2 else kwargs.get('body')
        generation = body is not None and kwargs.get('label', 'response') == 'response'
        if generation: self._occupancy(1)
        try: return super().call(*args, **kwargs)
        finally:
            if generation: self._occupancy(-1)


@contextmanager
def execution_lock(state):
    path = state.root / 'phase2-resume-run.lock'
    try: fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError: raise BatchError('Active/stale execution lock; inspect, never replay') from None
    os.close(fd)
    try:
        yield
    finally:
        path.unlink()


def run_batch(state, client, args):
    spec = spec_for(args.round, diagnostic_kind=args.diagnostic_kind, text_count=args.text_count)
    if not 1 <= args.task_timeout <= 1200: raise BatchError('Task polling timeout must be 1..1200 seconds')
    data = state.load()
    if any(slot(other) == slot(args.round) for other in data['batches']):
        raise BatchError('Resume round/shared C3 slot already used; original recovery only')
    if not sys.platform.startswith('linux'):
        raise BatchError('Real batches require Linux; no Windows or synthetic fallback')
    approved = identities(args.preflight, spec)
    operations = external(args.operations_file) if args.operations_file else None
    if operations is None or not operations.is_file():
        raise BatchError('Read-only exact original operation marker file is required')
    if args.round in {'C3R', 'C5R', 'SOAKR', 'SOAK9'} and not media_gate(state):
        raise BatchError('Image/edit/video full decode gate missing; C3TEXT remains explicitly partial')
    if not shutil.which(args.ffmpeg or 'ffmpeg') or not shutil.which(args.ffprobe or 'ffprobe'):
        raise BatchError('Full ffmpeg/ffprobe decoders required before any send')
    import PIL.Image
    state.initialize()
    with execution_lock(state):
        with client.open('/admin/backend') as response: backend = json.loads(response.read())
        if backend.get('backend') != 'protocol' or not backend.get('running'):
            raise BatchError('Protocol backend is not running')
        with client.open('/admin/accounts') as response: rows = json.loads(response.read())
        rows = rows if isinstance(rows, list) else rows['accounts']
        if {a['id'] for a in rows if a.get('enabled', True)} != set(approved):
            raise BatchError('Enabled Linux pool differs from identity preflight')
        start = time.monotonic()
        state.begin(args.round, digest(Path(args.preflight).read_bytes()), spec, start)
        items = plan(args.round, spec); references = legacy.fixtures(state.root)
        runner = Runner(state, client, approved, args)
        with ThreadPoolExecutor(max_workers=spec['concurrency']) as pool:
            jobs = []
            try:
                for item, delay in zip(items, offsets(args.round, len(items))):
                    jobs.append(pool.submit(runner.execute, item, references, start + delay))
                for job in as_completed(jobs):
                    try: job.result()
                    except Exception: runner.stop.set()
            except BaseException:
                runner.stop.set()  # Wake paced workers before Executor.__exit__ waits.
                for job in jobs: job.cancel()
                raise
        runner._occupancy(0)
        summary = state.finish(args.round, time.monotonic() - start, runner.peak, dict(runner.distribution))
        common.atomic_json(state.root / (args.round + '-summary.json'), summary)
        return summary


def diagnose(state, client, args):
    with execution_lock(state):
        return _diagnose(state, client, args)


def _diagnose(state, client, args):
    record = state.load()['cases'].get(args.case)
    if not record or not args.case.startswith(args.round + '-'):
        raise BatchError('Recorded original case required')
    if not args.operations_file or not external(args.operations_file).is_file():
        raise BatchError('Original operation marker file required for diagnostic binding')
    oid, aid = record.get('operation_id'), record.get('account_id')
    if not oid or not aid or record.get('operation_binding_verified') is not True or not record.get('marker'):
        raise SafetyError('IDENTITY_CONFLICT', 'Verified original operation/account/prompt required')
    runner = Runner(state, client, {aid: record['account_alias']}, args)
    runner.bind(args.case, record['marker'])
    if args.command == 'recover':
        _, raw = runner.call(args.case, '/admin/protocol/operations/' + urllib.parse.quote(oid, safe='') + '/recover',
                             json.dumps({'timeout': 60}).encode(), label='recovery')
        value = json.loads(raw)
        if value.get('resubmitted') is not False or value.get('operation_id') != oid:
            raise SafetyError('IDENTITY_CONFLICT', 'Recovery must attest original operation and resubmitted:false')
        state.update(args.case, recovery_status='RECOVERED' if value.get('ok') else 'UNKNOWN')
        if not value.get('ok'): return state.summary(args.round)
    if record['category'] == 'text':
        operation = runner.bind(args.case, record['marker'], require_completed=True)
        if args.command == 'recover' and value.get('result', {}).get('text') != operation.get('result', {}).get('text'):
            raise SafetyError('TEXT_MISMATCH', 'Recovery text differs from original operation')
    else:
        task = runner.poll(args.case)
        runner.bind(args.case, record['marker'], require_completed=True)
        runner.media(args.case, task, record['category'])
    state.update(args.case, diagnostic_read_verified=True)
    return state.summary(args.round)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('init', 'status', 'run', 'query', 'recover'))
    parser.add_argument('--private-root', type=Path, required=True)
    parser.add_argument('--original-ledger', type=Path, required=True)
    parser.add_argument('--original-sha256', required=True)
    parser.add_argument('--baseline-consumed', required=True, choices=('3,1,0',))
    parser.add_argument('--round', choices=ROUND_NAMES)
    parser.add_argument('--diagnostic-kind', choices=tuple(KINDS)); parser.add_argument('--text-count', type=int, default=100)
    parser.add_argument('--base'); parser.add_argument('--preflight', type=Path); parser.add_argument('--operations-file', type=Path)
    parser.add_argument('--case'); parser.add_argument('--key-env', default='MUSE2API_KEY')
    parser.add_argument('--request-timeout', type=int, default=900); parser.add_argument('--task-timeout', type=int, default=900)
    parser.add_argument('--ffprobe'); parser.add_argument('--ffmpeg')
    args = parser.parse_args(argv)
    try:
        state = State(args.private_root, args.original_ledger, args.original_sha256, BASELINE)
        if args.command == 'init': state.initialize(); result = state.summary(args.round)
        elif args.command == 'status': result = state.summary(args.round)
        else:
            if args.round is None or not args.base: raise BatchError('Explicit round and loopback base required')
            client = common.Client(args.base, os.environ.get(args.key_env, ''), min(1200, max(1, args.request_timeout)))
            if args.command == 'run':
                args.preflight = args.preflight or state.root / 'preflight.json'
                result = run_batch(state, client, args)
            else:
                result = diagnose(state, client, args)
        print(json.dumps(result, ensure_ascii=False)); return 0 if result['status'] in {'PASS', 'PARTIAL_PASS', 'NOT_RUN', 'NOT_INITIALIZED'} else 1
    except Exception as exc:
        print(json.dumps({'status': 'BLOCKED', 'error_class': type(exc).__name__, 'note': 'Inspect private original/journal; no reset or automatic replay performed'})); return 2


if __name__ == '__main__':
    raise SystemExit(main())
