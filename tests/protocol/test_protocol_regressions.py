"""Protocol regressions using actual modules and synthetic network peers.

Only curl_cffi's unavailable native transport/import surface is substituted.
Python protocol, wire codec, websocket and cryptography modules are real. No
upstream request or account credential is used. Run directly with Python 3.11+.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from _paths import ROOT, PROTOCOL_DIR, require_protocol_source
require_protocol_source()
sys.path.insert(0, str(PROTOCOL_DIR))

CURL_STUB = importlib.util.find_spec("curl_cffi") is None
if CURL_STUB:
    curl = ModuleType("curl_cffi")
    curl.__path__ = []
    requests = ModuleType("curl_cffi.requests")
    requests.__path__ = []
    impersonate = ModuleType("curl_cffi.requests.impersonate")
    impersonate.resolve_latest_browser_type = lambda target: "firefox147" if "firefox" in target else "chrome152"
    class UnavailableCurl:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("native curl transport is outside this offline test")
    requests.AsyncSession = curl.AsyncSession = UnavailableCurl
    curl.requests = requests
    sys.modules.update({"curl_cffi": curl, "curl_cffi.requests": requests,
                        "curl_cffi.requests.impersonate": impersonate})

from muse2api.chat import ChatService, ChatSession, SessionStore  # noqa: E402
from muse2api.gateway import (  # noqa: E402
    GatewayError, GatewaySession, HatchConnection, ProtocolCancelledError, ProtocolOperationError,
    RpcError, RpcResponse, _Stream,
)
from muse2api.filesystem import MuseFilesystem  # noqa: E402
from muse2api.media import (  # noqa: E402
    MediaRef, MediaTooLargeError, download_media, download_media_to,
)
from muse2api.protocol import ProtocolConfig  # noqa: E402
from muse2api.wire import ApplicationResponse, DecodedServiceFrame, Header  # noqa: E402


class IdentityCipher:
    """A wire test double; Noise cryptography itself is not reimplemented."""
    def encrypt_with_ad(self, _ad, data):
        return data
    def decrypt_with_ad(self, _ad, data):
        return data


class FakeWebSocket:
    def __init__(self):
        self.closed = asyncio.Event()
        self.sent = []
        self.on_send = None
    def __aiter__(self):
        return self
    async def __anext__(self):
        await self.closed.wait()
        raise StopAsyncIteration
    async def send(self, data):
        self.sent.append(data)
        if self.on_send is not None:
            await self.on_send(data)
    async def close(self):
        self.closed.set()


def response(stream, *, status=200, chunks=(), headers=(), complete=True):
    stream.feed(DecodedServiceFrame(stream.id, "response",
        ApplicationResponse(status, list(headers), b"", False)))
    for chunk in chunks:
        stream.feed(DecodedServiceFrame(stream.id, "body_chunk", (chunk, False)))
    if complete:
        stream.feed(DecodedServiceFrame(stream.id, "body_chunk", (b"", True)))


def chat_history(text="partial", *, active=True, terminal=False, extra=(), empty_latest=False):
    events = [
        {"event_name": "message.user", "message_id": "user-1", "text": "hello"},
        {"event_name": "message.assistant", "message_id": "assistant-1", "payload": {
            "text": text, "metadata": {"streaming": active},
            "status": "in_progress" if active else "completed",
        }},
        *extra,
    ]
    if empty_latest:
        events.append({"event_name": "message.assistant", "payload": {
            "text": "", "metadata": {"streaming": True}}})
    if terminal:
        events.append({"event_name": "turn.completed", "payload": {}})
    return {"chat_events": events}


class HistoryPeer:
    def __init__(self, history):
        self.history = history
        self.reads = 0
        self.submits = 0
        self.is_closed = False
    async def ack(self, *_args, **_kwargs):
        self.submits += 1
        return {"message_id": "user-1"}
    async def request(self, *_args, **_kwargs):
        self.reads += 1
        payload = self.history(self.reads) if callable(self.history) else self.history
        return RpcResponse(200, [], json.dumps(payload).encode())


def chat_service(peer):
    svc = object.__new__(ChatService)
    svc.settings = SimpleNamespace(timezone="UTC", chat_timeout=1, session_mode="thread")
    svc.protocol = ProtocolConfig.load()
    svc.approval_policy = "off"
    @asynccontextmanager
    async def leased():
        yield SimpleNamespace(name="only-account"), None, peer
    svc._connection = leased
    async def register(*_args, **_kwargs):
        pass
    svc._register_node = register
    svc._register_capabilities = register
    return svc


class ProtocolRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.connections = []
        original_connect = socket.socket.connect
        def guarded_connect(sock, address):
            if sock.family in {socket.AF_INET, socket.AF_INET6}:
                raise AssertionError("network is disabled in protocol regression tests")
            return original_connect(sock, address)
        self.network = patch.object(socket.socket, "connect", guarded_connect)
        self.network.start()
        self.addCleanup(self.network.stop)

    async def asyncTearDown(self):
        for conn in self.connections:
            await conn.close()

    def connection(self):
        ws = FakeWebSocket()
        conn = HatchConnection(ws, IdentityCipher(), IdentityCipher())
        self.connections.append(conn)
        return conn

    async def test_stream_disconnect_is_error_not_successful_eof(self):
        conn = self.connection()
        async def on_send(_data):
            if 1 in conn._streams:
                conn._streams[1]._finish("connection closed")
        conn.ws.on_send = on_send
        with self.assertRaises(ProtocolOperationError) as caught:
            _ = [item async for item in conn.stream("POST", "/chat/stream", {}, timeout=1)]
        self.assertEqual(caught.exception.code, "connection_lost")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(conn._streams, {})

    async def test_stream_http_error_remains_error(self):
        conn = self.connection()
        async def on_send(_data):
            if 1 in conn._streams:
                response(conn._streams[1], status=429, chunks=[b'{"error":"busy"}'])
        conn.ws.on_send = on_send
        with self.assertRaises(RpcError) as caught:
            _ = [item async for item in conn.stream("POST", "/chat/stream", {}, timeout=1)]
        self.assertEqual(caught.exception.status, 429)

    async def test_timeout_includes_send_and_retires_partial_write(self):
        conn = self.connection()
        async def delayed(_data):
            await asyncio.sleep(5)
        conn.ws.on_send = delayed
        started = time.monotonic()
        with self.assertRaises(ProtocolOperationError) as caught:
            await conn.request("POST", "/chat/stream", {}, timeout=0.02)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(caught.exception.delivery, "unknown")
        self.assertTrue(conn.is_closed)
        self.assertEqual(conn._streams, {})

    async def test_timeout_waiting_write_lock_is_not_sent(self):
        conn = self.connection()
        await conn._send_lock.acquire()
        try:
            with self.assertRaises(ProtocolOperationError) as caught:
                await conn.request("POST", "/chat/stream", {}, timeout=0.01)
            self.assertEqual(caught.exception.delivery, "not_sent")
            self.assertFalse(conn.ws.sent)
            self.assertFalse(conn.is_closed)
        finally:
            conn._send_lock.release()

    async def test_cancel_preserves_cancellation_and_unknown_delivery(self):
        conn = self.connection()
        entered = asyncio.Event()
        async def delayed(_data):
            entered.set()
            await asyncio.Event().wait()
        conn.ws.on_send = delayed
        task = asyncio.create_task(conn.request("POST", "/chat/stream", {}, timeout=10))
        await entered.wait()
        task.cancel()
        with self.assertRaises(ProtocolCancelledError) as caught:
            await task
        self.assertIsInstance(caught.exception, asyncio.CancelledError)
        self.assertEqual(caught.exception.delivery, "unknown")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(conn._streams, {})

    async def test_active_partial_text_at_timeout_cannot_succeed(self):
        peer = HistoryPeer(chat_history())
        svc = chat_service(peer)
        with self.assertRaises(ProtocolOperationError) as caught:
            await svc._run_thread("hello", ChatSession("session-A", "node-A"), on_delta=None, timeout=0.03)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(caught.exception.delivery, "accepted")
        self.assertEqual(caught.exception.session_id, "session-A")
        self.assertEqual(peer.submits, 1)

    async def test_completed_text_returns_same_stream_and_final_text(self):
        peer = HistoryPeer(chat_history("complete reply", active=False, terminal=True))
        svc = chat_service(peer)
        deltas = []
        result = await svc._run_thread("hello", ChatSession("session-A", "node-A"),
            on_delta=deltas.append, timeout=1)
        self.assertEqual(result.text, "complete reply")
        self.assertEqual("".join(deltas), result.text)
        self.assertEqual(result.session_id, "session-A")

    async def test_active_tool_blocks_refusal_completion(self):
        running = {"event_name": "tool.progress", "payload": {"id": "render", "status": "running"}}
        peer = HistoryPeer(chat_history("cannot generate yet", active=False, extra=[running]))
        svc = chat_service(peer)
        with self.assertRaises(ProtocolOperationError) as caught:
            await svc._poll_thread_reply(peer, "media-A", message_id="user-1", prompt="hello",
                on_delta=None, timeout=0.03, want_media=True, refusal_markers=("cannot generate",))
        self.assertEqual(caught.exception.code, "timeout")

    async def test_terminal_refusal_still_completes_promptly(self):
        peer = HistoryPeer(chat_history("cannot generate", active=False, terminal=True))
        svc = chat_service(peer)
        text, _, _, media = await svc._poll_thread_reply(peer, "media-A", message_id="user-1", prompt="hello",
            on_delta=None, timeout=1, want_media=True, refusal_markers=("cannot generate",))
        self.assertEqual(text, "cannot generate")
        self.assertEqual(media, [])
        self.assertEqual(peer.reads, 1)

    async def test_false_only_assistant_metadata_completes_media_after_quiet(self):
        artifact = {"event_name": "delta.presentation", "payload": {
            "kind": "image", "images": [{"path": "workspace/out.png", "mime": "image/png"}]}}
        body = chat_history("image ready", active=False, extra=[artifact])
        del body["chat_events"][1]["payload"]["status"]
        peer = HistoryPeer(body)
        svc = chat_service(peer)
        with patch("muse2api.chat.TEXT_SETTLE_SECONDS", 0):
            text, _, _, media = await svc._poll_thread_reply(peer, "media-A", message_id="user-1",
                prompt="hello", on_delta=None, timeout=2, want_media=True)
        self.assertEqual(text, "image ready")
        self.assertEqual([ref.path for ref in media], ["workspace/out.png"])

    async def test_false_only_assistant_does_not_complete_running_tool(self):
        running = {"event_name": "tool.progress", "payload": {"id": "render", "status": "running"}}
        body = chat_history("cannot generate yet", active=False, extra=[running])
        del body["chat_events"][1]["payload"]["status"]
        peer = HistoryPeer(body)
        svc = chat_service(peer)
        with self.assertRaises(ProtocolOperationError) as caught:
            await svc._poll_thread_reply(peer, "media-A", message_id="user-1", prompt="hello",
                on_delta=None, timeout=0.03, want_media=True, refusal_markers=("cannot generate",))
        self.assertEqual(caught.exception.code, "timeout")

    async def test_absent_assistant_lifecycle_is_not_false(self):
        artifact = {"event_name": "delta.presentation", "payload": {
            "kind": "image", "images": [{"path": "workspace/out.png"}]}}
        body = chat_history("ready", active=False, extra=[artifact])
        del body["chat_events"][1]["payload"]["status"]
        del body["chat_events"][1]["payload"]["metadata"]
        peer = HistoryPeer(body)
        svc = chat_service(peer)
        with self.assertRaises(ProtocolOperationError):
            await svc._poll_thread_reply(peer, "media-A", message_id="user-1", prompt="hello",
                on_delta=None, timeout=0.03, want_media=True)

    async def test_empty_new_assistant_does_not_reuse_old_finished_text(self):
        peer = HistoryPeer(chat_history("old reply", active=False, empty_latest=True))
        svc = chat_service(peer)
        with self.assertRaises(ProtocolOperationError):
            await svc._poll_thread_reply(peer, "media-A", message_id="user-1", prompt="hello",
                on_delta=None, timeout=0.03, want_media=True)

    async def test_media_with_terminal_turn_returns_actual_artifact(self):
        artifact = {"event_name": "delta.presentation", "payload": {
            "kind": "image", "images": [{"path": "workspace/out.png", "mime": "image/png"}]}}
        peer = HistoryPeer(chat_history("done", active=False, terminal=True, extra=[artifact]))
        svc = chat_service(peer)
        _, _, _, media = await svc._poll_thread_reply(peer, "media-A", message_id="user-1", prompt="hello",
            on_delta=None, timeout=1, want_media=True)
        self.assertEqual([m.path for m in media], ["workspace/out.png"])

    async def test_recovery_reads_existing_thread_without_submission(self):
        peer = HistoryPeer(chat_history("recovered", active=False, terminal=True))
        svc = chat_service(peer)
        result = await svc.recover_thread("session-A", prompt="hello", message_id="user-1", timeout=1)
        self.assertEqual(result.text, "recovered")
        self.assertEqual(result.session_id, "session-A")
        self.assertEqual(peer.submits, 0)

    async def test_media_request_uses_preallocated_session_key(self):
        peer = HistoryPeer(chat_history("done", active=False, terminal=True))
        svc = chat_service(peer)
        svc.sessions = SessionStore(self.root / "sessions.json")
        allocated = svc.sessions.get("job-A").session_id
        result = await svc.run_with_media("hello", session_key_value="job-A", timeout=1)
        self.assertEqual(result.session_id, allocated)

    async def test_binary_limit_rejects_before_body_or_queue_storage(self):
        stream = _Stream(1, mode="bytes", max_bytes=8)
        response(stream, chunks=[b"x" * 32])
        self.assertEqual(stream.body, b"")
        self.assertEqual(stream.pending_bytes, 0)
        self.assertEqual(stream.received_bytes, 0)
        item = await stream.next_item()
        self.assertEqual(item[1].code, "response_too_large")

    async def test_declared_length_rejects_before_first_body(self):
        stream = _Stream(1, mode="unary", max_bytes=8)
        response(stream, chunks=[b"x" * 32], headers=[Header("Content-Length", "32")])
        self.assertEqual(stream.body, b"")
        self.assertEqual(stream.received_bytes, 0)
        self.assertEqual(stream.error.code, "response_too_large")

    async def test_binary_pending_bytes_are_released_after_consumption(self):
        stream = _Stream(1, mode="bytes", max_bytes=128, max_pending_bytes=8)
        response(stream, chunks=[b"abcd"], complete=False)
        self.assertEqual(stream.pending_bytes, 4)
        self.assertEqual(await stream.next_item(), b"abcd")
        self.assertEqual(stream.pending_bytes, 0)
        stream.feed(DecodedServiceFrame(1, "body_chunk", (b"12345678", False)))
        self.assertEqual(stream.pending_bytes, 8)
        stream.feed(DecodedServiceFrame(1, "body_chunk", (b"9", False)))
        self.assertEqual(stream.error.code, "consumer_too_slow")
        self.assertEqual(stream.pending_bytes, 0)

    async def test_json_stream_does_not_retain_duplicate_full_body(self):
        stream = _Stream(1)
        response(stream, chunks=[b'{"text":"hello"}\n', b'{"text":"world"}\n'])
        self.assertEqual(stream.body, b"")
        self.assertEqual(await stream.next_item(), {"text": "hello"})
        self.assertEqual(await stream.next_item(), {"text": "world"})

    def download_connection(self, chunks):
        conn = self.connection()
        streams = []
        async def on_send(_data):
            pending = [s for s in conn._streams.values() if not s.responded]
            if pending:
                streams.append(pending[0])
                response(pending[0], chunks=chunks)
        conn.ws.on_send = on_send
        return conn, streams

    async def test_media_streams_to_atomic_file_and_bytes_api_remains_compatible(self):
        conn, streams = self.download_connection([b"abc", b"def"])
        target = self.root / "out.png"
        size = await download_media_to(conn, MediaRef("image", path="out.png"), target, max_bytes=6, timeout=1)
        self.assertEqual(size, 6)
        self.assertEqual(target.read_bytes(), b"abcdef")
        self.assertTrue(all(not stream.body for stream in streams))
        data = await download_media(conn, MediaRef("image", path="out.png"), max_bytes=6, timeout=1)
        self.assertEqual(data, b"abcdef")
        self.assertEqual(list(self.root.glob(".download-*")), [])

    async def test_media_oversize_preserves_existing_file_and_cleans_partial(self):
        conn, streams = self.download_connection([b"x" * 32])
        target = self.root / "out.png"
        target.write_bytes(b"previous-good-output")
        with self.assertRaises(MediaTooLargeError) as caught:
            await download_media_to(conn, MediaRef("image", path="out.png"), target, max_bytes=8, timeout=1)
        self.assertEqual(target.read_bytes(), b"previous-good-output")
        self.assertEqual(list(self.root.glob(".download-*")), [])
        self.assertEqual(streams[0].received_bytes, 0)
        self.assertEqual(caught.exception.delivery, "accepted")
        self.assertFalse(caught.exception.retryable)

    async def test_filesystem_download_streams_and_raw_forwards_range_headers(self):
        conn, streams = self.download_connection([b"abc", b"def"])
        fs = MuseFilesystem(conn)
        target = await fs.download("workspace/file.bin", str(self.root / "file.bin"), max_bytes=6, timeout=1)
        self.assertEqual(target.read_bytes(), b"abcdef")
        self.assertEqual(streams[0].mode, "bytes")
        raw = await fs.raw("workspace/file.bin", byte_range=(0, 5), max_bytes=6, timeout=1)
        self.assertEqual(raw, b"abcdef")
        self.assertTrue(any(b"Range" in frame and b"bytes=0-5" in frame for frame in conn.ws.sent))

    async def test_cancelled_media_keeps_destination_and_marks_download_phase(self):
        conn = self.connection()
        entered = asyncio.Event()
        async def on_send(_data):
            if not entered.is_set():
                stream = next(iter(conn._streams.values()))
                response(stream, chunks=[b"partial"], complete=False)
                entered.set()
        conn.ws.on_send = on_send
        target = self.root / "out.png"
        target.write_bytes(b"old")
        task = asyncio.create_task(download_media_to(conn, MediaRef("image", path="out.png"), target, timeout=10))
        await entered.wait()
        task.cancel()
        with self.assertRaises(ProtocolCancelledError) as caught:
            await task
        self.assertEqual(caught.exception.phase, "download")
        self.assertEqual(caught.exception.delivery, "accepted")
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(list(self.root.glob(".download-*")), [])

    async def test_cancel_during_ack_reset_retires_nonce_state_and_propagates(self):
        conn = self.connection()
        reset_started = asyncio.Event()
        calls = 0
        async def on_send(_data):
            nonlocal calls
            calls += 1
            if calls == 1:
                response(conn._streams[1], chunks=[b'{"message_id":"user-1"}\n'], complete=False)
            else:
                reset_started.set()
                await asyncio.Event().wait()
        conn.ws.on_send = on_send
        task = asyncio.create_task(conn.ack("POST", "/chat/stream", {}, timeout=5))
        await reset_started.wait()
        task.cancel()
        with self.assertRaises(ProtocolCancelledError):
            await task
        self.assertTrue(conn.is_closed)
        self.assertEqual(conn._streams, {})

    async def test_public_close_does_not_swallow_external_cancellation(self):
        conn = self.connection()
        pending = conn._new_stream()
        original_close = conn.ws.close
        entered = asyncio.Event()
        async def delayed_close():
            entered.set()
            await asyncio.Event().wait()
        conn.ws.close = delayed_close
        task = asyncio.create_task(conn.close())
        await entered.wait()
        task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(conn.is_closed)
            self.assertTrue(pending._done)
            self.assertIsInstance(pending.error, GatewayError)
        finally:
            conn.ws.close = original_close

    async def test_cancelled_handshake_cleanup_is_bounded(self):
        class HandshakePeer(FakeWebSocket):
            def __init__(self):
                super().__init__()
                self.receiving = asyncio.Event()
                self.closing = asyncio.Event()
            async def recv(self):
                self.receiving.set()
                await asyncio.Event().wait()
            async def close(self):
                self.closing.set()
                await asyncio.Event().wait()
        peer = HandshakePeer()
        async def connect_peer(*_args, **_kwargs):
            return peer
        client = SimpleNamespace(ws_connect=connect_peer)
        with patch.object(sys.modules["curl_cffi"], "WebSocketRetryStrategy",
                          lambda **_kwargs: object(), create=True):
            task = asyncio.create_task(HatchConnection.connect(
                GatewaySession("fake-vm", "https://gateway.example.test", "synthetic"),
                ws_client=client, timeout=10))
            await asyncio.wait_for(peer.receiving.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertTrue(peer.closing.is_set())

    async def test_external_media_uses_chunks_and_rejects_before_second_write(self):
        class Response:
            is_redirect = False
            status_code = 200
            headers = {}
            closed = False
            @property
            def content(self):
                raise AssertionError("whole-response content must not be read")
            async def aiter_content(self):
                yield b"abcd"
                yield b"efgh"
            async def aclose(self):
                self.closed = True
        remote = Response()
        class Client:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_args):
                pass
            async def get(self, _url, **kwargs):
                self.kwargs = kwargs
                return remote
        client = Client()
        target = self.root / "out.png"
        target.write_bytes(b"existing")
        with patch("curl_cffi.requests.AsyncSession", lambda **_kwargs: client), \
                patch("muse2api.media.assert_public_host", lambda _host: None):
            with self.assertRaises(MediaTooLargeError):
                await download_media_to(None, MediaRef("image", url="https://media.example.test/image"),
                    target, max_bytes=6, timeout=1)
        self.assertEqual(client.kwargs, {"allow_redirects": False, "stream": True})
        self.assertTrue(remote.closed)
        self.assertEqual(target.read_bytes(), b"existing")
        self.assertEqual(list(self.root.glob(".download-*")), [])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProtocolRegressionTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    evidence = {
        "scope": "actual protocol modules; synthetic network peers; no live native-curl transport test",
        "curl_transport_stub": CURL_STUB,
        "tests_run": result.testsRun,
        "failures": len(result.failures), "errors": len(result.errors),
        "successful": result.wasSuccessful(),
        "source_sha256": {name: hashlib.sha256((PROTOCOL_DIR / "muse2api" / name).read_bytes()).hexdigest()
                          for name in ("gateway.py", "chat.py", "media.py", "filesystem.py")},
        "failed_tests": [str(test) for test, _ in result.failures + result.errors],
    }
    print(json.dumps(evidence, indent=2))
    raise SystemExit(0 if result.wasSuccessful() else 1)
