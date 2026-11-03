#!/usr/bin/env python3
"""One original chat.history GET. Requires a stopped service and exclusive Store.

No generation/recover/model/approval entrypoint is used. Raw evidence is written
only under the existing external resume case. Importing this module opens no Store.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit
import uuid

try:
    from tools import phase2_resume as resume
except ModuleNotFoundError as exc:
    if exc.name not in {"tools", "tools.phase2_resume"}: raise
    import phase2_resume as resume


SLEEP_STATES = {"SUSPENDED", "STOPPED", "SLEEPING", "HIBERNATED", "INACTIVE"}


class ProbeError(RuntimeError):
    pass


def private_json(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())


def check_bindings(record, saved, current):
    fields = {"operation_id": "id", "account_id": "account_id", "session_id": "session_id", "vm_id": "vm_id"}
    if record.get("operation_binding_verified") is not True or not record.get("marker"):
        raise ProbeError("Verified original operation required")
    if saved != current:
        raise ProbeError("Operation snapshot differs from current Store")
    for left, right in fields.items():
        if not isinstance(record.get(left), str) or not record[left] or record[left] != current.get(right):
            raise ProbeError("Original operation/account/session/VM differs")
    for key in ("message_id", "task_id"):
        if record.get(key) is not None and record[key] != current.get(key):
            raise ProbeError("Original message/task differs")
    prompt = current.get("prompt")
    if not isinstance(prompt, str) or record["marker"] not in prompt:
        raise ProbeError("Original prompt marker missing")
    return current


class HTTPGuard:
    """Preserve the Backend-owned observed Cookie client and its CAS callback."""
    LIMITS = {"/api/session": 64, "/api/hatch/vm/wake": 1,
              "/api/hatch/token": 4, "/api/hatch/noise-notary-token": 2, "noise": 4}
    def __init__(self, wrapped, vm_id, protocol):
        self.wrapped, self.vm_id, self.protocol = wrapped, vm_id, protocol
        self.calls, self.blocked, self.sessions = [], 0, []
    def __getattr__(self, key):
        return getattr(self.wrapped, key)
    def reject(self):
        self.blocked += 1
        raise ProbeError("HTTP/bootstrap route or original VM refused")
    def charge(self, route, method):
        if sum(x["route"] == route for x in self.calls) >= self.LIMITS[route]: self.reject()
        self.calls.append({"route": route, "method": method})
        return self.calls[-1]
    async def request(self, method, url, **kwargs):
        parsed = urlsplit(str(url)); path = parsed.path
        if parsed.scheme != "https" or parsed.netloc != "muse.ai" or parsed.query or parsed.fragment:
            self.reject()
        method = method.upper(); body = kwargs.get("json")
        if any(kwargs.get(k) is not None for k in ("data", "content", "files")): self.reject()
        if path == "/api/session":
            if method != "GET" or body is not None or kwargs.get("params") != {"vm_state": "live"}: self.reject()
        elif path in {"/api/hatch/vm/wake", "/api/hatch/token", "/api/hatch/noise-notary-token"}:
            if method != "POST" or not isinstance(body, dict) or kwargs.get("params") not in (None, {}): self.reject()
            key = "vm_id" if path.endswith("/wake") else "vmName"
            if body.get(key) != self.vm_id: self.reject()
            allowed = ({"vm_id", "retry_count", "connect_attempt_id"} if path.endswith("/wake") else
                       {"vmName", "vmAddress"} if path.endswith("/token") else {"vmName"})
            if set(body) - allowed: self.reject()
        else: self.reject()
        call = self.charge(path, method)
        kwargs.update(allow_redirects=False, max_redirects=0)
        kwargs["timeout"] = min(float(kwargs.get("timeout") or 20), 20)
        response = await self.wrapped.request(method, url, **kwargs)
        call["status"] = response.status_code
        if path == "/api/session" and response.status_code == 200:
            data = response.json()
            if isinstance(data, dict): self.sessions.append(data)
            if isinstance(data, dict) and data.get("vm_id") and data["vm_id"] != self.vm_id: self.reject()
        return response
    async def ws_connect(self, url, **kwargs):
        parsed = urlsplit(str(url)); query = parse_qs(parsed.query)
        if (parsed.scheme != "wss" or parsed.netloc != self.protocol.lb_host
                or parsed.path != self.protocol.ws_path or parsed.fragment
                or query.get("vm_id") != [self.vm_id]
                or not query.get("auth_token")
                or set(query) - {"vm_id", "auth_token", "notary_token", "app_id", "request_id"}): self.reject()
        self.charge("noise", "WS_CONNECT")
        return await self.wrapped.ws_connect(url, **kwargs)


class FrameGuard:
    """Patch the actual send boundary in this sole diagnostic process."""
    def __init__(self, connection_class, history_path, session_id):
        self.cls, self.path, self.session = connection_class, history_path, session_id
        self.calls, self.blocked = [], 0
    def validate(self, method, path, body=b"", json_body=False, service="daemon", **kwargs):
        parsed = urlsplit(path); query = parse_qs(parsed.query, keep_blank_values=True)
        if (method != "GET" or parsed.scheme or parsed.netloc or parsed.fragment
                or parsed.path != self.path or query != {"limit": ["60"], "session_id": [self.session]}
                or body or json_body or service != "daemon" or self.calls):
            self.blocked += 1
            raise ProbeError("Only one exact original-session history GET is allowed")
        self.calls.append({"method": "GET", "route": "chat.history"})
    def __enter__(self):
        self.original = self.cls._send_stream_frames
        guard = self
        async def send(connection, stream_id, **kwargs):
            guard.validate(**kwargs)
            return await guard.original(connection, stream_id, **kwargs)
        self.cls._send_stream_frames = send
        return self
    def __exit__(self, *args):
        self.cls._send_stream_frames = self.original


def summarize(history, operation, record):
    from muse2api.chat import _turn_lifecycle, event_text, history_events
    from muse2api.media import extract_media
    events = history_events(history)
    users = [e for e in events if (e.get("event_name") or e.get("event")) == "message.user"]
    assistants = [e for e in events if (e.get("event_name") or e.get("event")) == "message.assistant"]
    matches = [e for e in users if event_text(e).strip() == operation["prompt"].strip()
               and record["marker"] in event_text(e)]
    def payload(event):
        return event["payload"] if isinstance(event.get("payload"), dict) else {}
    wrong_sessions = [e for e in events if (e.get("session_id") or payload(e).get("session_id"))
                      not in (None, operation["session_id"])]
    turn = []
    if len(matches) == 1:
        for event in events[events.index(matches[0]) + 1:]:
            if (event.get("event_name") or event.get("event")) == "message.user": break
            turn.append(event)
    active, assistant_done, turn_done = _turn_lifecycle(turn)
    raw_lifecycle = []
    def observe(value, path="history"):
        if len(raw_lifecycle) >= 512: return
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"active", "is_active", "turn_done", "completed", "is_completed", "status", "state"} and isinstance(child, (bool, str, int)):
                    raw_lifecycle.append({"path": path + "." + key, "value": child})
                elif isinstance(child, (dict, list)): observe(child, path + "." + key)
        elif isinstance(value, list):
            for index, child in enumerate(value): observe(child, path + "[" + str(index) + "]")
    observe(history)
    return {"event_count": len(events), "user_messages": len(users), "assistant_messages": len(assistants),
            "original_prompt_matches": len(matches) == 1, "foreign_session_events": len(wrong_sessions),
            "media_reference_count": len(extract_media(events)),
            "original_turn_active": active, "original_assistant_done": assistant_done, "original_turn_done": turn_done,
            "lifecycle_scope": "SDK lifecycle of original prompt turn; assistant text alone does not establish completion",
            "original_turn_event_names": dict(Counter(str(e.get("event_name") or e.get("event") or "") for e in turn)),
            "raw_lifecycle_observations": raw_lifecycle,
            "original_message_id_observed": bool(operation.get("message_id") and any(
                (e.get("message_id") or payload(e).get("message_id")) == operation["message_id"] for e in events))}


async def inspect_history(backend, record, operation, protocol, directory, frame_guard):
    runtime = gate = None
    explicit_sleep = bootstrap_attempted = False
    result = {"case": record["case_id"], "alias": record.get("account_alias", "UNKNOWN"),
              "generation_requests": 0, "approval_requests": 0, "recover_called": False, "status": "BLOCKED", "wake_status": "NOT_OBSERVED"}
    try:
        async with backend._owned_runtime(record["account_id"], allow_blocked=True) as runtime:
            runtime.expected_vm = operation["vm_id"]
            runtime.settings.ws_fallback = False
            gate = HTTPGuard(runtime.http.client, operation["vm_id"], protocol)
            runtime.http._client = gate
            before = await runtime.http.session_info()
            private_json(directory / "session-before.json", before)
            if before.get("vm_id") and before["vm_id"] != operation["vm_id"]:
                result["same_vm"] = False
                raise ProbeError("Original VM changed before connect")
            explicit_sleep = (before.get("vm_id") == operation["vm_id"] and
                              str(before.get("vm_state") or "").upper() in SLEEP_STATES)
            boot_after = None
            if explicit_sleep:
                # A real, explicit sleeping pre-state is required before forcing wake.
                bootstrap_attempted = True
                boot_session = await runtime.http.bootstrap(force=True)
                if boot_session.vm_id != operation["vm_id"]:
                    result["same_vm"] = False
                    raise ProbeError("Bootstrap reassigned VM")
                boot_after = await runtime.http.session_info()
                private_json(directory / "session-after-bootstrap.json", boot_after)
                if boot_after.get("vm_id") != operation["vm_id"] or str(boot_after.get("vm_state") or "").upper() != "RUNNING":
                    raise ProbeError("Original VM did not reach real RUNNING after bootstrap")
            _, session, connection = await runtime.pool.connect()
            if session.vm_id != operation["vm_id"]:
                result["same_vm"] = False
                raise ProbeError("Original VM changed during connect")
            after = await runtime.http.session_info()
            private_json(directory / "session-after.json", after)
            if after.get("vm_id") != operation["vm_id"]:
                result["same_vm"] = False
                raise ProbeError("Original VM changed after connect")
            result.update(before_vm_state=before.get("vm_state"), after_vm_state=after.get("vm_state"), same_vm=True)
            response = await connection.request("GET", protocol.routes["chat.history"].path,
                {"session_id": operation["session_id"], "limit": 60}, timeout=30, max_response_bytes=8*1024*1024)
            response.raise_for_status(); history = response.json()
            private_json(directory / "history.json", history)
            result.update(summarize(history, operation, record))
            if not result["original_prompt_matches"] or result["foreign_session_events"]:
                raise ProbeError("Original prompt/session not proven by remote history")
            if gate.blocked or frame_guard.blocked: raise ProbeError("A route guard rejected an attempted request")
            result["status"] = "READ_VERIFIED"
            if (explicit_sleep and boot_after and str(after.get("vm_state") or "").upper() == "RUNNING"
                    and any(x["route"] == "/api/hatch/vm/wake" for x in gate.calls)):
                result["wake_status"] = "PASS"
    except Exception:
        if result.get("same_vm") is False: result["wake_status"] = "VM_REASSIGNED"
        elif explicit_sleep and bootstrap_attempted: result["wake_status"] = "FAIL"
        raise
    finally:
        if gate is not None:
            private_json(directory / "session-observations.json", gate.sessions)
            if any(x.get("vm_id") and x["vm_id"] != operation["vm_id"] for x in gate.sessions):
                result["same_vm"] = False
                result["wake_status"] = "VM_REASSIGNED"
            result.update(http_calls=gate.calls, http_blocked_attempts=gate.blocked,
                          wake_request_count=sum(x["route"] == "/api/hatch/vm/wake" for x in gate.calls))
        result.update(gateway_calls=frame_guard.calls, gateway_blocked_attempts=frame_guard.blocked)
        private_json(directory / "read-result.json", result)
    return result


def preservation(state, cfg, store, operation_path):
    paths = {"journal": state.path, "original_ledger": state.original,
             "operations": Path(cfg.protocol_operations_file), "tasks": Path(cfg.tasks_file),
             "operation_snapshot": operation_path}
    return {"file_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()},
            "operation_ids": sorted(store.protocol_operations), "task_ids": sorted(store.tasks),
            "account_ids": sorted(a["id"] for a in store.list_accounts())}


@contextmanager
def exclusive_store(root):
    path = root / "linux-preflight.lock"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(fd)
        yield
    finally: path.unlink()


def run_probe(args):
    if not sys.platform.startswith("linux") or not args.service_stopped:
        raise ProbeError("Linux and explicit stopped-service ownership required")
    state = resume.State(args.private_root, args.original_ledger, args.original_sha256, resume.BASELINE)
    data = state.load(); record = data["cases"].get(args.case)
    if not record or not resume.CASE.fullmatch(args.case): raise ProbeError("Recorded original case required")
    home = state.root.parent / "home"
    env_home = Path(os.environ.get("MUSE2API_HOME", ""))
    if not env_home.is_absolute() or env_home.resolve() != home.resolve():
        raise ProbeError("MUSE2API_HOME must equal the private round home")
    if os.environ.get("MUSE_BACKEND") != "protocol": raise ProbeError("Explicit protocol backend required")
    # Deliberately lazy: no Config/Store exists until original journal and HOME pass.
    from config import CFG
    from store import Store
    from protocol_backend import ProtocolBackend
    from muse2api.gateway import HatchConnection
    from muse2api.protocol import ProtocolConfig
    if Path(CFG.base_dir).resolve() != home.resolve() or CFG.backend != "protocol": raise ProbeError("Config differs")
    for name in ("data_dir", "accounts_file", "tasks_file", "protocol_operations_file"):
        path = Path(getattr(CFG, name)).resolve()
        if not path.is_relative_to(home.resolve()): raise ProbeError("Store path escaped private home")
    protocol = ProtocolConfig.load(); route = protocol.routes["chat.history"]
    if route.http_method != "GET" or not route.path.startswith("/") or "?" in route.path:
        raise ProbeError("History route must be a plain GET")
    if protocol.lb_host != "hatch.metaaivm.com" or protocol.ws_path != "/v1/noise":
        raise ProbeError("Unexpected Noise gateway configuration")
    operation_path = resume.external(state.root / "cases" / args.case / "operation.json")
    if not operation_path.is_relative_to(state.root): raise ProbeError("Case path escaped private resume root")
    saved = json.loads(operation_path.read_text("utf-8"))
    with resume.execution_lock(state), exclusive_store(state.root.parent):
        store = Store(CFG)
        operation = check_bindings(record, saved, store.get_protocol_operation(record.get("operation_id")))
        directory = operation_path.parent / ("history-probe-" + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        before = preservation(state, CFG, store, operation_path)
        private_json(directory / "preservation-before.json", before)
        backend = ProtocolBackend(store, CFG)
        previous_logging = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        result = {"case": args.case, "alias": record.get("account_alias", "UNKNOWN"), "status": "BLOCKED", "generation_requests": 0}
        try:
            with FrameGuard(HatchConnection, route.path, operation["session_id"]) as gate:
                try: result = backend._call(inspect_history(backend, record, operation, protocol, directory, gate), 180)
                finally: backend.stop()
        except Exception as error:
            read_result = directory / "read-result.json"
            if read_result.is_file(): result.update(json.loads(read_result.read_text("utf-8")))
            result.update(status="BLOCKED", error_type=type(error).__name__)
            private_json(directory / "error.json", {"error_type": type(error).__name__, "message": str(error)})
        finally:
            logging.disable(previous_logging)
            after = preservation(state, CFG, store, operation_path)
            private_json(directory / "preservation-after.json", after)
            result["preserved"] = before == after
            if before != after: result["status"] = "PRESERVATION_FAILED"
            private_json(directory / "summary.json", result)
        return {key: result.get(key) for key in ("case", "alias", "status", "event_count", "user_messages",
            "assistant_messages", "original_prompt_matches", "media_reference_count", "same_vm", "wake_request_count",
            "generation_requests", "preserved", "error_type", "original_turn_active", "original_assistant_done", "original_turn_done", "wake_status")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--original-ledger", type=Path, required=True)
    parser.add_argument("--original-sha256", required=True)
    parser.add_argument("--baseline-consumed", choices=("3,1,0",), required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--service-stopped", action="store_true", required=True)
    args = parser.parse_args(argv)
    try: result = run_probe(args)
    except Exception as error: result = {"case": args.case if resume.CASE.fullmatch(args.case) else "INVALID", "status": "BLOCKED", "generation_requests": 0, "error_type": type(error).__name__}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("status") == "READ_VERIFIED" and result.get("preserved") is True else 2


if __name__ == "__main__": raise SystemExit(main())
