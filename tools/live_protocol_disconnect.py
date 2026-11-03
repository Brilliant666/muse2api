#!/usr/bin/env python3
"""Consume an already budgeted T5 intent exactly once, then disconnect its SSE.

No service or process is restarted here. Identifiers, partial output and disk
snapshots stay in the explicit private state directory. A successful controlled
disconnect is still an UNKNOWN case until the caller restarts and recovers the
original operation. Tests use a synthetic loopback HTTP peer only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import socket
import time
import urllib.error

from live_protocol_case import (CaseError, Client, DEFAULT_STATE, State,
                                atomic_json, now, read_key)


MAX_CAPTURE = 4 * 1024 * 1024


def disk_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # SessionStore.save is not atomic. Observe again rather than interpreting
        # a transient partial read as a new session or a reason to generate.
        return default


def get_json(client, route):
    with client.open(route) as response:
        raw = response.read(MAX_CAPTURE + 1)
    if len(raw) > MAX_CAPTURE:
        raise CaseError("Metadata response exceeded capture bound")
    return json.loads(raw)


def operation_snapshot(home, before, account):
    operations = disk_json(home / "data" / "protocol_operations.json", {})
    if not isinstance(operations, dict):
        raise CaseError("Unexpected operation store format")
    selected = [value for key, value in operations.items() if key not in before
                and value.get("account_id") == account and value.get("kind") == "chat"]
    if len(selected) > 1:
        raise CaseError("Concurrent operation identities are ambiguous; do not resend")
    if not selected:
        return None, None
    operation = selected[0]
    key = operation.get("session_key") or "operation:" + operation["id"]
    directory = hashlib.sha256(account.encode()).hexdigest()[:20]
    session = disk_json(home / "data" / "protocol" / directory / "chat_sessions.json", {}).get(key)
    return operation, session


def close_connection(response):
    # urllib's buffered response may retain a live socket after close(). Shutdown
    # the owned connection explicitly so the ASGI disconnect is observable.
    raw = getattr(getattr(response, "fp", None), "raw", None)
    connection = getattr(raw, "_sock", None)
    if connection is not None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    response.close()


def execute(state, client, home, cleanup_timeout=12):
    home = Path(home).expanduser().resolve()
    if home.parent != state.directory.parent:
        raise CaseError("Application home and private ledger must share the isolated test root")
    folder = state.folder("T5")
    request_file = folder / "reserved-request.json"
    data = json.loads(request_file.read_text(encoding="utf-8"))
    if data.get("stream") is not True or not data.get("messages"):
        raise CaseError("T5 requires a reserved streaming Chat request")
    body = json.dumps(data).encode()
    reserved = state.load()["cases"].get("T5", {})
    if reserved.get("request_sha256") != hashlib.sha256(body).hexdigest():
        raise CaseError("Reserved request changed; never send a different payload")
    if reserved.get("endpoint") != "/v1/chat/completions":
        raise CaseError("T5 reserve endpoint differs")
    accounts = get_json(client, "/admin/accounts").get("accounts", [])
    enabled = [a for a in accounts if a.get("enabled", True)]
    if len(accounts) != 1 or len(enabled) != 1 or enabled[0].get("protocol_blocked"):
        raise CaseError("T5 requires exactly one enabled, unblocked dedicated test account")
    account = enabled[0]["id"]
    before = set(disk_json(home / "data" / "protocol_operations.json", {}))
    # This is the only send claim. A crash after it conservatively consumes the
    # intent; a second execution must fail before another generation POST.
    state.claim_reserved("T5")
    receipt = {"case_id": "T5", "started_at": now(), "account_id": account,
               "controlled_disconnect": False, "recovery_required": False,
               "generation_post_attempts": 1, "automatic_replay": False,
               "lease_directly_observed": False}
    atomic_json(folder / "disconnect-receipt.json", receipt)
    response = None
    operation, session = None, None
    error_class = None
    size, characters = 0, 0
    natural_terminal = False
    try:
        response = client.open("/v1/chat/completions", body,
                               {"Content-Type": "application/json", "Accept": "text/event-stream"})
        receipt["http_status"] = response.status
        with (folder / "disconnect-stream.bin").open("wb") as capture:
            frame = []
            deadline = time.monotonic() + client.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("T5 response deadline elapsed")
                raw_socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if raw_socket is not None:
                    raw_socket.settimeout(remaining)
                line = response.readline(1024 * 1024 + 1)
                if not line:
                    break
                size += len(line)
                if size > MAX_CAPTURE or len(line) > 1024 * 1024:
                    raise CaseError("T5 response capture exceeded bound")
                capture.write(line)
                capture.flush()
                if line.strip():
                    if line.startswith(b"data:"):
                        frame.append(line[5:].lstrip().decode("utf-8").rstrip("\r\n"))
                    continue
                if not frame:
                    continue
                content = "\n".join(frame)
                frame = []
                if content == "[DONE]":
                    natural_terminal = True
                    break
                event = json.loads(content)
                if event.get("error"):
                    raise CaseError("T5 stream reported an error; do not replay")
                delta = "".join(choice.get("delta", {}).get("content") or ""
                                for choice in event.get("choices", []))
                characters += len(delta)
                if not delta:
                    continue
                operation, session = operation_snapshot(home, before, account)
                if not operation or operation.get("state") != "running":
                    continue
                if (not session or not session.get("node_id") or not operation.get("vm_id")
                        or operation.get("session_id") != session.get("session_id")):
                    continue
                # A real content delta establishes a submitted turn; disk state
                # establishes it was still running at this observation.
                receipt.update(controlled_disconnect=True, disconnect_at=now(),
                               first_content_seen=True, running_before_disconnect=True,
                               operation_before_disconnect=operation, session=session,
                               operation_id=operation["id"], recovery_required=True)
                atomic_json(folder / "disconnect-receipt.json", receipt)
                close_connection(response)
                response = None
                break
    except urllib.error.HTTPError as error:
        error_class = "HTTPError"
        receipt["http_status"] = error.code
        (folder / "disconnect-http-error.bin").write_bytes(error.read(MAX_CAPTURE))
    except Exception as error:
        error_class = type(error).__name__
    finally:
        if response is not None:
            close_connection(response)

    if operation is None:
        operation, session = operation_snapshot(home, before, account)
    if operation:
        receipt["operation_id"] = operation["id"]
        state.update("T5", operation_id=operation["id"])
    if receipt["controlled_disconnect"]:
        deadline = time.monotonic() + cleanup_timeout
        while True:
            operation, session = operation_snapshot(home, before, account)
            backend = get_json(client, "/admin/backend")
            current = get_json(client, "/admin/accounts").get("accounts", [])
            selected = next((a for a in current if a.get("id") == account), {})
            cleared = bool(operation and operation.get("state") == "unknown"
                           and selected.get("protocol_blocked") == operation["id"]
                           and backend.get("active_operations") == 0)
            if cleared or time.monotonic() >= deadline:
                receipt.update(operation_after_disconnect=operation, session_after_disconnect=session,
                               account_quarantined=bool(operation and selected.get("protocol_blocked") == operation["id"]),
                               active_operations=backend.get("active_operations"),
                               bounded_active_cleanup_observed=cleared)
                break
            time.sleep(0.1)
        status = "UNKNOWN"
    else:
        status = "NOT_OBSERVED" if natural_terminal else "UNKNOWN"
        receipt["not_observed_reason"] = ("Turn completed before a running submitted disconnect was observed"
                                           if natural_terminal else "No qualifying running submitted content observation")
    receipt.update(status=status, finished_at=now(), captured_bytes=size, characters=characters,
                   natural_terminal=natural_terminal, error_class=error_class)
    atomic_json(folder / "disconnect-receipt.json", receipt)
    state.update("T5", status=status, characters=characters, bytes=size,
                 disconnect_observed=receipt["controlled_disconnect"],
                 error_class=error_class)
    summary = state.summary("T5")
    summary.update(disconnect_observed=receipt["controlled_disconnect"],
                   active_cleanup_observed=receipt.get("bounded_active_cleanup_observed", False),
                   lease_directly_observed=False, recovery_required=receipt["recovery_required"])
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--application-home", required=True, type=Path)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--base", default="http://127.0.0.1:18611")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--cleanup-timeout", type=float, default=12)
    args = parser.parse_args(argv)
    try:
        if not 0 < args.cleanup_timeout <= 60 or not 0 < args.timeout <= 600:
            raise CaseError("Timeout outside allowed bounds")
        result = execute(State(args.state_dir), Client(args.base, read_key(args.env_file), args.timeout),
                         args.application_home, args.cleanup_timeout)
        print(json.dumps(result))
        return 0 if result["disconnect_observed"] or result["status"] == "NOT_OBSERVED" else 1
    except Exception as error:
        print(json.dumps({"status": "BLOCKED", "error_class": type(error).__name__,
                          "note": "Inspect private T5 receipt; never resend an existing intent"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
