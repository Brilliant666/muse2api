"""Acceptance evidence at a synthetic gateway boundary, including OS restart.

The application Store, ProtocolBackend, ChatService and SessionStore are real.
Gateway/bootstrap/media peers are synthetic; no Muse account or native network
transport is used, and no real generation budget is consumed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib


ROOT = Path(__file__).resolve().parents[2]


def _save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _event(root, kind, **fields):
    with (root / "peer-events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": kind, "pid": os.getpid(), **fields}) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _png():
    def chunk(name, body):
        return struct.pack(">I", len(body)) + name + body + struct.pack(">I", zlib.crc32(name + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


def _worker(root, mode):
    """Run one real backend lifetime; called in a fresh interpreter process."""
    root = Path(root)
    _save(root / (mode + "-worker.json"), {"pid": os.getpid()})
    sys.path[:0] = [str(ROOT / "tests" / "protocol"), str(ROOT)]
    from _paths import PROTOCOL_DIR, require_protocol_source
    require_protocol_source()
    sys.path.insert(0, str(PROTOCOL_DIR))
    from config import Config
    from store import Store
    from protocol_backend import BackendOperationError, ProtocolBackend
    from muse2api.cookies import CookieJar
    from muse2api.gateway import GatewaySession, RpcResponse
    from muse2api.protocol import ProtocolConfig

    cfg = Config(base_dir=str(root / "application"), profile_root=str(root / "profiles"),
                 backend="protocol", chat_timeout=120, max_concurrency=1)
    cfg.ensure_dirs()
    store = Store(cfg)
    if mode in {"start", "download-start"}:
        account = store.add_account({"hatch_sess": "synthetic-no-upstream-account"})
    else:
        account = store.accounts[0]
        store.recover_interrupted_protocol_operations()
    aid = account["id"]
    protocol = ProtocolConfig.load()
    vm = "different-vm" if mode == "mismatch" else "original-vm"

    class Peer:
        is_closed = False
        account_id = aid

        async def close(self):
            self.is_closed = True

        async def ack(self, method, path, params, **kwargs):
            assert mode in {"start", "download-start"}, "recovery must never submit"
            assert method == "POST" and path == protocol.route("chat.stream").path
            _event(root, "generation_submission", account_id=aid, vm_id=vm,
                   session_id=params["session_id"], node_id=params["node_id"])
            _save(root / "submission.json", {"account_id": aid, "vm_id": vm, **params})
            return {"message_id": "original-user"}

        async def request(self, method, path, params=None, **kwargs):
            if path in {protocol.route("nodes.register").path,
                        protocol.route("client.register_capabilities").path}:
                _event(root, "registration", account_id=aid, params=params)
                return RpcResponse(200, [], b"{}")
            assert method == "GET" and path == protocol.route("chat.history").path
            submission = json.loads((root / "submission.json").read_text())
            assert params["session_id"] == submission["session_id"]
            assert aid == submission["account_id"] and vm == submission["vm_id"]
            _event(root, "history_query", account_id=aid, vm_id=vm,
                   session_id=params["session_id"])
            terminal = mode != "start"
            events = [
                {"event_name": "message.user", "message_id": "original-user",
                 "payload": {"text": submission["message"]}},
                {"event_name": "message.assistant", "message_id": "original-assistant",
                 "payload": {"text": "recovered original answer" if terminal else "still executing",
                             "metadata": {"streaming": not terminal},
                             "status": "completed" if terminal else "in_progress"}},
            ]
            if mode in {"download-start", "download-recover"}:
                events.append({"event_name": "delta.presentation", "payload": {
                    "kind": "image", "images": [{"path": "workspace/original.png", "mime": "image/png"}]}})
            if terminal:
                events.append({"event_name": "turn.completed", "payload": {}})
            return RpcResponse(200, [], json.dumps({"chat_events": events}).encode())

    class HTTP:
        def __init__(self, cookies, on_cookies):
            self.jar, self.on_cookies = CookieJar(cookies), on_cookies
        async def bootstrap(self, force=False):
            self.on_cookies(self.jar.copy(), {})
            return GatewaySession(vm, "https://synthetic.invalid", "synthetic-token")
        async def connect(self):
            _event(root, "connect", account_id=aid, vm_id=vm)
            return await self.bootstrap(), Peer()
        async def close(self):
            pass

    async def download(conn, ref, destination, **kwargs):
        assert conn.account_id == aid
        assert ref.path == "workspace/original.png"
        _event(root, "media_download", account_id=aid, path=ref.path)
        if mode == "download-start":
            Path(destination).write_bytes(b"partial")
            raise OSError("controlled delivery interruption")
        Path(destination).write_bytes(_png())
        return len(_png())

    backend = ProtocolBackend(store, cfg, transport_factory=lambda settings, cookies, callback: HTTP(cookies, callback),
                              media_downloader=download)
    try:
        if mode == "start":
            for _ in backend.chat_stream({}, "original operation", account_id=aid, timeout=120):
                pass
            raise AssertionError("active synthetic task unexpectedly completed")
        if mode == "download-start":
            try:
                backend.generate({}, "original image", account_id=aid, timeout=10)
            except BackendOperationError as exc:
                assert exc.code == "media_delivery_failed"
                _save(root / "first-result.json", {"code": exc.code, "operation_id": exc.request_id})
                return
            raise AssertionError("controlled download error was not surfaced")
        op = next(iter(store.protocol_operations.values()))
        # Validate the real SDK session/node receipt survives the process boundary.
        from muse2api.chat import SessionStore
        session_files = list((Path(cfg.data_dir) / "protocol").rglob("chat_sessions.json"))
        assert len(session_files) == 1, session_files
        persisted = SessionStore(session_files[0]).get(op["session_key"])
        original = json.loads((root / "submission.json").read_text())
        assert persisted.session_id == original["session_id"]
        assert persisted.node_id == original["node_id"]
        before = store.get_account(aid)
        try:
            result = backend.recover_operation(op["id"], timeout=10)
        except BackendOperationError as exc:
            if mode != "mismatch":
                raise
            _save(root / "recovery-result.json", {"code": exc.code, "operation_id": op["id"],
                "state": store.get_protocol_operation(op["id"])["state"],
                "blocked": store.get_account(aid).get("protocol_blocked"), "pid": os.getpid()})
        else:
            if mode == "mismatch":
                raise AssertionError("changed workspace must not recover the old operation")
            summary = {"operation_id": op["id"], "account_id": aid,
                       "session_id": persisted.session_id, "node_id": persisted.node_id,
                       "vm_id": original["vm_id"], "state": store.get_protocol_operation(op["id"])["state"],
                       "blocked_before": before.get("protocol_blocked"),
                       "blocked_after": store.get_account(aid).get("protocol_blocked"), "pid": os.getpid()}
            if "path" in result:
                payload = Path(result["path"]).read_bytes()
                summary.update(sha256=hashlib.sha256(payload).hexdigest(), size=len(payload))
            else:
                summary["text"] = result["text"]
            _save(root / "recovery-result.json", summary)
    finally:
        backend.stop()


class ProcessRecoveryAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="muse-acceptance-recovery-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = {k: v for k, v in os.environ.items()
                            if not k.startswith(("MUSE_", "MUSE2API_")) and k not in {
                                "PYTHONPATH", "MODEL_API_KEY", "META_API_KEY", "META_MODEL_API_KEY", "IMAGE_GATEWAY_TOKEN"}}
        self.environment.update(MUSE2API_HOME=str(self.root / "application"),
                                MUSE2API_KEY="acceptance-synthetic-service-key", PYTHONDONTWRITEBYTECODE="1")
        if os.environ.get("MUSE_PROTOCOL_SOURCE"):
            self.environment["MUSE_PROTOCOL_SOURCE"] = os.environ["MUSE_PROTOCOL_SOURCE"]

    def child(self, mode):
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(self.root), mode],
                                cwd=self.root, env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)

    def run_child(self, mode):
        process = self.child(mode)
        try:
            stdout, stderr = process.communicate(timeout=25)
        except BaseException:
            process.kill()
            process.communicate()
            raise
        self.assertEqual(process.returncode, 0, stdout + stderr)
        return json.loads((self.root / (mode + "-worker.json")).read_text())["pid"]

    def crash_submitted_child(self):
        process = self.child("start")
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not (self.root / "submission.json").exists():
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    self.fail("child exited before submitting: " + stdout + stderr)
                time.sleep(0.02)
            self.assertTrue((self.root / "submission.json").is_file(), "child never submitted")
            # Windows venv redirectors may create a second Python process.
            # Terminate the actual recorded interpreter, not just its launcher.
            worker_pid = json.loads((self.root / "start-worker.json").read_text())["pid"]
            os.kill(worker_pid, signal.SIGTERM)
            process.communicate(timeout=5)
            self.assertNotEqual(process.returncode, 0)
            return worker_pid
        finally:
            if process.poll() is None:
                worker_receipt = self.root / "start-worker.json"
                if worker_receipt.is_file():
                    try:
                        os.kill(json.loads(worker_receipt.read_text())["pid"], signal.SIGTERM)
                    except OSError:
                        pass
                process.kill()
                process.communicate()

    def events(self, kind):
        return [event for line in (self.root / "peer-events.jsonl").read_text().splitlines()
                if (event := json.loads(line))["kind"] == kind]

    def test_process_kill_then_same_operation_history_recovery_never_resubmits(self):
        first_pid = self.crash_submitted_child()
        second_pid = self.run_child("recover")
        self.assertNotEqual(first_pid, second_pid)
        result = json.loads((self.root / "recovery-result.json").read_text())
        submissions = self.events("generation_submission")
        self.assertEqual(len(submissions), 1)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["text"], "recovered original answer")
        self.assertEqual(result["blocked_before"], result["operation_id"])
        self.assertIsNone(result["blocked_after"])
        for key in ("account_id", "session_id", "node_id", "vm_id"):
            self.assertEqual(result[key], submissions[0][key])
        queries = [event for event in self.events("history_query") if event["pid"] == second_pid]
        self.assertGreaterEqual(len(queries), 1)
        self.assertTrue(all(event["session_id"] == result["session_id"] for event in queries))

    def test_changed_vm_after_process_kill_remains_unknown_without_query_or_resubmit(self):
        self.crash_submitted_child()
        second_pid = self.run_child("mismatch")
        result = json.loads((self.root / "recovery-result.json").read_text())
        self.assertEqual(result["code"], "workspace_changed")
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["blocked"], result["operation_id"])
        self.assertEqual(len(self.events("generation_submission")), 1)
        self.assertFalse([event for event in self.events("history_query") if event["pid"] == second_pid])

    def test_delivery_failure_then_new_process_downloads_original_media_only(self):
        first_pid = self.run_child("download-start")
        second_pid = self.run_child("download-recover")
        self.assertNotEqual(first_pid, second_pid)
        result = json.loads((self.root / "recovery-result.json").read_text())
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["sha256"], hashlib.sha256(_png()).hexdigest())
        self.assertEqual(result["size"], len(_png()))
        self.assertEqual(len(self.events("generation_submission")), 1)
        downloads = self.events("media_download")
        self.assertEqual(len(downloads), 2)
        self.assertEqual(downloads[0]["account_id"], downloads[1]["account_id"])
        self.assertEqual(downloads[0]["path"], downloads[1]["path"])
        self.assertFalse([event for event in self.events("history_query") if event["pid"] == second_pid])
        self.assertFalse(list((self.root / "application" / "data" / "media").glob("*.pending")))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        _worker(sys.argv[2], sys.argv[3])
    else:
        unittest.main(verbosity=2)
