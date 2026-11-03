"""API -> real backend -> real ChatService with only network peers substituted.

Actual Settings, SessionStore, history parsing, media extraction and streaming
file delivery run together. HTTP/bootstrap and gateway peer responses are
synthetic; native curl, real sockets, authentication and Noise handshakes are
outside this offline integration test.
"""
from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

import test_app_contract as support
# Reuse only the offline import guard. No ChatService method is replaced.
import test_protocol_regressions as protocol_support

from muse2api.chat import ChatService, SessionStore
from muse2api.config import Settings
from muse2api.cookies import CookieJar
from muse2api.gateway import GatewaySession, RpcResponse
from muse2api.protocol import ProtocolConfig


class SyntheticGateway:
    def __init__(self, account_id, kind):
        self.account_id, self.kind = account_id, kind
        self.is_closed = False
        self.protocol = ProtocolConfig.load()
        self.submissions = []
        self.registrations = []
        self.history_reads = []
        self.downloads = []

    async def close(self):
        self.is_closed = True

    async def ack(self, method, path, params, **kwargs):
        assert method == "POST" and path == self.protocol.route("chat.stream").path
        assert params["session_id"] and params["node_id"]
        assert params["node_id"] in [entry["node_id"] for entry in self.registrations
                                     if "node_id" in entry]
        self.submissions.append(copy.deepcopy(params))
        return {"message_id": "composed-user-1"}

    async def request(self, method, path, params=None, **kwargs):
        if path in {self.protocol.route("nodes.register").path,
                    self.protocol.route("client.register_capabilities").path}:
            assert method == "POST"
            self.registrations.append(copy.deepcopy(params))
            return RpcResponse(200, [], b"{}")
        assert method == "GET" and path == self.protocol.route("chat.history").path
        assert len(self.submissions) == 1, "one generated result must have only one submission"
        submitted = self.submissions[0]
        assert params["session_id"] == submitted["session_id"]
        self.history_reads.append(copy.deepcopy(params))
        events = [
            {"event_name": "message.user", "message_id": "composed-user-1",
             "payload": {"text": submitted["message"]}},
            {"event_name": "message.assistant", "message_id": "composed-assistant-1",
             "payload": {"text": "跨层完整回答", "metadata": {"streaming": False},
                         "status": "completed"}},
        ]
        if self.kind == "image":
            events.append({"event_name": "delta.presentation", "payload": {
                "kind": "image", "images": [{"path": "workspace/composed.png", "mime": "image/png"}]}})
        events.append({"event_name": "turn.completed", "payload": {}})
        return RpcResponse(200, [], json.dumps({"chat_events": events}, ensure_ascii=False).encode())

    async def iter_bytes(self, method, path, **kwargs):
        assert method == "GET" and path == "/media/raw/workspace/composed.png"
        assert self.kind == "image" and len(self.submissions) == 1
        self.downloads.append({"connection": self, "account": self.account_id, "path": path})
        for offset in range(0, len(support.PNG), 13):
            yield support.PNG[offset:offset + 13]
            await asyncio.sleep(0)


class ComposedProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.AppContractTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        support.AppContractTests.tearDownClass.__func__(cls)

    def setUp(self):
        from config import Config
        self.directory = tempfile.TemporaryDirectory(prefix="muse-composed-")
        self.cfg = Config(base_dir=self.directory.name, api_key=support.KEY, backend="protocol",
                          max_concurrency=2, chat_timeout=4, image_timeout=4)
        self.cfg.ensure_dirs()
        self.store = self.module.Store(self.cfg)
        self.account = self.store.add_account({"hatch_sess": "composed-synthetic-session"})
        self.kind = "text"
        self.peers = []
        self.settings_seen = []
        self.backend = self.backend_module.ProtocolBackend(self.store, self.cfg,
                                                            transport_factory=self.transport)
        self.module.CFG, self.module.store, self.module.engine = self.cfg, self.store, self.backend
        self.module.GEN_LOCK = self.module.ConcurrencyLock(2)
        self.module.ACCOUNT_POOL = self.module.AccountPoolManager(self.store)
        original_connect = socket.socket.connect
        def offline_connect(sock, address):
            if sock.family in {socket.AF_INET, socket.AF_INET6}:
                # Windows asyncio implements socketpair with a loopback TCP
                # connection. Permit that local plumbing while retaining the
                # external-network guard on the composed tests.
                try:
                    local = ipaddress.ip_address(address[0]).is_loopback
                except ValueError:
                    local = False
                if not local:
                    raise AssertionError("external network is forbidden in composed tests")
            return original_connect(sock, address)
        self.network = mock.patch.object(socket.socket, "connect", offline_connect)
        self.network.start()
        self.environment = mock.patch.dict(os.environ, {"MUSE2API_APPROVAL_POLICY": "off", "MUSE_CHAT_TIMEOUT": "4"})
        self.environment.start()

    def tearDown(self):
        try:
            self.backend.stop()
        finally:
            self.environment.stop()
            self.network.stop()
            self.directory.cleanup()

    def transport(self, settings, cookies, on_cookies):
        self.assertIsInstance(settings, Settings)
        self.settings_seen.append(settings)
        control = self

        class SyntheticHTTP:
            def __init__(self):
                self.jar = CookieJar(cookies)
                self.peer = None

            async def session_info(self):
                on_cookies(self.jar.copy(), {})
                return {"status": "assigned", "vm_id": "composed-vm", "vm_state": "RUNNING"}

            async def bootstrap(self, *, force=False):
                await self.session_info()
                return GatewaySession("composed-vm", "https://synthetic.invalid", "synthetic-token")

            async def connect(self):
                self.peer = SyntheticGateway(control.account["id"], control.kind)
                control.peers.append(self.peer)
                return await self.bootstrap(), self.peer

            async def close(self):
                if self.peer is not None:
                    await self.peer.close()

        return SyntheticHTTP()

    def request(self, method, target, payload=None):
        return asyncio.run(support.asgi_request(self.module.app, method, target, payload=payload,
                         headers={"authorization": "Bearer " + support.KEY}))

    def assert_real_composition(self):
        self.assertEqual(len(self.peers), 1)
        peer = self.peers[0]
        self.assertEqual(len(peer.submissions), 1)
        self.assertEqual(len(peer.history_reads), 1)
        runtime = self.backend._runtimes[self.account["id"]]
        self.assertIsInstance(runtime.settings, Settings)
        self.assertIsInstance(runtime.service, ChatService)
        self.assertIsInstance(runtime.service.sessions, SessionStore)
        self.assertIs(runtime.conn, peer)
        self.assertIs(runtime.service.pool.runtime, runtime)
        saved = json.loads(runtime.service.sessions.path.read_text())
        self.assertEqual(len(saved), 1)
        operation = next(iter(self.store.protocol_operations.values()))
        self.assertEqual(operation["state"], "completed")
        self.assertEqual(operation["session_id"], peer.submissions[0]["session_id"])
        self.assertEqual(saved[operation["session_key"]]["session_id"], operation["session_id"])
        self.assertEqual(peer.submissions[0]["node_id"], saved[operation["session_key"]]["node_id"])
        return peer, operation

    def test_text_api_runs_actual_chat_service_with_persisted_session(self):
        response = self.request("POST", "/v1/chat/completions",
                                {"messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "跨层完整回答")
        peer, operation = self.assert_real_composition()
        self.assertEqual(peer.downloads, [])
        self.assertEqual(operation["result"]["text"], "跨层完整回答")

    def test_image_api_extracts_history_and_downloads_on_the_original_connection(self):
        self.kind = "image"
        response = self.request("POST", "/v1/images/generations", {"prompt": "synthetic render"})
        self.assertEqual(response.status_code, 200, response.text)
        peer, operation = self.assert_real_composition()
        self.assertEqual(operation["media"]["path"], "workspace/composed.png")
        self.assertEqual(len(peer.downloads), 1)
        self.assertIs(peer.downloads[0]["connection"], peer)
        self.assertEqual(peer.downloads[0]["account"], self.account["id"])
        media = self.request("GET", response.json()["data"][0]["url"])
        self.assertEqual(media.content, support.PNG)
        self.assertEqual(response.json()["data"][0]["bytes"], len(support.PNG))
        self.assertFalse(list(Path(self.cfg.media_dir).glob("*.pending")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
