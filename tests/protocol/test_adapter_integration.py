"""Protocol lifecycle/receipt tests using real application Store and engine types.

Network and model boundaries are deterministic doubles. These are not live Muse
authentication, Noise-handshake or image-quality acceptance tests.
"""
import asyncio
import base64
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _paths import ROOT, APP_DIR
sys.path.insert(0, str(APP_DIR))
from config import Config
from store import Store
from protocol_backend import ProtocolBackend, BackendOperationError, cookie_observations, _check_media

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jS1sAAAAASUVORK5CYII=")


class Sessions:
    def get(self, key):
        return SimpleNamespace(session_id="session-" + key)


class Error(RuntimeError):
    code, phase, delivery, retryable = "history_timeout", "history", "unknown", False
    session_id, message_id = None, None


class Boundary:
    def __init__(self):
        self.submissions = 0
        self.downloads = 0
        self.queries = 0
        self.fail_download = False
        self.unknown = False
        self.loops = set()
        self.transports = []

    def settings(self, account, directory):
        return SimpleNamespace(account_id=account["id"], state_dir=directory,
                               impersonate="chrome", proxy=None)

    def transport(self, settings, cookies, on_cookies):
        boundary = self
        class HTTP:
            def __init__(self):
                self.values = dict(cookies)
                self.jar = SimpleNamespace(copy=lambda: dict(self.values))
                self.callback = on_cookies
            async def bootstrap(self, force=False):
                boundary.loops.add(id(asyncio.get_running_loop()))
                self.callback(self.jar.copy(), {})
                return SimpleNamespace(vm_id="same-vm")
            async def session_info(self):
                return {"status": "assigned", "vm_state": "RUNNING", "vm_id": "same-vm"}
            async def connect(self):
                class Connection:
                    is_closed = False
                    account_id = settings.account_id
                    async def close(self):
                        self.is_closed = True
                return await self.bootstrap(), Connection()
            async def close(self):
                return None
        value = HTTP()
        boundary.transports.append(value)
        return value

    def service(self, settings, pool):
        boundary = self
        class Service:
            sessions = Sessions()
            def result(self, session_id):
                ref = SimpleNamespace(kind="image", path="generated/test.png", url=None,
                    mime="image/png", label=None, byte_len=len(PNG),
                    identity="generated/test.png", extension=".png", account_id=settings.account_id)
                return SimpleNamespace(text="completed", message_id="reply-id", session_id=session_id, media=[ref])
            async def run_with_media(self, prompt, *, session_key_value=None, **kwargs):
                async with pool.connection():
                    boundary.submissions += 1
                    if boundary.unknown:
                        raise Error("simulated timeout after submission")
                    return self.result(self.sessions.get(session_key_value).session_id)
            async def recover_thread(self, session_id, **kwargs):
                async with pool.connection():
                    boundary.queries += 1
                    return self.result(session_id)
        return Service()

    async def downloader(self, conn, ref, destination, **kwargs):
        self.downloads += 1
        if conn.account_id != ref.account_id:
            raise AssertionError("switched account during download")
        if self.fail_download:
            raise OSError("simulated disconnected transfer")
        Path(destination).write_bytes(PNG)
        return len(PNG)


class BackendIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cfg = Config(base_dir=self.directory.name, backend="protocol")
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg)
        self.account = self.store.add_account({"hatch_sess": "test-only-session"})
        self.boundary = Boundary()
        self.backend = self.make_backend(self.store)

    def make_backend(self, store):
        return ProtocolBackend(store, self.cfg, settings_factory=self.boundary.settings,
            transport_factory=self.boundary.transport, service_factory=self.boundary.service,
            media_downloader=self.boundary.downloader)

    def tearDown(self):
        self.backend.stop()
        self.directory.cleanup()

    def generate(self):
        return self.backend.generate({}, "one image", account_id=self.account["id"], timeout=5)

    def test_completed_generation_download_failure_is_recovered_without_resubmission(self):
        self.boundary.fail_download = True
        with self.assertRaises(BackendOperationError) as caught:
            self.generate()
        error = caught.exception
        self.assertEqual(error.task_status, "delivery_failed")
        self.assertFalse(error.retryable)
        self.assertEqual(self.boundary.submissions, 1)
        self.boundary.fail_download = False
        recovered = self.backend.recover_operation(error.request_id)
        self.assertEqual(Path(recovered["path"]).read_bytes(), PNG)
        self.assertEqual(self.boundary.submissions, 1)
        self.assertEqual(self.boundary.downloads, 2)
        self.assertEqual(self.boundary.queries, 0)

    def test_unknown_operation_is_quarantined_then_recovered_by_history_only(self):
        self.boundary.unknown = True
        with self.assertRaises(BackendOperationError) as caught:
            self.generate()
        rid = caught.exception.request_id
        self.assertEqual(self.store.get_account(self.account["id"])["protocol_blocked"], rid)
        self.assertIsNone(self.store.pick_account())
        result = self.backend.recover_operation(rid)
        self.assertEqual(result["kind"], "image")
        self.assertEqual(self.boundary.submissions, 1)
        self.assertEqual(self.boundary.queries, 1)
        self.assertNotIn("protocol_blocked", self.store.get_account(self.account["id"]))

    def test_unknown_operation_survives_store_and_backend_restart(self):
        self.boundary.unknown = True
        with self.assertRaises(BackendOperationError) as caught:
            self.generate()
        rid = caught.exception.request_id
        self.backend.stop()
        self.store = Store(self.cfg)
        self.backend = self.make_backend(self.store)
        result = self.backend.recover_operation(rid)
        self.assertEqual(Path(result["path"]).read_bytes(), PNG)
        self.assertEqual(self.boundary.submissions, 1)
        self.assertEqual(self.boundary.queries, 1)

    def test_abandon_releases_quarantine_without_claiming_upstream_cancellation(self):
        self.boundary.unknown = True
        with self.assertRaises(BackendOperationError) as caught:
            self.generate()
        result = self.backend.abandon_operation(caught.exception.request_id)
        self.assertTrue(result["abandoned"])
        self.assertFalse(result["upstream_cancelled"])
        self.assertIsNotNone(self.store.pick_account())
        self.assertEqual(self.boundary.submissions, 1)

    def test_stale_transport_cookie_callback_is_rejected_after_manual_import(self):
        self.backend.renew_account(self.account["id"])
        callback = self.boundary.transports[0].callback
        self.store.update_account(self.account["id"], cookies={"hatch_sess": "new-import"})
        with self.assertRaises(BackendOperationError):
            callback({"hatch_sess": "old-late-response"}, {})
        self.assertEqual(self.store.get_account(self.account["id"])["cookies"]["hatch_sess"], "new-import")

    def test_no_chromium_process_and_generated_name_is_derived_from_media_signature(self):
        with patch("subprocess.Popen", side_effect=AssertionError("no browser allowed")):
            result = self.generate()
        self.assertEqual(Path(result["path"]).suffix, ".png")
        self.assertEqual((result["w"], result["h"]), (1, 1))
        self.assertEqual(len(self.boundary.loops), 1)
        self.assertFalse(self.backend.diagnostics()["browser_running"])

    def test_html_or_wrong_media_type_cannot_be_published_as_an_image(self):
        output = Path(self.cfg.media_dir) / "pretend.png"
        output.write_bytes(b"<!doctype html><html>error</html>")
        with self.assertRaises(BackendOperationError):
            _check_media(output, "image")
        output.write_bytes(PNG)
        with self.assertRaises(BackendOperationError):
            _check_media(output, "video")

    def test_cookie_expiry_deletion_and_session_cookie_resets_are_observations(self):
        with patch("protocol_backend.time.time", return_value=1000):
            expires, resets, deleted = cookie_observations([
                "hatch_sess=new; Max-Age=60; Path=/; Secure; HttpOnly",
                "hatch_vml=; Max-Age=0; Path=/", "session_cookie=same; Secure; Path=/"])
        self.assertEqual(expires, {"hatch_sess": 1060})
        self.assertEqual(deleted, {"hatch_vml"})
        self.assertEqual(resets, {"session_cookie"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
