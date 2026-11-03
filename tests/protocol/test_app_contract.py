"""Real ASGI/application/Store contracts with deterministic protocol boundaries.

These tests never contact Muse, start Chromium, or replace FastAPI/Starlette.
The injected transport/service/downloader isolate network effects while the
actual public routes, account scheduler, backend facade and Store run together.
Missing import dependencies produce an explicit skip, not a claimed pass.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit


from _paths import ROOT, APP_DIR, PROTOCOL_DIR
KEY = "contract-test-key"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jS1sAAAAASUVORK5CYII="
)
# The second reference intentionally has different bytes; no image model is used.
PNG2 = PNG + b"reference-two"
VIDEO = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42"


class ASGIResponse:
    def __init__(self, status, headers, body):
        self.status_code = status
        self.headers = {k.decode().lower(): v.decode() for k, v in headers}
        self.content = body
        self.text = body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content)


async def asgi_request(application, method, target, *, payload=None, body=None,
                       headers=None, disconnect_when=None, timeout=10):
    """An in-process ASGI request; avoids an HTTP-client package dependency."""
    parsed = urlsplit(target)
    raw = body if body is not None else (
        json.dumps(payload).encode() if payload is not None else b""
    )
    request_headers = {"host": "testserver", **(headers or {})}
    if payload is not None:
        request_headers.setdefault("content-type", "application/json")
    request_headers["content-length"] = str(len(raw))
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method,
        "scheme": "http", "path": parsed.path,
        "raw_path": parsed.path.encode(), "query_string": parsed.query.encode(),
        "root_path": "", "headers": [(k.lower().encode(), v.encode())
                                          for k, v in request_headers.items()],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
    }
    delivered = False
    disconnected = asyncio.Event()
    outgoing = []
    status, response_headers = None, []

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal status, response_headers
        if message["type"] == "http.response.start":
            status, response_headers = message["status"], message["headers"]
        elif message["type"] == "http.response.body":
            part = message.get("body", b"")
            outgoing.append(part)
            if disconnect_when and disconnect_when(part):
                disconnected.set()
            if not message.get("more_body", False):
                disconnected.set()

    await asyncio.wait_for(application(scope, receive, send), timeout)
    return ASGIResponse(status, response_headers, b"".join(outgoing))


class SyntheticOperationError(RuntimeError):
    def __init__(self, *, delivery="unknown", code="timeout", phase="history", retryable=False):
        super().__init__("synthetic upstream failure")
        self.code, self.phase = code, phase
        self.delivery, self.retryable = delivery, retryable
        self.session_id, self.message_id = "test-session", "test-message"


class MuseAuthError(RuntimeError):
    """Synthetic HTTP-boundary exception with the real protocol type's name."""


class Jar:
    def __init__(self, values):
        self.values = dict(values)

    def copy(self):
        return dict(self.values)


class Connection:
    def __init__(self, account_id):
        self.account_id = account_id
        self.is_closed = False

    async def close(self):
        self.is_closed = True


class DeterministicProtocol:
    """Injected network boundary; every account remains separately identifiable."""
    def __init__(self):
        self.mode = "success"
        self.operations = []
        self.recoveries = []
        self.downloads = []
        self.transports = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.renew_status = None

    def settings(self, account, directory):
        return SimpleNamespace(
            account_id=account["id"], state_dir=directory,
            proxy=account.get("proxy"), impersonate="chrome",
            chat_timeout=4, request_timeout=4, session_mode="thread",
            approval_policy="off", cache_session_seconds=0,
        )

    def transport(self, settings, cookies, on_cookies):
        control = self

        class HTTP:
            def __init__(self):
                self.jar = Jar(cookies)
                self.conn = None
                self.account_id = settings.account_id
                self.on_cookies = on_cookies
                self.last_info = {"status": "assigned", "vm_id": "vm-test",
                                  "vm_state": "RUNNING"}

            def rotate(self, values, expires=None):
                self.jar.values = dict(values)
                self.on_cookies(self.jar.copy(), expires or {})

            async def session_info(self):
                if control.renew_status is not None:
                    raise MuseAuthError("muse API HTTP " + str(control.renew_status))
                self.on_cookies(self.jar.copy(), {})
                return dict(self.last_info)

            async def bootstrap(self, *, force=False):
                await self.session_info()
                return SimpleNamespace(vm_id="vm-test")

            async def connect(self):
                self.conn = Connection(settings.account_id)
                return await self.bootstrap(), self.conn

            async def close(self):
                if self.conn is not None:
                    await self.conn.close()

        transport = HTTP()
        control.transports.append(transport)
        return transport

    def service(self, settings, pool):
        control = self

        class Service:
            def __init__(self):
                self.sessions = SimpleNamespace(get=lambda key: SimpleNamespace(
                    session_id="test-session:" + key))

            async def run(self, prompt, *, on_delta=None, session_key_value=None,
                          timeout=None, **kwargs):
                async with pool.connection():
                    control.operations.append({"kind": "chat", "account": settings.account_id,
                        "prompt": prompt, "session_key": session_key_value})
                    control.started.set()
                    if control.mode == "unknown":
                        raise SyntheticOperationError()
                    try:
                        if control.mode == "stream_hold":
                            for _ in range(400):
                                if on_delta:
                                    on_delta("hold")
                                await asyncio.sleep(0.005)
                            while not control.release.is_set():
                                await asyncio.sleep(0.01)
                        else:
                            for text in ("测", "试"):
                                if on_delta:
                                    on_delta(text)
                                await asyncio.sleep(0)
                    except asyncio.CancelledError:
                        control.cancelled.set()
                        raise
                    return SimpleNamespace(text="测试", media=[], session_id="test-session",
                                           message_id="test-message")

            async def run_with_media(self, prompt, *, kind="image", timeout=None,
                                     items=None, session_key_value=None, **kwargs):
                async with pool.connection():
                    control.operations.append({"kind": kind, "account": settings.account_id,
                        "prompt": prompt, "items": copy.deepcopy(items),
                        "session_key": session_key_value})
                    control.started.set()
                    if control.mode == "unknown":
                        raise SyntheticOperationError()
                    if control.mode == "not_sent":
                        raise SyntheticOperationError(delivery="not_sent", code="connect_unavailable",
                                                      phase="connect", retryable=True)
                    if control.mode == "hold":
                        try:
                            while not control.release.is_set():
                                await asyncio.sleep(0.01)
                        except asyncio.CancelledError:
                            control.cancelled.set()
                            raise
                    ref = SimpleNamespace(kind=kind, account_id=settings.account_id,
                        path="test/result.png" if kind == "image" else "test/result.mp4",
                        url=None, mime="image/png" if kind == "image" else "video/mp4",
                        extension=".png" if kind == "image" else ".mp4",
                        identity="fixture:" + settings.account_id)
                    return SimpleNamespace(text="generated", media=[ref],
                                           session_id="test-session", message_id="test-message")

            async def recover_thread(self, session_id, *, prompt, message_id="",
                                     timeout=None, want_media=False):
                async with pool.connection():
                    control.recoveries.append({"account": settings.account_id,
                        "session_id": session_id, "message_id": message_id,
                        "prompt": prompt})
                    ref = SimpleNamespace(kind="image", account_id=settings.account_id,
                        path="test/result.png", url=None, mime="image/png",
                        extension=".png", identity="fixture:" + settings.account_id)
                    return SimpleNamespace(text="recovered", media=[ref] if want_media else [],
                                           session_id=session_id, message_id="recovered-message")

        return Service()

    async def download(self, conn, ref, destination, *, proxy=None,
                       impersonate=None, max_bytes=None, timeout=None, **kwargs):
        if conn.account_id != ref.account_id:
            raise AssertionError("media download changed account")
        self.downloads.append({"account": conn.account_id, "reference": ref.identity,
                               "proxy": proxy})
        if self.mode == "download_failure":
            raise OSError("synthetic transfer failure after generation")
        data = PNG if ref.kind == "image" else VIDEO
        Path(destination).write_bytes(data)
        return {"bytes": len(data), "mime": ref.mime}


class StoreContractTests(unittest.TestCase):
    """Persistence/race contracts that require no third-party dependencies."""
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="muse-store-contract-")
        self.addCleanup(self.directory.cleanup)
        self.path_patch = mock.patch.object(sys, "path", [str(APP_DIR), *sys.path])
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        spec = importlib.util.spec_from_file_location("contract_store_only", APP_DIR / "store.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.cfg = SimpleNamespace(data_dir=self.directory.name,
            accounts_file=str(Path(self.directory.name) / "accounts.json"),
            tasks_file=str(Path(self.directory.name) / "tasks.json"))
        self.store = self.module.Store(self.cfg)

    def test_stale_cookie_rotation_cannot_overwrite_new_login(self):
        first = self.store.add_account({"hatch_sess": "old", "old_only": "x"})
        revision = first["credential_revision"]
        replacement = self.store.update_account(first["id"], cookies={"hatch_sess": "manual-new"})
        applied = self.store.persist_protocol_cookies(first["id"], revision,
            {"hatch_sess": "late-rotation", "old_only": "x"})
        self.assertFalse(applied)
        self.assertGreater(replacement["credential_revision"], revision)
        self.assertEqual(self.store.get_account(first["id"])["cookies"], {"hatch_sess": "manual-new"})
        restarted = self.module.Store(self.cfg)
        self.assertEqual(restarted.get_account(first["id"])["cookies"], {"hatch_sess": "manual-new"})

    def test_rotation_removes_deleted_cookies_and_does_not_reuse_expiry(self):
        account = self.store.add_account({"hatch_sess": "old", "hatch_vml": "deleted"},
            cookies_exp={"hatch_sess": 1900000000, "hatch_vml": 1900001000})
        self.assertTrue(self.store.persist_protocol_cookies(account["id"], account["credential_revision"],
                        {"hatch_sess": "new-session-cookie"}, {}))
        current = self.store.get_account(account["id"])
        self.assertEqual(current["cookies"], {"hatch_sess": "new-session-cookie"})
        self.assertEqual(current["cookies_exp"], {})
        current["cookies"]["hatch_sess"] = "mutated-return-value"
        self.assertEqual(self.store.get_account(account["id"])["cookies"]["hatch_sess"], "new-session-cookie")

    def test_restart_recovers_unknown_operation_without_creating_a_new_submission(self):
        account = self.store.add_account({"hatch_sess": "session"})
        task = self.store.create_task("image", "one request")
        self.store.begin_protocol_operation("request-1", account["id"], "image", "one request", task["id"])
        self.store.update_protocol_operation("request-1", state="running", delivery="accepted", session_id="session-1")
        restarted = self.module.Store(self.cfg)
        recovered = restarted.recover_interrupted_protocol_operations()
        self.assertEqual(recovered, ["request-1"])
        self.assertEqual(restarted.get_protocol_operation("request-1")["state"], "unknown")
        self.assertEqual(restarted.get_account(account["id"])["protocol_blocked"], "request-1")
        self.assertEqual(len(restarted.protocol_operations), 1)
        self.assertEqual(len(restarted.tasks), 1)
        self.assertEqual(restarted.recover_interrupted_protocol_operations(), [])

    def test_public_operation_status_does_not_leak_prompt_or_signed_media(self):
        account = self.store.add_account({"hatch_sess": "session-secret"})
        self.store.begin_protocol_operation("request-1", account["id"], "image", "private-prompt")
        self.store.update_protocol_operation("request-1", media=[{"url": "https://example.invalid/?token=secret"}])
        public = json.dumps(self.store.list_protocol_operations())
        self.assertNotIn("private-prompt", public)
        self.assertNotIn("token=secret", public)
        self.assertNotIn("session-secret", public)


class AppContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.import_directory = tempfile.TemporaryDirectory(prefix="muse-contract-import-")
        cls.path_patch = mock.patch.object(sys, "path", [str(APP_DIR), str(PROTOCOL_DIR), *sys.path])
        cls.path_patch.start()
        cls.env_patch = mock.patch.dict(os.environ, {
            "MUSE2API_HOME": cls.import_directory.name,
            "MUSE2API_PROFILE_ROOT": str(Path(cls.import_directory.name) / "profiles"),
            "MUSE2API_KEY": KEY, "MUSE_BACKEND": "protocol",
        })
        cls.env_patch.start()
        try:
            spec = importlib.util.spec_from_file_location("contract_app_under_test", APP_DIR / "app.py")
            cls.module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = cls.module
            spec.loader.exec_module(cls.module)
            import protocol_backend
            cls.backend_module = protocol_backend
        except ImportError as exc:
            cls.env_patch.stop()
            cls.path_patch.stop()
            cls.import_directory.cleanup()
            raise unittest.SkipTest("real ASGI import dependency is unavailable: " + str(exc))

    @classmethod
    def tearDownClass(cls):
        cls.env_patch.stop()
        cls.path_patch.stop()
        cls.import_directory.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="muse-contract-")
        from config import Config
        self.cfg = Config(base_dir=self.directory.name, api_key=KEY,
            profile_root=str(Path(self.directory.name) / "profiles"),
            max_concurrency=2, chat_timeout=4, image_timeout=4, video_timeout=4)
        self.cfg.backend = "protocol"
        self.cfg.ensure_dirs()
        self.protocol = DeterministicProtocol()
        self.store = self.module.Store(self.cfg)
        self.accounts = [self.store.add_account({"hatch_sess": "synthetic-" + name}, name)
                         for name in ("account-a", "account-b")]
        self.backend = self.backend_module.ProtocolBackend(self.store, self.cfg,
            settings_factory=self.protocol.settings, transport_factory=self.protocol.transport,
            service_factory=self.protocol.service, media_downloader=self.protocol.download)
        self.module.CFG, self.module.store, self.module.engine = self.cfg, self.store, self.backend
        self.module.GEN_LOCK = self.module.ConcurrencyLock(2)
        self.module.ACCOUNT_POOL = self.module.AccountPoolManager(self.store)
        self.headers = {"authorization": "Bearer " + KEY}

    def tearDown(self):
        self.protocol.release.set()
        try:
            self.backend.stop()
        finally:
            self.directory.cleanup()

    def request(self, method, target, *, payload=None, body=None, headers=None, **kwargs):
        return asyncio.run(asgi_request(self.module.app, method, target, payload=payload,
            body=body, headers={**self.headers, **(headers or {})}, **kwargs))

    def wait_task(self, task_id, kind="image"):
        target = "/v1/images/tasks/" + task_id if kind == "image" else "/v1/videos/" + task_id
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.request("GET", target)
            self.assertEqual(response.status_code, 200, response.text)
            task = response.json()
            if task["status"] not in ("queued", "processing", "running"):
                return task
            time.sleep(0.01)
        self.fail("task failed to reach a terminal/unknown state")

    def test_auth_error_and_account_redaction_are_preserved(self):
        denied = self.request("POST", "/v1/chat/completions",
            payload={"messages": [{"role": "user", "content": "hello"}]},
            headers={"authorization": "Bearer incorrect"})
        self.assertEqual(denied.status_code, 401)
        self.assertIn("error", denied.json())
        public = self.request("GET", "/admin/accounts")
        self.assertEqual(public.status_code, 200)
        self.assertNotIn("synthetic-account", public.text)

    def test_chat_sync_and_stream_keep_openai_envelopes(self):
        request = {"model": "muse-spark", "messages": [{"role": "user", "content": "hello"}]}
        result = self.request("POST", "/v1/chat/completions", payload=request)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["object"], "chat.completion")
        self.assertEqual(result.json()["choices"][0]["message"]["content"], "测试")
        stream = self.request("POST", "/v1/chat/completions", payload={**request, "stream": True})
        self.assertEqual(stream.status_code, 200, stream.text)
        packets = [json.loads(line[6:]) for line in stream.text.splitlines()
                   if line.startswith("data: ") and line != "data: [DONE]"]
        text = "".join(p["choices"][0].get("delta", {}).get("content", "")
                       for p in packets if p.get("choices"))
        self.assertEqual(text, "测试")
        self.assertIn("data: [DONE]", stream.text)
        self.assertEqual(len(self.protocol.operations), 2)
        self.assertNotEqual(self.protocol.operations[0]["session_key"],
                            self.protocol.operations[1]["session_key"])

    def test_responses_sync_and_stream_keep_event_contract(self):
        payload = {"model": "muse-spark", "instructions": "be concise", "input": "hello"}
        result = self.request("POST", "/v1/responses", payload=payload)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "completed")
        self.assertEqual(result.json()["output"][0]["content"][0]["text"], "测试")
        stream = self.request("POST", "/v1/responses", payload={**payload, "stream": True})
        self.assertIn("event: response.output_text.delta", stream.text)
        self.assertIn("event: response.completed", stream.text)
        self.assertNotIn("event: response.failed", stream.text)
        self.assertIn("be concise", self.protocol.operations[0]["prompt"])

    def test_responses_unknown_failure_exposes_receipt_and_never_completes(self):
        self.protocol.mode = "unknown"
        stream = self.request("POST", "/v1/responses", payload={"input": "once", "stream": True})
        self.assertEqual(stream.status_code, 200, stream.text)
        events = [json.loads(line[6:]) for line in stream.text.splitlines()
                  if line.startswith("data: ")]
        failed = [event for event in events if event.get("type") == "response.failed"]
        self.assertEqual(len(failed), 1, stream.text)
        operation = failed[0]["response"]["error"]["operation"]
        self.assertEqual(operation["delivery"], "unknown")
        self.assertFalse(operation["retryable"])
        self.assertTrue(operation["request_id"])
        self.assertIsNotNone(self.store.get_protocol_operation(operation["request_id"]))
        self.assertNotIn("event: response.completed", stream.text)
        self.assertEqual(len(self.protocol.operations), 1)

    def test_renew_401_invalidates_account_but_403_preserves_health(self):
        aid = self.accounts[0]["id"]
        self.store.update_account(self.accounts[1]["id"], enabled=False)
        for status, expected_health, code in ((401, False, "session_unauthorized"),
                                               (403, True, "upstream_access_denied")):
            with self.subTest(http_status=status):
                self.store.mark(aid, True, "previously confirmed session")
                self.protocol.renew_status = status
                result = self.request("POST", "/v1/images/generations", payload={"prompt": "renew first"})
                self.assertEqual(result.status_code, 502, result.text)
                self.assertEqual(result.json()["error"]["operation"]["code"], code)
                self.assertIs(self.store.get_account(aid)["ok"], expected_health)
        self.assertEqual(self.protocol.operations, [])

    def test_image_generation_delivers_original_media_route_and_pins_account(self):
        result = self.request("POST", "/v1/images/generations",
            payload={"prompt": "a test image", "response_format": "url"})
        self.assertEqual(result.status_code, 200, result.text)
        entry = result.json()["data"][0]
        self.assertEqual(entry["bytes"], len(PNG))
        image = self.request("GET", entry["url"])
        self.assertEqual(image.content, PNG)
        self.assertEqual(self.protocol.operations[0]["account"], self.protocol.downloads[0]["account"])
        self.assertEqual(len(self.protocol.operations), 1)

    def test_repeated_multipart_image_fields_keep_reference_order(self):
        boundary = "muse-contract-boundary"
        body = bytearray()
        body.extend(("--" + boundary + '\r\nContent-Disposition: form-data; name="prompt"\r\n\r\nedit\r\n').encode())
        for index, raw in enumerate((PNG, PNG2)):
            body.extend(("--" + boundary + f'\r\nContent-Disposition: form-data; name="image"; filename="{index}.png"\r\nContent-Type: image/png\r\n\r\n').encode())
            body.extend(raw + b"\r\n")
        body.extend(("--" + boundary + "--\r\n").encode())
        result = self.request("POST", "/v1/images/edits", body=bytes(body),
            headers={"content-type": "multipart/form-data; boundary=" + boundary})
        self.assertEqual(result.status_code, 200, result.text)
        images = [item for item in self.protocol.operations[0]["items"] if item["type"] == "image"]
        self.assertEqual([base64.b64decode(item["data_base64"]) for item in images], [PNG, PNG2])

    def test_unsafe_or_ambiguous_reference_inputs_fail_before_protocol_submission(self):
        for payload in (
            {"prompt": "edit", "images": ["http://127.0.0.1/private.png"]},
            {"prompt": "edit", "images": ["data:image/png;base64," + base64.b64encode(PNG).decode()],
             "image": "data:image/png;base64," + base64.b64encode(PNG2).decode()},
        ):
            with self.subTest(payload_keys=list(payload)):
                result = self.request("POST", "/v1/images/generations", payload=payload)
                self.assertIn(result.status_code, (400, 422), result.text)
        self.assertEqual(self.protocol.operations, [])

    def test_async_image_idempotency_and_polling_do_not_regenerate(self):
        payload = {"prompt": "one image"}
        created = self.request("POST", "/v1/images/tasks", payload=payload,
                               headers={"idempotency-key": "contract-once"})
        self.assertEqual(created.status_code, 202, created.text)
        task_id = created.json()["id"]
        done = self.wait_task(task_id)
        self.assertEqual(done["status"], "completed", done)
        repeated = self.request("POST", "/v1/images/tasks", payload=payload,
                                headers={"idempotency-key": "contract-once"})
        self.assertEqual(repeated.json()["id"], task_id)
        self.wait_task(task_id)
        self.assertEqual(len(self.protocol.operations), 1)
        with open(self.cfg.tasks_file, encoding="utf-8") as saved:
            self.assertEqual(json.load(saved)[task_id]["status"], "completed")

    def test_video_job_polling_keeps_result_shape_and_one_submission(self):
        created = self.request("POST", "/v1/videos", payload={"prompt": "video", "duration": 5})
        self.assertEqual(created.status_code, 200, created.text)
        done = self.wait_task(created.json()["id"], "video")
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["result"]["bytes"], len(VIDEO))
        self.assertEqual(self.request("GET", done["result"]["url"]).content, VIDEO)
        self.wait_task(created.json()["id"], "video")
        self.assertEqual(len(self.protocol.operations), 1)

    def test_unknown_submission_is_not_replayed_on_another_account(self):
        self.protocol.mode = "unknown"
        created = self.request("POST", "/v1/images/tasks", payload={"prompt": "do once"})
        self.assertEqual(created.status_code, 202, created.text)
        done = self.wait_task(created.json()["id"])
        self.assertEqual(done["status"], "unknown", done)
        self.assertEqual(len(self.protocol.operations), 1,
                         "an ambiguous upstream submission must never rotate and regenerate")

    def test_not_sent_failure_preserves_its_receipt_without_automatic_resubmission(self):
        self.protocol.mode = "not_sent"
        created = self.request("POST", "/v1/images/tasks", payload={"prompt": "connect once"})
        failed = self.wait_task(created.json()["id"])
        self.assertEqual(failed["status"], "failed", failed)
        self.assertEqual(failed["protocol_error"]["code"], "connect_unavailable")
        self.assertEqual(failed["protocol_error"]["delivery"], "not_sent")
        self.assertTrue(failed["protocol_error"]["retryable"])
        self.assertEqual(failed["operation_id"], created.json()["id"])
        self.assertEqual(len(self.protocol.operations), 1)

    def test_download_failure_is_distinct_from_generation_failure(self):
        self.protocol.mode = "download_failure"
        created = self.request("POST", "/v1/images/tasks", payload={"prompt": "already generated"})
        done = self.wait_task(created.json()["id"])
        self.assertEqual(done["status"], "delivery_failed", done)
        self.assertEqual(len(self.protocol.operations), 1)
        self.assertEqual(len(self.protocol.downloads), 1)

    def test_unknown_task_recovery_queries_the_same_session_without_resubmission(self):
        self.protocol.mode = "unknown"
        created = self.request("POST", "/v1/images/tasks",
            payload={"prompt": "recover original", "response_format": "b64_json"})
        failed = self.wait_task(created.json()["id"])
        self.assertEqual(failed["status"], "unknown", failed)
        operation = self.store.get_protocol_operation(failed["operation_id"])
        self.assertEqual(self.store.get_account(operation["account_id"])["protocol_blocked"],
                         operation["id"])
        self.protocol.mode = "success"
        recovered = self.request("POST", "/admin/protocol/operations/" + operation["id"] + "/recover",
                                 payload={"timeout": 3})
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertFalse(recovered.json()["resubmitted"])
        done = self.wait_task(created.json()["id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(base64.b64decode(done["data"][0]["b64_json"]), PNG)
        self.assertEqual(len(self.protocol.operations), 1)
        self.assertEqual(len(self.protocol.recoveries), 1)
        self.assertEqual(self.protocol.recoveries[0]["session_id"], operation["session_id"])
        self.assertEqual(self.protocol.recoveries[0]["account"], operation["account_id"])
        self.assertFalse(self.store.get_account(operation["account_id"]).get("protocol_blocked"))

    def test_download_recovery_reuses_stored_media_and_original_account(self):
        self.protocol.mode = "download_failure"
        created = self.request("POST", "/v1/images/tasks", payload={"prompt": "recover transfer"})
        failed = self.wait_task(created.json()["id"])
        self.assertEqual(failed["status"], "delivery_failed", failed)
        self.protocol.mode = "success"
        recovered = self.request("POST", "/admin/protocol/operations/" + failed["operation_id"] + "/recover",
                                 payload={"timeout": 3})
        self.assertEqual(recovered.status_code, 200, recovered.text)
        done = self.wait_task(created.json()["id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(self.request("GET", done["data"][0]["url"]).content, PNG)
        self.assertEqual(len(self.protocol.operations), 1)
        self.assertEqual(self.protocol.recoveries, [])
        self.assertEqual(len(self.protocol.downloads), 2)
        self.assertEqual({download["account"] for download in self.protocol.downloads},
                         {self.protocol.operations[0]["account"]})

    def test_cookie_observations_use_the_last_header_for_each_name(self):
        observe = self.backend_module.cookie_observations
        with mock.patch.object(self.backend_module.time, "time", return_value=1000):
            expires, resets, deleted = observe([
                "hatch_sess=; Max-Age=0; Path=/",
                "hatch_sess=rotated; Max-Age=60; Path=/",
            ])
            self.assertEqual(expires, {"hatch_sess": 1060})
            self.assertEqual((resets, deleted), (set(), set()))
            expires, resets, deleted = observe([
                "hatch_sess=persistent; Max-Age=60; Path=/",
                "hatch_sess=session-only; Path=/",
            ])
            self.assertEqual((expires, resets, deleted), ({}, {"hatch_sess"}, set()))
            expires, resets, deleted = observe([
                "hatch_sess=persistent; Max-Age=60; Path=/",
                "hatch_sess=; Max-Age=0; Path=/",
            ])
            self.assertEqual((expires, resets, deleted), ({}, set(), {"hatch_sess"}))

    def test_backend_capabilities_quota_cache_and_update_guard(self):
        status = self.request("GET", "/admin/backend")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["backend"], "protocol")
        self.assertFalse(status.json()["quota_supported"])
        aid = self.accounts[0]["id"]
        cached = {"found": True, "weekly_used_pct": 42, "checked_at": 1000}
        self.store.update_account(aid, quota=cached)
        quota = self.request("POST", "/admin/accounts/" + aid + "/quota")
        self.assertEqual(quota.status_code, 502, quota.text)
        self.assertEqual(quota.json()["detail"]["operation"]["code"], "quota_unsupported")
        self.assertEqual(self.store.get_account(aid)["quota"], cached)
        with mock.patch.object(self.module, "_git", side_effect=AssertionError("unexpected Git invocation")), \
             mock.patch("requests.get", side_effect=AssertionError("unexpected network request")):
            updates = self.request("GET", "/admin/update/check")
            self.assertTrue(updates.json()["disabled"])
            upgrade = self.request("POST", "/admin/update/upgrade", payload={"restart": False})
            self.assertEqual(upgrade.status_code, 409, upgrade.text)

    def test_cookie_reimport_does_not_race_an_active_account(self):
        self.protocol.mode = "hold"
        created = self.request("POST", "/v1/images/tasks", payload={"prompt": "hold"})
        self.assertTrue(self.protocol.started.wait(2))
        account_id = self.protocol.operations[0]["account"]
        update = self.request("POST", "/admin/accounts/" + account_id + "/cookies",
            payload={"cookies": {"hatch_sess": "new-session"}})
        self.assertEqual(update.status_code, 409, update.text)
        deletion = self.request("DELETE", "/admin/accounts/" + account_id)
        self.assertEqual(deletion.status_code, 409, deletion.text)
        self.assertIsNotNone(self.store.get_account(account_id))
        self.protocol.release.set()
        done = self.wait_task(created.json()["id"])
        self.assertEqual(done["status"], "completed", done)
        update = self.request("POST", "/admin/accounts/" + account_id + "/cookies",
            payload={"cookies": {"hatch_sess": "new-session"}})
        self.assertEqual(update.status_code, 200, update.text)
        self.assertEqual(self.store.get_account(account_id)["cookies"]["hatch_sess"], "new-session")

    def test_stream_disconnect_stops_worker_and_releases_local_lease(self):
        self.protocol.mode = "stream_hold"
        response = self.request("POST", "/v1/chat/completions",
            payload={"messages": [{"role": "user", "content": "stream"}], "stream": True},
            disconnect_when=lambda part: b'"content": "ho"' in part or b'"content":"ho"' in part
                                        or b'"hold"' in part,
            timeout=6)
        self.assertEqual(response.status_code, 200)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not self.module.ACCOUNT_POOL.busy_accounts and not self.module.GEN_LOCK.locked():
                break
            time.sleep(0.01)
        self.assertFalse(self.module.ACCOUNT_POOL.busy_accounts, "disconnected client retained an account lease")
        self.assertFalse(self.module.GEN_LOCK.locked(), "disconnected client retained a global slot")
        self.assertTrue(self.protocol.cancelled.wait(1), "upstream local waiting was not cancelled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
