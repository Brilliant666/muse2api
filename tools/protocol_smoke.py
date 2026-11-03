#!/usr/bin/env python3
"""Verify a running integration through its existing HTTP API.

The default checks metadata only. --generate sends one text request and one
image task; --video additionally sends one video task. No generation is replayed.
Read the API key from MUSE2API_KEY; never pass a Muse Cookie to this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class CheckFailed(RuntimeError):
    pass


class Client:
    def __init__(self, base, key):
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise CheckFailed("--base must be an HTTP(S) URL without embedded credentials")
        self.base = base.rstrip("/")
        self.headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}

    def request(self, path, payload=None, *, timeout=90, headers=None):
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base + path, data=body,
            headers={**self.headers, **(headers or {})}, method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read(32768))
                error = data.get("error", data.get("detail", {}))
                operation = error.get("operation", {}) if isinstance(error, dict) else {}
                suffix = ("; operation=" + str(operation.get("request_id"))) if operation.get("request_id") else ""
            except (ValueError, TypeError, AttributeError):
                suffix = ""
            raise CheckFailed(f"{path}: HTTP {exc.code}{suffix}; no generation retry was performed") from None

    def media(self, url, max_bytes=256 * 1024 * 1024):
        # Download through the tested service, preserving only its public media path.
        path = urllib.parse.urlsplit(url).path
        if not path.startswith("/v1/media/"):
            raise CheckFailed("result did not contain a /v1/media/ URL")
        request = urllib.request.Request(self.base + path, headers=self.headers)
        digest, size, prefix = hashlib.sha256(), 0, b""
        with urllib.request.urlopen(request, timeout=90) as response:
            content_type = response.headers.get("Content-Type", "")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise CheckFailed("media exceeded the smoke-test download limit")
                prefix = (prefix + chunk)[:32]
                digest.update(chunk)
        if not size or prefix.lstrip().lower().startswith((b"<!doctype", b"<html", b"{")):
            raise CheckFailed("media was empty or contained an HTML/JSON error")
        return {"bytes": size, "sha256": digest.hexdigest(), "content_type": content_type}


def poll_task(client, path, task_id, timeout, report):
    report.update(task_id=task_id, status="pending", auto_replayed=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.request(path + "/" + urllib.parse.quote(task_id, safe=""), timeout=30)
        status = task.get("status")
        report["status"] = status
        if task.get("operation_id"):
            report["operation_id"] = task["operation_id"]
        if status in {"completed", "succeeded"}:
            return task
        if status in {"unknown", "delivery_failed"}:
            raise CheckFailed(f"{task_id}: {status}; use Tasks / Recover result, do not generate again")
        if status == "failed":
            raise CheckFailed(f"{task_id}: failed; inspect the original task before another generation")
        time.sleep(min(3, max(0, deadline - time.monotonic())))
    raise CheckFailed(f"{task_id}: local polling deadline reached; upstream outcome is unconfirmed")


def run(args, report):
    key = os.environ.get("MUSE2API_KEY", "")
    if not key and args.env_file:
        for line in args.env_file.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.strip().partition("=")
            if separator and name == "MUSE2API_KEY":
                key = value.strip().strip('\"\'')
                break
    if not key:
        raise CheckFailed("set MUSE2API_KEY to this service's API key (not a Muse Cookie)")
    client = Client(args.base, key)
    backend = client.request("/admin/backend")
    report["backend"] = backend
    if backend.get("backend") != "protocol" or backend.get("browser_running") is not False:
        raise CheckFailed("service is not running the protocol backend")
    models = client.request("/v1/models")
    model_ids = {m.get("id") for m in models.get("data", [])}
    if not {"muse-spark", "muse-image", "muse-video"}.issubset(model_ids):
        raise CheckFailed("original model aliases are missing")
    report["metadata"] = "passed"
    accounts = client.request("/admin/accounts").get("accounts", [])
    enabled = [a for a in accounts if a.get("enabled", True)]
    report["enabled_accounts"] = len(enabled)
    if args.recover:
        result = client.request("/admin/protocol/operations/" + urllib.parse.quote(args.recover, safe="") + "/recover",
                                {"timeout": min(args.timeout, 300)}, timeout=min(args.timeout, 300) + 15)
        report["recovery"] = {"operation_id": args.recover, "ok": result.get("ok"), "resubmitted": result.get("resubmitted")}
        return
    if not args.generate:
        report["live_generation"] = "not_requested"
        return
    if len(enabled) != 1:
        raise CheckFailed("use a separate test deployment with exactly one enabled account before --generate")
    account = enabled[0]
    if account.get("protocol_blocked"):
        raise CheckFailed("the test account has an unresolved operation; recover it first")
    checked = client.request("/admin/accounts/" + urllib.parse.quote(account["id"], safe="") + "/test", {}, timeout=110)
    report["session_check"] = bool(checked.get("ok"))
    if not checked.get("ok"):
        raise CheckFailed("the account session check did not succeed")

    chat = client.request("/v1/chat/completions", {"model": "muse-spark", "stream": False,
        "messages": [{"role": "user", "content": "Reply with the words: Python protocol test successful."}],
        "timeout": min(args.timeout, 300)}, timeout=min(args.timeout, 300) + 20)
    answer = chat.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not isinstance(answer, str) or not answer.strip():
        raise CheckFailed("text completion returned no text")
    report["chat"] = {"passed": True, "characters": len(answer)}

    record = report.setdefault("image", {})
    task = client.request("/v1/images/tasks", {"prompt": "A simple blue circle centered on a plain white background.",
        "model": "muse-image", "timeout": min(args.timeout, 600)},
        headers={"Idempotency-Key": "protocol-smoke-" + uuid.uuid4().hex})
    if not task.get("id"):
        raise CheckFailed("image task response has no id")
    task = poll_task(client, "/v1/images/tasks", task["id"], args.timeout, record)
    data = task.get("data") or []
    url = data[0].get("url") if data else (task.get("result") or {}).get("url")
    if not url:
        raise CheckFailed("completed image task has no result URL")
    record["download"] = client.media(url)

    if args.video:
        record = report.setdefault("video", {})
        task = client.request("/v1/videos", {"prompt": "A blue circle slowly moving on a plain white background.",
            "model": "muse-video", "duration": 5, "timeout": min(args.timeout, 600)})
        if not task.get("id"):
            raise CheckFailed("video task response has no id")
        task = poll_task(client, "/v1/videos", task["id"], args.timeout, record)
        url = (task.get("result") or {}).get("url") or task.get("url")
        if not url:
            raise CheckFailed("completed video task has no result URL")
        record["download"] = client.media(url)
    report["live_generation"] = "passed_for_requested_cases"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:18610")
    parser.add_argument("--env-file", type=Path, help="read only MUSE2API_KEY from this local configuration")
    parser.add_argument("--generate", action="store_true", help="send one text and one image generation")
    parser.add_argument("--video", action="store_true", help="also send one video generation; requires --generate")
    parser.add_argument("--recover", help="recover an existing operation without generating anything")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.video and not args.generate:
        parser.error("--video requires --generate")
    if args.recover and args.generate:
        parser.error("--recover and --generate are mutually exclusive")
    if not 1 <= args.timeout <= 1800:
        parser.error("--timeout must be between 1 and 1800 seconds")
    report = {"started_at": int(time.time()), "completed": False,
              "scope": "running service HTTP smoke test, not full lifecycle certification"}
    code = 0
    try:
        run(args, report)
        report["completed"] = True
    except (CheckFailed, OSError, ValueError) as exc:
        report["error"] = str(exc)
        code = 1
    finally:
        report["finished_at"] = int(time.time())
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.report:
            args.report.write_text(text + "\n", encoding="utf-8")
        print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
