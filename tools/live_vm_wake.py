"""Observe a real idle VM, wake once, then read an existing chat: no generation.

Run only while the application's service is stopped. Raw session/history payloads
stay in a new private receipt directory beneath the ORIGINAL acceptance root.
The existing ledger is mandatory and never modified. Do not import app here.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import sys
import time


TERMINAL = {"PASS", "completed", "RECOVERED"}
IDLE_STATES = {"ASLEEP", "SLEEPING", "SUSPENDED", "STOPPED", "INACTIVE", "HIBERNATED"}
HTTP_ALLOWED = {("GET", "/api/session"), ("POST", "/api/hatch/vm/wake"),
                ("POST", "/api/hatch/token"), ("POST", "/api/hatch/noise-notary-token")}


class ExclusiveConflict(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else
                          json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load(path):
    return json.loads(path.read_text("utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")


def assert_service_off(port):
    with socket.socket() as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise ExclusiveConflict("Service is running: exclusive Store ownership required")


def footprint(root):
    ledger_path = root / "live-cases/ledger.json"
    ledger = load(ledger_path)
    cases = ledger["cases"]
    if set(cases) != {"T1", "T2", "T3", "T4", "T5", "T6", "I1", "I2", "V1"}:
        raise RuntimeError("The original nine-case ledger is required")
    if any(c["status"] not in TERMINAL for c in cases.values()):
        raise RuntimeError("The original cases are not all complete")
    used = collections.Counter()
    for case in cases.values():
        used[case["category"]] += case["units"]
    if dict(used) != {"text": 6, "image": 2, "video": 1}:
        raise RuntimeError("Original 6/2/1 consumed budget not found")
    operations_path = root / "application/data/protocol_operations.json"
    operations = load(operations_path)
    if len(operations) != 8 or any(o["state"] != "completed" for o in operations.values()):
        raise RuntimeError("Eight completed original operations required")
    tasks_path = root / "application/data/tasks.json"
    tasks = load(tasks_path)
    if len(tasks) != 3 or any(t["status"] != "completed" for t in tasks.values()):
        raise RuntimeError("Three completed original media tasks required")
    return {"ledger_sha256": digest(ledger_path.read_bytes()), "used": dict(used),
            "intent_count": len(cases), "intent_ids": sorted(cases),
            "operation_ids": sorted(operations), "operation_sha256": digest(operations_path.read_bytes()),
            "task_ids": sorted(tasks), "task_sha256": digest(tasks_path.read_bytes())}


def explicit_idle(info):
    return bool(info.get("vm_id") and info.get("endpoint_url") and
                str(info.get("vm_state") or "").upper() in IDLE_STATES)


def cookie_snapshot(account):
    return {"credential_revision": account.get("credential_revision"),
            "cookies": {k: digest(v.encode())[:16] for k, v in account.get("cookies", {}).items()},
            "expires": account.get("cookies_exp", {})}


def original_turn(events, operation, text_of):
    """Match the original user ID/prompt and its assistant reply in the same turn."""
    for index, event in enumerate(events):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if (event.get("message_id") or payload.get("message_id")) != operation["message_id"]:
            continue
        if (event.get("event_name") or event.get("event")) != "message.user":
            continue
        prompt_matches = text_of(event) == operation["prompt"]
        following = []
        for reply in events[index + 1:]:
            name = reply.get("event_name") or reply.get("event")
            if name == "message.user":
                break
            if name == "message.assistant":
                following.append(reply)
        return {"original_user_message_id_matched": True, "original_user_prompt_matched": prompt_matches,
                "original_assistant_text_in_same_turn": any(text_of(e) == operation["result"]["text"]
                                                             for e in following)}
    return {"original_user_message_id_matched": False, "original_user_prompt_matched": False,
            "original_assistant_text_in_same_turn": False}


async def observe(backend, operation, args, private, result):
    from muse2api.gateway import HatchConnection
    from muse2api.protocol import ProtocolConfig
    from muse2api.chat import history_events, event_text

    account_id = operation["account_id"]
    async with backend._owned_runtime(account_id) as rt:
        original_request = rt.http.request_json
        original_wake = rt.http._wake_vm
        started = time.monotonic()
        deadline = started + args.observe_minutes * 60
        phase = "observe"
        wake_vm_ids = []

        async def request(method, path, **kwargs):
            if (method.upper(), path) not in HTTP_ALLOWED:
                raise RuntimeError("Diagnostic HTTP allowlist rejected request")
            assert_service_off(args.service_port)
            row = {"at": timestamp(), "elapsed_seconds": round(time.monotonic()-started, 3),
                   "phase": phase, "method": method.upper(), "path": path}
            result["http_requests"].append(row)
            write(private / "result.json", result)
            data = await original_request(method, path, **kwargs)
            write(private / ("http-%02d.json" % len(result["http_requests"])), data)
            row["response_received_at"] = timestamp()
            if path == "/api/session":
                row.update(status=data.get("status"), vm_state=data.get("vm_state"),
                           vm_fingerprint=digest(str(data.get("vm_id") or "").encode())[:16],
                           has_vm_id=bool(data.get("vm_id")), has_endpoint=bool(data.get("endpoint_url")),
                           matches_original_vm=data.get("vm_id") == operation["vm_id"])
                if wake_vm_ids and str(data.get("vm_state") or "").upper() == "RUNNING":
                    result.setdefault("running_after_wake_at", row["response_received_at"])
                print(json.dumps({"phase": phase, "elapsed_seconds": row["elapsed_seconds"],
                                  "status": row["status"], "vm_state": row["vm_state"],
                                  "matches_original_vm": row["matches_original_vm"]}), flush=True)
            write(private / "result.json", result)
            return data

        async def wake(vm_id):
            wake_vm_ids.append(vm_id)
            result["wake_calls"].append({"at": timestamp(), "matches_original_vm": vm_id == operation["vm_id"],
                                         "vm_fingerprint": digest(vm_id.encode())[:16]})
            write(private / "result.json", result)
            return await original_wake(vm_id)

        rt.http.request_json = request
        rt.http._wake_vm = wake
        try:
            target = None
            next_probe = started
            while True:
                while time.monotonic() < next_probe:
                    await asyncio.sleep(min(5, next_probe - time.monotonic()))
                    assert_service_off(args.service_port)
                try:
                    info = await rt.http.session_info()
                except ExclusiveConflict:
                    raise
                except Exception as exc:
                    result["observation_errors"].append({"at": timestamp(), "type": type(exc).__name__})
                    info = {}
                if explicit_idle(info):
                    target = info
                    result["explicit_idle_observed"] = True
                    result["idle_state"] = info["vm_state"]
                    result["idle_observed_at"] = timestamp()
                    break
                if time.monotonic() >= deadline:
                    result["status"] = "NOT_OBSERVED" if any(
                        "response_received_at" in r for r in result["http_requests"]) else "BLOCKED"
                    result["reason"] = "No explicit idle state captured within the bounded window"
                    return
                next_probe = min(next_probe + args.interval_seconds, deadline)
            if target["vm_id"] != operation["vm_id"]:
                result.update(status="NOT_OBSERVED", reason="VM reassigned; original VM wake not proved",
                              vm_reassigned=True)
                return
            phase = "bootstrap"
            result["bootstrap_force"] = True
            result["bootstrap_started_at"] = timestamp()
            session = await rt.http.bootstrap(force=True)
            result["bootstrap_finished_at"] = timestamp()
            phase = "post_bootstrap"
            after = await rt.http.session_info()
            same_vm = (session.vm_id == target["vm_id"] == operation["vm_id"] == after.get("vm_id") and
                       all(v == operation["vm_id"] for v in wake_vm_ids))
            result["same_original_vm"] = same_vm
            if not same_vm:
                result.update(status="NOT_OBSERVED", vm_reassigned=True, reason="VM changed during bootstrap")
                return
            if not wake_vm_ids:
                result.update(status="NOT_OBSERVED", reason="Explicit idle seen, but bootstrap did not take wake path")
                return
            if str(after.get("vm_state") or "").upper() != "RUNNING":
                raise RuntimeError("Real post-bootstrap session is not RUNNING")
            phase = "noise_history"
            assert_service_off(args.service_port)
            protocol = ProtocolConfig.load()
            conn = await HatchConnection.connect(session, lb_host=protocol.lb_host, ws_path=protocol.ws_path,
                cvm_rv_key=rt.settings.cvm_rv_key, attestation_cmd=rt.settings.attestation_cmd,
                ws_client=rt.http.client, impersonate=rt.settings.impersonate,
                user_agent=rt.settings.user_agent, proxy=rt.proxy, ws_fallback=rt.settings.ws_fallback)
            rt.conn = conn
            result["noise_connected_at"] = timestamp()
            path = protocol.route("chat.history").path
            assert_service_off(args.service_port)
            result["gateway_requests"].append({"at": timestamp(), "method": "GET", "route": "chat.history"})
            response = await conn.request("GET", path, {"session_id": operation["session_id"], "limit": 60}, timeout=20)
            response.raise_for_status()
            data = response.json()
            write(private / "history.json", data)
            events = history_events(data)
            expected = operation["result"]["text"]
            matched = original_turn(events, operation, event_text)
            result["history"] = {"http_status": response.status, "event_count": len(events),
                "same_original_session_queried": True, **matched,
                "expected_text_sha256": digest(expected.encode()), "expected_characters": len(expected),
                "received_at": timestamp(), "payload_sha256": digest(data)}
            if not all(matched.values()):
                raise RuntimeError("Remote history did not match the original user ID/prompt and same-turn reply")
            result["status"] = "PASS"
        except ExclusiveConflict:
            result.update(status="BLOCKED", reason="Exclusive Store ownership lost; stopped immediately")
        except Exception as exc:
            result["status"] = "FAIL" if result["wake_calls"] else "BLOCKED"
            result["error_type"] = type(exc).__name__
            # Exception strings can contain signed URLs or raw IDs: keep them private.
            write(private / "error.json", {"type": type(exc).__name__, "message": str(exc)})
        finally:
            if rt.conn is not None:
                await rt.conn.close()
                rt.conn = None
            result["observation_elapsed_seconds"] = round(time.monotonic() - started, 3)
            write(private / "result.json", result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--service-port", type=int, default=18611)
    parser.add_argument("--observe-minutes", type=float, default=30)
    parser.add_argument("--interval-seconds", type=int, default=600)
    args = parser.parse_args()
    if not (0 < args.observe_minutes <= 30 and args.interval_seconds >= 300):
        parser.error("Maximum 30 minutes; idle queries must be at least five minutes apart")
    root = args.private_root.resolve(strict=True)
    private = args.receipts.resolve()
    if root not in private.parents or private.exists():
        parser.error("Use a new receipt directory beneath the original private root")
    before = footprint(root)
    assert_service_off(args.service_port)
    lock_path = root / "vm-wake-exclusive.lock"
    lock = lock_path.open("x")
    lock.write(str(os.getpid())); lock.flush()
    backend = None
    future = None
    result = {"status": "BLOCKED", "started_at": timestamp(), "additional_generation_budget":
        {"text": 0, "image": 0, "video": 0}, "maximum_observation_minutes": args.observe_minutes,
        "interval_seconds": args.interval_seconds, "http_requests": [], "gateway_requests": [],
        "wake_calls": [], "observation_errors": [], "explicit_idle_observed": False,
        "no_app_import_or_background_keepalive": True}
    try:
        private.mkdir()
        write(private / "before.json", before)
        from dotenv import dotenv_values
        for key in list(os.environ):
            if key.startswith(("MUSE_", "MUSE2API_")):
                del os.environ[key]
        os.environ.update({k: v for k, v in dotenv_values(args.env_file).items() if v is not None})
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from config import Config
        from store import Store
        from protocol_backend import ProtocolBackend
        cfg = Config()
        if Path(cfg.base_dir).resolve() != (root / "application").resolve():
            raise RuntimeError("Configured Store is not the original acceptance Store")
        ledger = load(root / "live-cases/ledger.json")
        store = Store(cfg)
        operation = store.protocol_operations[ledger["cases"]["T5"]["operation_id"]]
        if not all(operation.get(k) for k in ("account_id", "session_id", "vm_id", "message_id")):
            raise RuntimeError("Original private operation identities are incomplete")
        write(private / "original-identity.json", operation)
        result["cookie_before"] = cookie_snapshot(store.get_account(operation["account_id"]))
        backend = ProtocolBackend(store, cfg)
        # Use the existing Backend's event loop/Store-owned transport, not app startup.
        future = backend._submit(observe(backend, operation, args, private, result))
        future.result(timeout=args.observe_minutes * 60 + 150)
        result["cookie_after"] = cookie_snapshot(store.get_account(operation["account_id"]))
    except Exception as exc:
        if future is not None:
            future.cancel()
        result["status"] = "FAIL" if result["wake_calls"] else "BLOCKED"
        result["error_type"] = type(exc).__name__
        if private.exists():
            write(private / "outer-error.json", {"type": type(exc).__name__, "message": str(exc)})
    finally:
        if backend is not None:
            backend.stop()
        try:
            after = footprint(root)
        except Exception as exc:
            after = {"validation_error": type(exc).__name__}
        if private.exists():
            write(private / "after.json", after)
            result["original_footprint_unchanged"] = before == after
            result["finished_at"] = timestamp()
            result["diagnostic_connections_closed"] = True
            if before != after:
                result.update(status="FAIL", reason="Original ledger/operation/task footprint changed")
            write(private / "result.json", result)
            print(json.dumps({"status": result["status"], "unchanged": before == after,
                "budget": after.get("used"), "wake_calls": len(result["wake_calls"]),
                "gateway_gets": len(result["gateway_requests"])}), flush=True)
        lock.close()
        lock_path.unlink()


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)  # upstream logging may contain VM IDs/URLs
    main()
