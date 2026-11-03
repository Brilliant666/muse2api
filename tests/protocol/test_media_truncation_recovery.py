"""Controlled cross-layer truncation: installed SDK disk I/O, synthetic gateway.

No sockets, Muse accounts, approvals, real generations, or existing data are used.
Run this file in the acceptance venv so muse2api.media is the installed package,
not a global protocol-suite import substitute.
"""
from __future__ import annotations

from collections import Counter
import hashlib
from importlib.metadata import distribution, version
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from store import Store
from protocol_backend import BackendOperationError, ProtocolBackend, _default_media_downloader
from muse2api import media as native_media
from muse2api.media import MediaRef


def complete_png():
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload)))
    rows = (b"\x00" + b"\xff\x00\x00" * 4) * 4
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


PNG = complete_png()


class GatewayBoundary:
    def __init__(self):
        self.affected_account = None
        self.interrupt = True
        self.submissions = Counter()
        self.connections = []
        self.requests = []
        self.partial_writes = []
        self.real_temporary_handles = []
        self.closed_iterators = 0
        self.recovery_queries = 0

    def settings(self, account, directory):
        return SimpleNamespace(account_id=account["id"], state_dir=directory,
                               impersonate="chrome", proxy=None)

    def transport(self, settings, cookies, on_cookies):
        boundary = self

        class HTTP:
            jar = SimpleNamespace(copy=lambda: dict(cookies))

            async def connect(self):
                conn = Connection(settings.account_id)
                boundary.connections.append(conn)
                return SimpleNamespace(vm_id="synthetic-vm-" + settings.account_id), conn

            async def close(self):
                pass

        class Connection:
            is_closed = False

            def __init__(self, account_id):
                self.account_id = account_id

            async def close(self):
                self.is_closed = True

            async def iter_bytes(self, method, path, **kwargs):
                boundary.requests.append((self.account_id, method, path))
                try:
                    if method != "GET" or not path.endswith("/generated/original.png"):
                        raise AssertionError("download must only read the original file")
                    if self.account_id != boundary.affected_account:
                        raise AssertionError("media moved to a different account")
                    if self.is_closed:
                        raise ConnectionResetError("synthetic gateway remains disconnected")
                    if boundary.interrupt:
                        prefix = PNG[:len(PNG) // 2]
                        yield prefix
                        # Resumption proves the real SDK consumer handled the chunk.
                        # Inspect its actual NamedTemporaryFile after flushing the
                        # observation buffer; no downloader implementation is replaced.
                        handle = boundary.real_temporary_handles[-1]
                        handle.flush()
                        received = Path(handle.name).read_bytes()
                        if received != prefix:
                            raise AssertionError("SDK did not write the real partial prefix")
                        boundary.partial_writes.append({"bytes": len(received),
                            "sha256": hashlib.sha256(received).hexdigest()})
                        self.is_closed = True
                        raise ConnectionResetError("synthetic disconnect after partial media")
                    yield PNG[:len(PNG) // 2]
                    yield PNG[len(PNG) // 2:]
                finally:
                    boundary.closed_iterators += 1

        return HTTP()

    def service(self, settings, pool):
        boundary = self

        class Service:
            sessions = SimpleNamespace(get=lambda key: SimpleNamespace(session_id="synthetic-session-" + key))

            async def run_with_media(self, prompt, **kwargs):
                async with pool.connection():
                    boundary.submissions[settings.account_id] += 1
                    return SimpleNamespace(text="synthetic completed image", message_id="synthetic-original-message",
                        media=[MediaRef(kind="image", path="generated/original.png", mime="image/png", byte_len=len(PNG))])

            async def run(self, prompt, on_delta, **kwargs):
                async with pool.connection():
                    boundary.submissions[settings.account_id] += 1
                    on_delta("synthetic healthy account reply")
                    return SimpleNamespace(text="synthetic healthy account reply", message_id="synthetic-healthy-message")

            async def recover_thread(self, *args, **kwargs):
                boundary.recovery_queries += 1
                raise AssertionError("saved media recovery must not poll or resubmit the model")

        return Service()


class MediaTruncationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="muse-phase2-media-truncation-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cfg = SimpleNamespace(data_dir=str(self.root), media_dir=str(self.root / "media"),
            accounts_file=str(self.root / "accounts.json"), tasks_file=str(self.root / "tasks.json"),
            protocol_operations_file=str(self.root / "protocol_operations.json"), chat_timeout=5,
            protocol_shutdown_timeout=2, protocol_media_max_bytes=1024 * 1024)
        self.store = Store(self.cfg)
        self.account_a = self.store.add_account({"hatch_sess": "synthetic-A-no-upstream"})
        self.account_b = self.store.add_account({"hatch_sess": "synthetic-B-no-upstream"})
        self.boundary = GatewayBoundary()
        self.boundary.affected_account = self.account_a["id"]
        self.backend = ProtocolBackend(self.store, self.cfg, settings_factory=self.boundary.settings,
            transport_factory=self.boundary.transport, service_factory=self.boundary.service)
        self.addCleanup(self.backend.stop)

    def test_partial_native_sdk_write_becomes_delivery_failed_then_recovers_original_only(self):
        installed = Path(distribution("muse2api-http").locate_file("muse2api/media.py")).resolve()
        self.assertEqual(Path(native_media.__file__).resolve(), installed)
        self.assertIs(self.backend.media_downloader, _default_media_downloader)
        original_tempfile = native_media.tempfile.NamedTemporaryFile
        original_download = native_media.download_media_to

        def observe_actual_tempfile(*args, **kwargs):
            handle = original_tempfile(*args, **kwargs)
            self.boundary.real_temporary_handles.append(handle)
            return handle

        with patch.object(native_media.tempfile, "NamedTemporaryFile", side_effect=observe_actual_tempfile), \
                patch.object(native_media, "download_media_to", wraps=original_download) as sdk:
            with self.assertRaises(BackendOperationError) as raised:
                self.backend.generate({}, "synthetic image", account_id=self.account_a["id"], timeout=5,
                                      operation_id="synthetic-truncated-original")
            error = raised.exception
            operation = self.store.get_protocol_operation(error.request_id)
            self.assertEqual(error.code, "media_delivery_failed")
            self.assertEqual(operation["state"], "delivery_failed")
            self.assertEqual(operation["delivery"], "completed")
            self.assertFalse(error.retryable)
            self.assertEqual(sdk.await_count, 1)
            self.assertEqual(len(self.boundary.partial_writes), 1)
            self.assertEqual(self.boundary.partial_writes[0]["bytes"], len(PNG) // 2)
            self.assertEqual(list(Path(self.cfg.media_dir).iterdir()), [], "no pending/temp/public partial media")
            self.assertEqual(self.backend.diagnostics()["active_operations"], 0)
            self.assertEqual(self.boundary.submissions[self.account_a["id"]], 1)
            initial_download_requests = len(self.boundary.requests)
            self.assertEqual(initial_download_requests, 2, "media route plus same-file fs fallback")

            self.assertEqual(self.backend.chat({}, "healthy B", account_id=self.account_b["id"], timeout=5),
                             "synthetic healthy account reply")
            self.assertFalse(self.store.get_account(self.account_b["id"]).get("protocol_blocked"))
            operation_ids = set(self.store.protocol_operations)
            submissions = dict(self.boundary.submissions)
            identity = {key: operation[key] for key in ("account_id", "session_id", "message_id", "vm_id")}
            self.boundary.interrupt = False
            result = self.backend.recover_operation(error.request_id, timeout=5)
            recovered = self.store.get_protocol_operation(error.request_id)
            self.assertEqual(recovered["state"], "completed")
            self.assertEqual({key: recovered[key] for key in identity}, identity)
            self.assertEqual(set(self.store.protocol_operations), operation_ids)
            self.assertEqual(dict(self.boundary.submissions), submissions)
            self.assertEqual(self.boundary.recovery_queries, 0)
            self.assertEqual(sdk.await_count, 2)
            self.assertEqual(len(self.boundary.requests) - initial_download_requests, 1)
            self.assertEqual(self.boundary.closed_iterators, 3)
            self.assertEqual(Path(result["path"]).read_bytes(), PNG)
            self.assertEqual(result["operation_id"], error.request_id)
            self.assertEqual((result["w"], result["h"]), (4, 4))
            self.assertEqual(list(Path(self.cfg.media_dir).iterdir()), [Path(result["path"])])
            self.assertEqual(self.backend.diagnostics()["active_operations"], 0)
            for account_id, method, path in self.boundary.requests:
                self.assertEqual(account_id, self.account_a["id"])
                self.assertEqual(method, "GET")
                self.assertTrue(path.endswith("/generated/original.png"))

        print("TRUNCATION_EVIDENCE=" + json.dumps({"status": "PASS", "level": "controlled gateway iterator and model; installed SDK file I/O",
            "real_generation_calls": 0, "native_socket_io": False, "noise_handshake": False,
            "installed_media_sha256": hashlib.sha256(installed.read_bytes()).hexdigest(),
            "protocol_package_version": version("muse2api-http"), "partial_bytes_written": len(PNG) // 2,
            "full_file_bytes": len(PNG), "full_file_sha256": hashlib.sha256(PNG).hexdigest(),
            "failed_state": "delivery_failed", "partial_media_published": False, "healthy_B_completed": True,
            "synthetic_submissions_A": submissions[self.account_a["id"]],
            "synthetic_submissions_B": submissions[self.account_b["id"]], "recovery_generation_submissions": 0,
            "operations_before_recovery": len(operation_ids), "operations_after_recovery": len(self.store.protocol_operations),
            "same_account_session_message_vm": True, "initial_same_file_get_attempts": initial_download_requests,
            "recovery_original_file_gets": 1, "history_or_model_recovery_queries": 0,
            "temporary_files_after_failure": 0, "closed_iterators": self.boundary.closed_iterators}))


if __name__ == "__main__":
    unittest.main()
