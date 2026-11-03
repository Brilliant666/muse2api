#!/usr/bin/env python3
"""One Linux D01-001 SSE disconnect; no retries, restart, recovery or service stop.

Shares phase2_resume's single authority, append-only 600-cap journal and
execution lock. Only this HTTP response is closed. All identifiers/deltas/disk
snapshots remain private; stdout is the anonymized result. A successful
interruption remains UNKNOWN until the operator restarts and recovers its
original operation with phase2_resume.py recover; never run D01 again.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error

import live_protocol_disconnect as old
import phase2_resume as resume

CASE, ROUND, MARKER = 'D01-001', 'D01', 'PHASE2_D01_001'
PROMPT = ('Acceptance marker PHASE2_D01_001. Write exactly 300 short numbered sentences, '
          'numbered 001 through 300, one per line. Each sentence has five to eight words '
          'about simple shapes and colors. Begin now and complete all 300 lines. Do not use tools.')
ENDPOINT = '/v1/chat/completions'


def snapshot(home, before, approved, expected=None):
    operations = old.disk_json(home / 'data/protocol_operations.json', None)
    if operations is None: return None, None
    if not isinstance(operations, dict):
        raise resume.SafetyError('DATA_CORRUPTION', 'Unexpected operation snapshot')
    candidates = [op for ident, op in operations.items() if ident not in before
                  and isinstance(op, dict) and MARKER in str(op.get('prompt', ''))]
    if len(candidates) > 1:
        raise resume.SafetyError('BINDING_AMBIGUOUS', 'Multiple original marker operations; never guess')
    if not candidates: return None, None
    operation = candidates[0]; account = operation.get('account_id')
    if account not in approved:
        raise resume.SafetyError('CROSS_ACCOUNT', 'Original operation is outside the approved identity pool')
    if operation.get('prompt') != PROMPT or operation.get('kind') != 'chat' or not operation.get('id'):
        raise resume.SafetyError('IDENTITY_CONFLICT', 'Original prompt/category binding differs')
    selected, session = old.operation_snapshot(home, before, account)
    if not selected or selected.get('id') != operation['id']:
        raise resume.SafetyError('IDENTITY_CONFLICT', 'Operation account snapshot differs')
    if operation.get('session_key') != 'operation:' + operation['id']:
        raise resume.SafetyError('IDENTITY_CONFLICT', 'Operation session key differs')
    if expected:
        for field in ('id', 'account_id', 'session_id', 'vm_id'):
            if expected.get(field) is not None and operation.get(field) != expected[field]:
                raise resume.SafetyError('IDENTITY_CONFLICT', 'Original operation/session/VM changed')
    if session and operation.get('session_id') and session.get('session_id') != operation['session_id']:
        raise resume.SafetyError('IDENTITY_CONFLICT', 'Original session snapshot differs')
    return operation, session


def metric_evidence(path, operation, session, deadline):
    while True:
        with path.open('rb') as stream: raw = stream.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise resume.SafetyError('DATA_CORRUPTION', 'Private native metrics exceed bounded capture')
        rows = [json.loads(line) for line in raw.splitlines(keepends=True) if line.endswith(b'\n')]
        sends = [row for row in rows if row.get('event') == 'gateway_write_started'
                 and row.get('operation_id') == operation['id'] and row.get('generation_post') is True]
        if len(sends) > 1:
            raise resume.SafetyError('DATA_CORRUPTION', 'More than one observed generation submission for original operation')
        if sends:
            sent = sends[0]
            if (sent.get('account_id') != operation['account_id'] or sent.get('vm_id') != operation.get('vm_id')
                    or sent.get('method') != 'POST' or sent.get('route') != 'chat.stream'
                    or sent.get('operation_session_id') != operation.get('session_id')
                    or sent.get('session_id') != session.get('session_id') or sent.get('node_id') != session.get('node_id')):
                raise resume.SafetyError('IDENTITY_CONFLICT', 'Native generation account/session/node/VM differs')
            handshakes = [row for row in rows if row.get('event') == 'noise_handshake_finished'
                and row.get('connection_id') == sent.get('connection_id')
                and type(row.get('monotonic_ns')) is int and type(sent.get('monotonic_ns')) is int
                and row['monotonic_ns'] <= sent['monotonic_ns']]
            if handshakes:
                handshake = max(handshakes, key=lambda row: row['monotonic_ns'])
                if handshake.get('vm_id') != operation.get('vm_id') or not sent.get('connection_id'):
                    raise resume.SafetyError('IDENTITY_CONFLICT', 'Native handshake belongs to a different VM')
                return {'generation_write_started': sent, 'noise_handshake_finished': handshake,
                        'scope': 'One observed local generation write entry; not provider-accepted count'}
        if time.monotonic() >= deadline: return None
        time.sleep(min(.1, max(0, deadline - time.monotonic())))


def bind(state, operation, approved):
    if operation:
        state.update(CASE, operation_id=operation['id'], account_id=operation['account_id'],
            account_alias=approved[operation['account_id']], session_key=operation.get('session_key'),
            session_id=operation.get('session_id'), message_id=operation.get('message_id'),
            vm_id=operation.get('vm_id'), operation_state=operation.get('state'),
            operation_delivery=operation.get('delivery'), operation_binding_verified=True)


def execute(state, client, home, preflight, metrics_file, cleanup_timeout=12):
    if not sys.platform.startswith('linux'):
        raise resume.BatchError('Real disconnect observation requires Linux')
    if not 0 < cleanup_timeout <= 60 or not 0 < client.timeout <= 600:
        raise resume.BatchError('Bounded response and cleanup deadlines required')
    home = resume.external(home); preflight = resume.external(preflight); metrics_file = resume.external(metrics_file)
    if not metrics_file.is_file(): raise resume.BatchError('Current private native metrics file is required')
    approved = resume.identities(preflight, {**resume.spec_for(ROUND, diagnostic_kind='chat_sse'), 'accounts': 9})
    if len(approved) != 9: raise resume.BatchError('D01 requires the approved independent nine-account pool')
    if ROUND in state.load()['batches']:
        raise resume.BatchError('D01 already claimed; original recovery only')
    state.initialize()
    with resume.execution_lock(state):
        if ROUND in state.load()['batches']: raise resume.BatchError('D01 already claimed')
        backend = old.get_json(client, '/admin/backend')
        if backend.get('backend') != 'protocol' or not backend.get('running') or backend.get('active_operations') != 0:
            raise resume.BatchError('Healthy idle protocol backend required')
        value = old.get_json(client, '/admin/accounts'); accounts = value if isinstance(value, list) else value.get('accounts', [])
        enabled = [a for a in accounts if a.get('enabled', True)]
        if {a.get('id') for a in enabled} != set(approved) or any(a.get('protocol_blocked') for a in enabled):
            raise resume.BatchError('Exact unblocked approved identity pool required')
        existing = old.disk_json(home / 'data/protocol_operations.json', None)
        if not isinstance(existing, dict) or any(MARKER in str(v.get('prompt', '')) for v in existing.values()):
            raise resume.BatchError('Valid original operation snapshot without a reused D01 marker required')
        before = set(existing)
        body = json.dumps(resume.common.payload('T2', prompt=PROMPT, timeout=client.timeout)).encode()
        state.begin(ROUND, resume.digest(preflight.read_bytes()), resume.spec_for(ROUND, diagnostic_kind='chat_sse'))
        state.reserve(CASE, 'text', ENDPOINT, resume.digest(body))
        folder = state.folder(CASE); (folder / 'request.bin').write_bytes(body)
        state.update(CASE, marker=MARKER, kind='chat_sse')
        state.mark_send(CASE)  # Durable precharge/send claim precede the only generation POST.
        receipt = {'case_id': CASE, 'started_at': resume.common.now(), 'generation_post_attempts': 1,
                   'automatic_replay': False, 'controlled_disconnect': False, 'recovery_required': True,
                   'lease_directly_observed': False, 'identity_preflight_accounts': 9}
        response = None; operation = session = None; native = None; metrics_deadline = None
        characters = size = 0; natural_terminal = False; error_class = failure = None
        try:
            response = client.open(ENDPOINT, body, {'Content-Type': 'application/json', 'Accept': 'text/event-stream', 'Idempotency-Key': 'phase2-' + CASE})
            receipt['http_status'] = response.status
            if response.status != 200: raise resume.BatchError('SSE request did not return HTTP 200')
            with (folder / 'disconnect-stream.bin').open('wb') as capture:
                frame = []; deadline = time.monotonic() + client.timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: raise TimeoutError('D01 bounded stream deadline')
                    sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                    if sock is not None: sock.settimeout(remaining)
                    line = response.readline(1024 * 1024 + 1)
                    if not line: break
                    size += len(line)
                    if size > old.MAX_CAPTURE or len(line) > 1024 * 1024:
                        raise resume.BatchError('D01 bounded SSE capture exceeded')
                    capture.write(line); capture.flush()
                    if line.strip():
                        if line.startswith(b'data:'): frame.append(line[5:].lstrip().decode('utf-8').rstrip('\r\n'))
                        continue
                    if not frame: continue
                    content = '\n'.join(frame); frame = []
                    if content == '[DONE]': natural_terminal = True; break
                    event = json.loads(content)
                    if event.get('error'): raise resume.BatchError('D01 SSE reported an error; never resend')
                    delta = ''.join(choice.get('delta', {}).get('content') or '' for choice in event.get('choices', []))
                    characters += len(delta)
                    if any(choice.get('finish_reason') for choice in event.get('choices', [])):
                        natural_terminal = True; break
                    if not delta: continue
                    operation, session = snapshot(home, before, approved, operation)
                    bind(state, operation, approved)
                    qualifies = (operation and operation.get('state') == 'running' and operation.get('session_id')
                        and session and session.get('node_id') and operation.get('vm_id')
                        and session.get('session_id') == operation['session_id'])
                    if not qualifies: continue
                    if metrics_deadline is None: metrics_deadline = time.monotonic() + 2
                    native = metric_evidence(metrics_file, operation, session, metrics_deadline)
                    if native is None: continue
                    # The writer wait may outlast a short generation. Re-observe
                    # running and stable disk identities immediately before close.
                    operation, current_session = snapshot(home, before, approved, operation)
                    if not operation or operation.get('state') != 'running': continue
                    if not current_session or current_session.get('node_id') != session['node_id']:
                        raise resume.SafetyError('IDENTITY_CONFLICT', 'Original client node changed before disconnect')
                    receipt.update(controlled_disconnect=True, disconnect_at=resume.common.now(),
                        first_content_seen=True, running_before_disconnect=True, operation_before_disconnect=operation,
                        session_before_disconnect=session, native_before_disconnect=native)
                    resume.common.atomic_json(folder / 'disconnect-receipt.json', receipt)
                    old.close_connection(response); response = None
                    break
        except urllib.error.HTTPError as error:
            error_class = type(error).__name__; receipt['http_status'] = error.code
            (folder / 'disconnect-http-error.bin').write_bytes(error.read(old.MAX_CAPTURE))
        except Exception as error:
            error_class = type(error).__name__; failure = getattr(error, 'failure_class', None)
        finally:
            if response is not None: old.close_connection(response)
        try:
            operation, session = snapshot(home, before, approved, operation)
            bind(state, operation, approved)
            if receipt['controlled_disconnect']:
                deadline = time.monotonic() + cleanup_timeout
                while True:
                    operation, session = snapshot(home, before, approved, operation)
                    current = old.get_json(client, '/admin/accounts')
                    rows = current if isinstance(current, list) else current.get('accounts', [])
                    account = next((a for a in rows if operation and a.get('id') == operation['account_id']), {})
                    backend = old.get_json(client, '/admin/backend')
                    cleared = bool(operation and operation.get('state') == 'unknown'
                        and account.get('protocol_blocked') == operation['id'] and backend.get('active_operations') == 0)
                    if cleared or (operation and operation.get('state') == 'completed') or time.monotonic() >= deadline:
                        receipt.update(operation_after_disconnect=operation, session_after_disconnect=session,
                            account_quarantined=bool(operation and account.get('protocol_blocked') == operation['id']),
                            active_operations=backend.get('active_operations'), bounded_active_cleanup_observed=cleared)
                        break
                    time.sleep(.1)
            bind(state, operation, approved)
        except Exception as error:
            error_class = type(error).__name__; failure = getattr(error, 'failure_class', None)
        completed = bool(operation and operation.get('state') == 'completed' and operation.get('delivery') == 'completed')
        observed = receipt['controlled_disconnect'] and receipt.get('bounded_active_cleanup_observed') is True
        status = 'NOT_OBSERVED' if completed and not observed else 'UNKNOWN'
        receipt.update(status=status, observation_status='FAIL' if error_class else 'PASS' if observed else 'NOT_OBSERVED',
            finished_at=resume.common.now(), captured_bytes=size, characters=characters, natural_terminal=natural_terminal,
            error_class=error_class, failure_class=failure, recovery_required=not completed, operation_bound=bool(operation))
        resume.common.atomic_json(folder / 'disconnect-receipt.json', receipt)
        state.update(CASE, status=status, characters=characters, bytes=size, disconnect_observed=bool(observed),
            error_class=error_class, failure_class=failure, recovery_required=not completed,
            active_cleanup_observed=receipt.get('bounded_active_cleanup_observed', False))
        state._record('FINISH', {'round': ROUND, 'changes': {'status': status, 'finished_at': resume.common.now(), 'diagnostic_type': 'CLIENT_DISCONNECT'}})
        result = state.summary(ROUND)
        result.update(observation_status=receipt['observation_status'], disconnect_observed=bool(observed),
            active_cleanup_observed=receipt.get('bounded_active_cleanup_observed', False),
            recovery_required=not completed, lease_directly_observed=False, natural_terminal=natural_terminal)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-root', type=Path, required=True)
    parser.add_argument('--original-ledger', type=Path, required=True); parser.add_argument('--original-sha256', required=True)
    parser.add_argument('--baseline-consumed', choices=('3,1,0',), required=True)
    parser.add_argument('--application-home', type=Path, required=True); parser.add_argument('--preflight', type=Path, required=True)
    parser.add_argument('--metrics-file', type=Path, required=True)
    parser.add_argument('--base', required=True); parser.add_argument('--key-env', default='MUSE2API_KEY')
    parser.add_argument('--timeout', type=int, default=180); parser.add_argument('--cleanup-timeout', type=float, default=12)
    args = parser.parse_args(argv)
    try:
        state = resume.State(args.private_root, args.original_ledger, args.original_sha256, resume.BASELINE)
        client = resume.common.Client(args.base, os.environ.get(args.key_env, ''), args.timeout)
        result = execute(state, client, args.application_home, args.preflight, args.metrics_file, args.cleanup_timeout)
        print(json.dumps(result)); return 0 if result['disconnect_observed'] or result['status'] == 'NOT_OBSERVED' else 1
    except Exception as error:
        print(json.dumps({'status': 'BLOCKED', 'error_class': type(error).__name__, 'note': 'Inspect private D01 evidence; no automatic replay/recovery/restart'})); return 2


if __name__ == '__main__': raise SystemExit(main())
