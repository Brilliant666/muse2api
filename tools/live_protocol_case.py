#!/usr/bin/env python3
"""One explicit, budgeted acceptance case; never retries a generation POST.

Only the caller runs this against a live service. Tests use isolated loopback
fixtures. Raw requests, replies and identifiers stay in an explicitly private
state directory outside the repository. stdout contains redacted summaries.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = Path("C:/Users/brilliant/AppData/Local/Temp/muse2api-acceptance-20261103/live-cases")
LIMITS = {"text": 6, "image": 2, "video": 1}
CATEGORIES = {**{f"T{i}": "text" for i in range(1, 7)}, "I1": "image", "I2": "image", "V1": "video"}
ENDPOINTS = {"T1": "/v1/chat/completions", "T2": "/v1/chat/completions",
             "T3": "/v1/responses", "T4": "/v1/responses", "T6": "/v1/chat/completions",
             "I1": "/v1/images/tasks", "I2": "/v1/images/edits", "V1": "/v1/videos"}
MAX_RESPONSE = 32 * 1024 * 1024


class CaseError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class State:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        if self.directory.is_relative_to(ROOT):
            raise CaseError("Private state must be outside the Git repository")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "ledger.json"

    @contextmanager
    def locked(self):
        lock = self.directory / "ledger.lock"
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise CaseError("Ledger is locked; inspect the prior run, do not resend") from None
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(str(os.getpid()))
            yield
        finally:
            lock.unlink()

    def load(self):
        if not self.path.exists():
            return {"schema_version": 1, "limits": LIMITS, "cases": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("limits") != LIMITS or not isinstance(data.get("cases"), dict):
            raise CaseError("Ledger schema/limits differ; inspect rather than resetting it")
        return data

    def reserve(self, case, request_digest, endpoint=None):
        if case not in CATEGORIES:
            raise CaseError("Unknown case")
        with self.locked():
            ledger = self.load()
            if case in ledger["cases"]:
                raise CaseError("This case already has an intent; query/recover it, never resend")
            category = CATEGORIES[case]
            charged = sum(item["category"] == category for item in ledger["cases"].values())
            if charged >= LIMITS[category]:
                raise CaseError("Generation budget exhausted")
            receipt = {"case_id": case, "category": category, "units": 1, "status": "intent",
                       "intent_at": now(), "request_sha256": request_digest,
                       "endpoint": endpoint, "automatic_replay": False}
            ledger["cases"][case] = receipt
            atomic_json(self.path, ledger)
        return receipt

    def update(self, case, **changes):
        with self.locked():
            ledger = self.load()
            if case not in ledger["cases"]:
                raise CaseError("No prior send intent for this case")
            ledger["cases"][case].update(changes, updated_at=now())
            atomic_json(self.path, ledger)

    def claim_reserved(self, case):
        """Consume an external controlled intent exactly once (used by T5)."""
        with self.locked():
            ledger = self.load()
            receipt = ledger["cases"].get(case)
            if not receipt or receipt.get("status") != "intent" or receipt.get("send_started_at"):
                raise CaseError("No untouched reserved intent; inspect/recover, never resend")
            receipt.update(status="sending", send_started_at=now())
            atomic_json(self.path, ledger)
            return dict(receipt)

    def folder(self, case):
        if case not in CATEGORIES:
            raise CaseError("Unknown case")
        path = self.directory / case
        path.mkdir(exist_ok=True)
        return path

    def summary(self, case=None):
        ledger = self.load()
        consumed = {kind: sum(v["category"] == kind for v in ledger["cases"].values()) for kind in LIMITS}
        result = {"consumed": consumed, "remaining": {k: LIMITS[k] - consumed[k] for k in LIMITS}}
        if case:
            item = ledger["cases"].get(case, {})
            result.update(case_id=case, status=item.get("status", "not_sent"))
            for key in ("http_status", "characters", "events", "terminal", "bytes", "sha256", "error_class"):
                if key in item:
                    result[key] = item[key]
        return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, base, key, timeout=150):
        parts = urllib.parse.urlsplit(base)
        try:
            local = parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname or "").is_loopback
        except ValueError:
            local = False
        if (parts.scheme not in {"http", "https"} or not local or parts.username or parts.password
                or parts.path not in {"", "/"} or parts.query or parts.fragment):
            raise CaseError("Base must be a loopback HTTP(S) origin without credentials")
        if not key:
            raise CaseError("Service key is missing")
        self.base, self.key, self.timeout = base.rstrip("/"), key, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def open(self, path, body=None, headers=None):
        request = urllib.request.Request(self.base + path, data=body,
                    headers={"Authorization": "Bearer " + self.key, **(headers or {})},
                    method="GET" if body is None else "POST")
        return self.opener.open(request, timeout=self.timeout)


def read_key(path):
    # Deliberately extract only the service key, not Muse account variables.
    with Path(path).open(encoding="utf-8-sig") as stream:
        for line in stream:
            name, separator, value = line.strip().partition("=")
            if separator and name == "MUSE2API_KEY":
                return value.strip().strip("\"'")
    raise CaseError("MUSE2API_KEY is missing from the designated configuration")


def payload(case, prompt=None, timeout=None):
    default = ("A simple blue circle centered on a plain white background." if case == "I1" else
               "Combine the first red-square reference and second blue-circle reference in their original left-to-right order on white." if case == "I2" else
               "A blue circle slowly moving on a plain white background." if case == "V1" else
               "Reply with the words: Python protocol acceptance successful.")
    prompt = prompt or default
    timeout = timeout or (600 if case == "V1" else 360 if case.startswith("I") else 120)
    if case in {"T3", "T4"}:
        return {"model": "muse-spark", "input": prompt, "stream": case == "T4", "timeout": timeout}
    if case.startswith("T"):
        return {"model": "muse-spark", "messages": [{"role": "user", "content": prompt}],
                "stream": case in {"T2", "T5"}, "timeout": timeout}
    if case == "V1":
        return {"model": "muse-video", "prompt": prompt, "duration": 5, "timeout": timeout}
    return {"model": "muse-image", "prompt": prompt, "timeout": timeout, "async": True}


def multipart_edit(data, references):
    if len(references) != 2:
        raise CaseError("I2 requires exactly two ordered reference files")
    boundary = "acceptance-" + uuid.uuid4().hex
    parts, fingerprints = [], []
    for name, value in data.items():
        value = "true" if value is True else str(value)
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    for index, filename in enumerate(references):
        source = Path(filename)
        content = source.read_bytes()
        if len(content) > 10 * 1024 * 1024 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise CaseError("Reference fixtures must be PNG files within 10 MiB each")
        fingerprints.append(hashlib.sha256(content).hexdigest())
        header = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
                  f"filename=\"reference-{index + 1}.png\"\r\nContent-Type: image/png\r\n\r\n").encode()
        parts.extend((header, content, b"\r\n"))
    if fingerprints[0] == fingerprints[1]:
        raise CaseError("Reference fixtures must be distinguishable")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def frames(raw):
    event, data = "message", []
    for line in raw.decode("utf-8").splitlines() + [""]:
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


def validate_text(case, raw):
    if case in {"T2", "T4"}:
        text, events, finish, terminal = [], [], False, False
        response_id = None
        for event, data in frames(raw):
            if data == "[DONE]":
                if case != "T2" or not finish or terminal:
                    raise CaseError("Premature or unexpected stream terminal")
                terminal = True
                continue
            item = json.loads(data)
            kind = item.get("type", event)
            if item.get("error") or kind in {"error", "response.failed", "response.error"}:
                raise CaseError("Stream reported an error")
            if terminal:
                raise CaseError("Data arrived after terminal event")
            events.append(kind)
            if case == "T2":
                for choice in item.get("choices", []):
                    text.append(choice.get("delta", {}).get("content") or "")
                    finish = finish or choice.get("finish_reason") == "stop"
            else:
                if kind == "response.created":
                    response_id = item.get("response", {}).get("id")
                    if not response_id:
                        raise CaseError("Response creation has no identifier")
                elif kind == "response.output_text.delta":
                    if not events or events[0] != "response.created":
                        raise CaseError("Response delta preceded creation")
                    text.append(item.get("delta") or "")
                elif kind == "response.output_text.done":
                    if item.get("text") != "".join(text):
                        raise CaseError("Response final text differs from deltas")
                    finish = True
                elif kind == "response.completed":
                    result = item.get("response", {})
                    completed_text = "".join(part.get("text", "") for message in result.get("output", [])
                                             for part in message.get("content", [])
                                             if part.get("type") == "output_text")
                    if (not finish or result.get("status") != "completed"
                            or result.get("id") != response_id or completed_text != "".join(text)):
                        raise CaseError("Response completed before output text was done")
                    terminal = True
        if not terminal or not finish or not "".join(text).strip():
            raise CaseError("Stream ended without nonempty successful completion")
        if case == "T4" and (events[:3] != ["response.created", "response.output_item.added", "response.content_part.added"]
                or events[-3:] != ["response.output_text.done", "response.output_item.done", "response.completed"]
                or any(event != "response.output_text.delta" for event in events[3:-3])):
            raise CaseError("Response event order differs from the original contract")
        return {"characters": len("".join(text)), "events": len(events), "terminal": True}
    data = json.loads(raw)
    if case in {"T3"}:
        text = "".join(part.get("text", "") for item in data.get("output", [])
                       for part in item.get("content", []) if part.get("type") == "output_text")
        if data.get("status") != "completed" or not text.strip():
            raise CaseError("Responses returned no completed output")
    else:
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        text = message.get("content") or ""
        if message.get("role") != "assistant" or choice.get("finish_reason") != "stop" or not text.strip():
            raise CaseError("Chat returned no successful assistant output")
    return {"characters": len(text), "terminal": True}


def bounded_read(response, path):
    size = 0
    with Path(path).open("wb") as output:
        while block := response.read(65536):
            size += len(block)
            if size > MAX_RESPONSE:
                raise CaseError("Response exceeds capture limit")
            output.write(block)
    return Path(path).read_bytes()


def send(state, client, case, data, references=()):
    if case == "T5":
        raise CaseError("T5 uses reserve plus a manually controlled disconnect; send is forbidden")
    if case not in ENDPOINTS:
        raise CaseError("Unsupported send case")
    if case == "I2":
        body, content_type = multipart_edit(data, references)
    else:
        body, content_type = json.dumps(data).encode(), "application/json"
    state.reserve(case, hashlib.sha256(body).hexdigest(), ENDPOINTS[case])
    folder = state.folder(case)
    (folder / "request.bin").write_bytes(body)
    state.update(case, send_started_at=now(), status="sending")
    try:
        with client.open(ENDPOINTS[case], body, {"Content-Type": content_type,
                         "Idempotency-Key": "acceptance-" + case}) as response:
            status = response.status
            raw = bounded_read(response, folder / "response.bin")
        state.update(case, http_status=status)
        if case.startswith("T"):
            details = validate_text(case, raw)
            state.update(case, status="PASS", **details)
        else:
            reply = json.loads(raw)
            atomic_json(folder / "response.json", reply)
            task = reply.get("task_id") or reply.get("id")
            if not task:
                raise CaseError("Accepted media request returned no task identifier")
            state.update(case, status="ACCEPTED", task_id=task,
                         query_path=("/v1/videos/" if case == "V1" else "/v1/images/tasks/") + urllib.parse.quote(task, safe=""))
    except urllib.error.HTTPError as error:
        bounded_read(error, folder / "response.bin")
        state.update(case, status="FAIL", http_status=error.code, error_class="HTTPError")
    except Exception as error:
        state.update(case, status="UNKNOWN" if not isinstance(error, CaseError) else "FAIL",
                     error_class=type(error).__name__)
    return state.summary(case)


def query(state, client, case, wait=0):
    record = state.load()["cases"].get(case, {})
    path = record.get("query_path")
    if not path:
        raise CaseError("No recorded task; inspect the private original response/operation")
    deadline = time.monotonic() + wait
    while True:
        with client.open(path) as response:
            raw = bounded_read(response, state.folder(case) / "task-response.bin")
        task = json.loads(raw)
        atomic_json(state.folder(case) / "task.json", task)
        status = task.get("status", "unknown")
        state.update(case, status=status, operation_id=task.get("operation_id"))
        if status in {"completed", "failed", "unknown", "delivery_failed"} or time.monotonic() >= deadline:
            return state.summary(case)
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def recover(state, client, case, operation, timeout=60):
    if case not in state.load()["cases"]:
        raise CaseError("Recovery requires an existing case intent")
    if not operation:
        operation = state.load()["cases"][case].get("operation_id")
    if not operation:
        raise CaseError("An original recorded operation is required")
    body = json.dumps({"timeout": min(timeout, 300)}).encode()
    path = "/admin/protocol/operations/" + urllib.parse.quote(operation, safe="") + "/recover"
    try:
        with client.open(path, body, {"Content-Type": "application/json"}) as response:
            raw = bounded_read(response, state.folder(case) / "recover-response.bin")
    except urllib.error.HTTPError as error:
        bounded_read(error, state.folder(case) / "recover-response.bin")
        state.update(case, status="UNKNOWN", http_status=error.code, error_class="RecoveryHTTPError")
        return state.summary(case)
    result = json.loads(raw)
    atomic_json(state.folder(case) / "recovery.json", result)
    if result.get("resubmitted") is not False:
        raise CaseError("Recovery did not confirm resubmitted:false")
    state.update(case, status="RECOVERED" if result.get("ok") else "UNKNOWN")
    return state.summary(case)


def download(state, client, case, url, max_bytes=256 * 1024 * 1024):
    if case not in state.load()["cases"]:
        raise CaseError("Download requires an existing case intent")
    path = urllib.parse.urlsplit(url).path
    name = urllib.parse.unquote(path.removeprefix("/v1/media/"))
    if not path.startswith("/v1/media/") or not name or any(x in name for x in ("/", "\\", "..")):
        raise CaseError("Only an original /v1/media/ result path is allowed")
    destination = state.folder(case) / ("download-" + uuid.uuid4().hex + Path(name).suffix)
    temporary = destination.with_suffix(destination.suffix + ".part")
    size, digest = 0, hashlib.sha256()
    try:
        with client.open(path) as response, temporary.open("xb") as output:
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > max_bytes:
                    raise CaseError("Media exceeds download limit")
                output.write(block)
                digest.update(block)
        if not size:
            raise CaseError("Empty media download")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    state.update(case, bytes=size, sha256=digest.hexdigest(), downloaded_file=str(destination))
    return state.summary(case)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "reserve", "send", "query", "recover", "download"))
    parser.add_argument("--case", choices=tuple(CATEGORIES))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--base", default="http://127.0.0.1:18611")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--prompt")
    parser.add_argument("--request-file", type=Path, help="explicit JSON payload override; remains private")
    parser.add_argument("--reference", action="append", default=[], type=Path)
    parser.add_argument("--operation")
    parser.add_argument("--url")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--wait", type=int, default=0, help="query polling deadline; never resends")
    args = parser.parse_args(argv)
    if args.command != "status" and not args.case:
        parser.error("--case is required")
    try:
        state = State(args.state_dir)
        if args.command == "status":
            result = state.summary(args.case)
        elif args.command == "reserve":
            data = json.loads(args.request_file.read_text(encoding="utf-8-sig")) if args.request_file else payload(args.case, args.prompt)
            state.reserve(args.case, hashlib.sha256(json.dumps(data).encode()).hexdigest(),
                          ENDPOINTS.get(args.case, "/v1/chat/completions"))
            atomic_json(state.folder(args.case) / "reserved-request.json", data)
            result = state.summary(args.case)
        else:
            if not args.env_file:
                parser.error("explicit --env-file is required for network actions")
            client = Client(args.base, read_key(args.env_file), args.timeout)
            if args.command == "send":
                data = json.loads(args.request_file.read_text(encoding="utf-8-sig")) if args.request_file else payload(args.case, args.prompt)
                if data.get("n", 1) != 1:
                    raise CaseError("Each acceptance request must produce at most one media result")
                result = send(state, client, args.case, data, args.reference)
            elif args.command == "query":
                result = query(state, client, args.case, args.wait)
            elif args.command == "recover":
                result = recover(state, client, args.case, args.operation, args.timeout)
            else:
                if not args.url:
                    parser.error("download requires --url from the original result")
                result = download(state, client, args.case, args.url)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("status") not in {"FAIL", "UNKNOWN", "failed", "unknown", "delivery_failed"} else 1
    except Exception as error:
        # Exceptions can contain response text or identifiers; keep stdout terse.
        print(json.dumps({"status": "BLOCKED", "error_class": type(error).__name__,
                          "note": "Inspect private state; no automatic replay was performed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
