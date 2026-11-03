#!/usr/bin/env python3
"""Exercise installed protocol code over real native curl loopback I/O.

The HTTP and WebSocket peers are synthetic and bind only to 127.0.0.1. This
does not contact Muse, read account exports, or consume generation budget.
WebSocket tests use an explicit identity-cipher boundary; they do not claim
to validate the production TLS/Noise authentication handshake.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class HTTPPeer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), HTTPHandler)
        self.calls = Counter()
        self.sleeping = False
        self.cookie_round = 0


class HTTPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        route = urlsplit(self.path).path
        self.server.calls[(self.command, route)] += 1
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        headers = []
        status, body = 200, {}
        if route == "/api/session":
            self.server.cookie_round += 1
            if self.server.cookie_round == 1:
                headers = [
                    ("Set-Cookie", "hatch_sess=deleted; Path=/; Max-Age=0"),
                    ("Set-Cookie", "hatch_sess=synthetic-rotated; Path=/; Max-Age=3600"),
                    ("Set-Cookie", "temporary=present; Path=/; Max-Age=3600"),
                ]
            elif self.server.cookie_round == 2:
                headers = [
                    ("Set-Cookie", "hatch_sess=synthetic-session; Path=/"),
                    ("Set-Cookie", "temporary=; Path=/; Max-Age=0"),
                ]
            body = {"status": "assigned", "vm_id": "synthetic-native-vm",
                    "vm_state": "STOPPED" if self.server.sleeping else "RUNNING",
                    "endpoint_url": "https://synthetic.invalid"}
        elif route == "/api/hatch/vm/wake":
            self.server.sleeping = False
            body = {"ok": True}
        elif route == "/api/hatch/token":
            body = {"token": "synthetic-admission-token"}
        elif route == "/api/hatch/noise-notary-token":
            body = {"notaryToken": "synthetic-notary-token"}
        elif route.startswith("/status/"):
            status = int(route.rsplit("/", 1)[1])
            body = {"synthetic_status": status}
        else:
            status, body = 404, {"error": "unknown synthetic route"}
        payload = json.dumps(body).encode()
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class IdentityCipher:
    """Explicit test-only Noise boundary, not a cryptographic implementation."""

    def encrypt_with_ad(self, _ad, value):
        return value

    def decrypt_with_ad(self, _ad, value):
        return value


class WSPeer:
    def __init__(self):
        self.calls = Counter()
        self.frames = 0
        self.bytes_sent = 0
        self.body = b"synthetic-complete-media" * 4

    async def handle(self, ws):
        from muse2api import wire
        assembler = wire.NoiseReassembler()
        async for message in ws:
            self.frames += 1
            envelope = assembler.feed(bytes(message))
            if envelope is None:
                continue
            values = {number: value for number, _, value in wire.fields(envelope)}
            frame = wire.decode_service_frame(bytes(values[2]))
            if frame.kind != "request":
                continue
            request = {number: value for number, _, value in wire.fields(frame.payload)}
            method, path = bytes(request[1]).decode(), bytes(request[2]).decode()
            self.calls[(method, path)] += 1
            response = wire.f_varint(1, 200) + wire.f_bool(4, False)
            frame_bytes = wire.f_varint(1, frame.stream_id) + wire.f_bytes(3, response)
            await self.send(ws, wire.f_bytes(1, frame_bytes))
            if path.endswith("slow.bin"):
                chunks, complete = [b"partial"], False
            elif path.endswith("oversize.bin"):
                chunks, complete = [b"x" * 128], True
            else:
                chunks, complete = [self.body[:13], self.body[13:]], True
            for chunk in chunks:
                frame_bytes = wire.encode_service_frame_body(frame.stream_id, chunk)
                await self.send(ws, wire.f_bytes(1, frame_bytes))
            if complete:
                frame_bytes = wire.encode_service_frame_body(frame.stream_id, b"", True)
                await self.send(ws, wire.f_bytes(1, frame_bytes))

    async def send(self, ws, envelope):
        from muse2api.wire import split_noise_payload
        for chunk in split_noise_payload(envelope):
            self.bytes_sent += len(chunk)
            await ws.send(chunk)


@asynccontextmanager
async def native_connection(peer):
    from curl_cffi.requests import AsyncSession
    from muse2api.gateway import HatchConnection
    from websockets.asyncio.server import serve
    async with serve(peer.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with AsyncSession(impersonate="chrome", trust_env=False) as client:
            ws = await client.ws_connect(f"ws://127.0.0.1:{port}/native-test", timeout=3)
            connection = HatchConnection(ws, IdentityCipher(), IdentityCipher())
            try:
                yield connection
            finally:
                await connection.close()


async def checks(directory, report):
    # Import only after selecting a fresh application home. Config's .env
    # loader therefore cannot read the repository's private trial settings.
    os.environ["MUSE2API_HOME"] = str(directory / "application")
    os.environ["MUSE2API_PROFILE_ROOT"] = str(directory / "profiles")
    os.environ["MUSE2API_KEY"] = "synthetic-native-only"
    sys.path.insert(0, str(ROOT))
    import muse2api
    from muse2api import http as protocol_http
    from muse2api.config import Settings
    from muse2api.gateway import ProtocolOperationError
    from muse2api.media import MediaRef, MediaTooLargeError, download_media_to
    from config import Config
    from protocol_backend import BackendOperationError, _default_transport
    from store import Store

    package = Path(muse2api.__file__).resolve().parent
    manifest = json.loads((ROOT / "protocol-dependency/manifest.json").read_text())
    for relative, expected in manifest["output_files"].items():
        if relative.startswith("muse2api/"):
            actual = hashlib.sha256((package / relative.split("/", 1)[1]).read_bytes()).hexdigest()
            require(actual == expected, "installed protocol differs from reviewed patch: " + relative)
    require((package / "protocol.json").is_file(), "installed protocol.json is missing")
    report["installed_protocol"] = str(package)
    report["versions"] = {name: importlib.metadata.version(name) for name in
                          ("curl_cffi", "cryptography", "muse2api-http", "websockets")}

    async def case(name, action):
        started = time.monotonic()
        result = {"case_id": name, "level": "native curl I/O; synthetic loopback peer"}
        try:
            details = await action()
            result.update(status="PASS", **(details or {}))
        except Exception as exc:
            result.update(status="FAIL", error_type=type(exc).__name__, error=str(exc))
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["cases"].append(result)

    peer = HTTPPeer()
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    original_base = protocol_http.BASE
    protocol_http.BASE = f"http://127.0.0.1:{peer.server_port}"
    cfg = Config(base_dir=str(directory / "application"), api_key="synthetic-native-only")
    store = Store(cfg)
    account = store.add_account({"hatch_sess": "synthetic-initial"})
    revision = account["credential_revision"]

    def persist(values, expiries, resets):
        if not store.persist_protocol_cookies(account["id"], revision, values, expiries, resets):
            raise BackendOperationError("synthetic old runtime rejected", code="credential_revision_changed")

    settings = Settings(state_dir=directory / "protocol", cookies_file=str(directory / "unused"),
                        request_timeout=2, http_version="1.1", impersonate="chrome", proxy=None)
    transport = _default_transport(settings, account["cookies"], persist)
    # Keep loopback testing independent of a user's HTTP_PROXY/HTTPS_PROXY.
    transport.client._wrapped.trust_env = False
    try:
        async def cookie_roundtrip():
            await transport.session_info()
            first = store.get_account(account["id"])
            require(first["cookies"]["hatch_sess"] == "synthetic-rotated", "ordered Set-Cookie lost final value")
            require(first["cookies_exp"]["hatch_sess"] > time.time(), "actual expiry was not persisted")
            await transport.session_info()
            second = Store(cfg).get_account(account["id"])
            require(second["cookies"]["hatch_sess"] == "synthetic-session", "session cookie did not persist")
            require("temporary" not in second["cookies"], "deleted cookie remains in Store")
            require("hatch_sess" not in second["cookies_exp"], "old expiry survived session reset")
            return {"set_cookie_order": "deletion then replacement", "expiry_persisted": True,
                    "session_expiry_reset": True, "cookie_deletion": True, "store_reloaded": True}
        await case("N1_HTTP_COOKIE_STORE", cookie_roundtrip)

        async def status_contract():
            caught = {}
            for code in (401, 403, 503):
                try:
                    await transport.request_json("GET", f"/status/{code}")
                except Exception as exc:
                    caught[code] = type(exc).__name__
            require(caught == {401: "MuseAuthError", 403: "MuseAuthError", 503: "MuseUpstreamError"},
                    "HTTP health exception classification differs")
            return {"exceptions": caught}
        await case("N2_HTTP_STATUS_CONTRACT", status_contract)

        async def wake_bootstrap():
            peer.sleeping = True
            session = await transport.bootstrap(force=True)
            require(session.vm_id == "synthetic-native-vm", "bootstrap switched synthetic VM")
            require(peer.calls[("POST", "/api/hatch/vm/wake")] == 1, "wake submission count differs")
            return {"wake_posts": 1, "synthetic_vm_sleep_to_running": True,
                    "real_vm_wakeup_tested": False}
        await case("N3_HTTP_VM_WAKE_SYNTHETIC", wake_bootstrap)
    finally:
        await transport.close()
        protocol_http.BASE = original_base
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=3)
    report["http_calls"] = [{"method": method, "path": path, "count": count}
                            for (method, path), count in sorted(peer.calls.items())]

    ws_peer = WSPeer()
    async with native_connection(ws_peer) as connection:
        async def complete_download():
            destination = directory / "media" / "complete.bin"
            size = await download_media_to(connection, MediaRef("file", path="complete.bin"),
                                           destination, max_bytes=1024, timeout=3)
            require(destination.read_bytes() == ws_peer.body, "native WS protocol body differs")
            require(size == len(ws_peer.body), "native download byte count differs")
            require(not connection._streams, "successful native stream was not dropped")
            return {"bytes": size, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                    "protocol_module_used": True, "noise_handshake_tested": False}
        await case("N4_WS_PROTOCOL_MEDIA", complete_download)

        async def oversized_download():
            destination = directory / "media" / "oversize.bin"
            destination.write_bytes(b"previous-complete-file")
            try:
                await download_media_to(connection, MediaRef("file", path="oversize.bin"),
                                        destination, max_bytes=32, timeout=3)
            except MediaTooLargeError:
                pass
            else:
                raise AssertionError("native stream byte limit was not enforced")
            require(destination.read_bytes() == b"previous-complete-file", "oversize replaced complete file")
            require(not list(destination.parent.glob(".download-*")), "oversize temporary file leaked")
            require(not connection._streams, "oversize native stream leaked")
            require(ws_peer.calls[("GET", "/fs/raw/oversize.bin")] == 1, "oversize download retried")
            return {"limit_bytes": 32, "read_requests": 1, "previous_file_preserved": True,
                    "temporary_files": 0, "remaining_streams": 0}
        await case("N5_WS_MEDIA_LIMIT", oversized_download)

        async def timed_download():
            destination = directory / "media" / "slow.bin"
            started = time.monotonic()
            try:
                await download_media_to(connection, MediaRef("file", path="slow.bin"),
                                        destination, max_bytes=1024, timeout=0.25)
            except ProtocolOperationError as exc:
                require(exc.phase == "download" and exc.delivery == "accepted", "timeout metadata differs")
            else:
                raise AssertionError("unfinished native stream was returned as complete")
            require(time.monotonic() - started < 2, "native download timeout cleanup was unbounded")
            require(not destination.exists(), "partial native result was published")
            require(not list(destination.parent.glob(".download-*")), "timed download temporary file leaked")
            require(not connection._streams, "timed native stream leaked")
            return {"timeout_seconds": 0.25, "partial_published": False,
                    "temporary_files": 0, "remaining_streams": 0}
        await case("N6_WS_MEDIA_TIMEOUT", timed_download)
    report["websocket_calls"] = [{"method": method, "path": path, "count": count}
                                 for (method, path), count in sorted(ws_peer.calls.items())]
    report["websocket_frames_received_by_peer"] = ws_peer.frames
    report["generation_submissions"] = sum(count for (method, path), count in
                                          list(peer.calls.items()) + list(ws_peer.calls.items())
                                          if method == "POST" and "chat" in path)
    require(report["generation_submissions"] == 0, "native harness submitted generation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = {"status": "BLOCKED", "scope": "installed protocol + native loopback HTTP/WebSocket",
              "live_native_transport_tested": False, "live_muse_tested": False,
              "noise_handshake_tested": False, "real_generation_calls": {"text": 0, "image": 0, "video": 0},
              "cases": []}
    try:
        with tempfile.TemporaryDirectory(prefix="muse-native-acceptance-") as temporary:
            asyncio.run(checks(Path(temporary), report))
        report["live_native_transport_tested"] = any(case["status"] == "PASS" for case in report["cases"])
        report["status"] = "PASS" if len(report["cases"]) == 6 and all(
            case["status"] == "PASS" for case in report["cases"]) else "FAIL"
    except (ImportError, OSError) as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    except Exception as exc:
        report.update(status="FAIL", error=type(exc).__name__ + ": " + str(exc))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
