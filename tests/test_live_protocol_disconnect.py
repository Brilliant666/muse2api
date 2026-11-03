"""T5 tooling against a synthetic loopback SSE peer; never imports an account."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from live_protocol_case import CaseError, Client, State, atomic_json
from live_protocol_disconnect import execute


class Peer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, home, mode):
        super().__init__(("127.0.0.1", 0), Handler)
        self.home, self.mode = home, mode
        self.posts, self.active, self.blocked = 0, 0, None
        self.closed = threading.Event()

    def operation(self, state):
        record = {"id": "synthetic-operation", "account_id": "synthetic-account",
                  "kind": "chat", "state": state, "session_id": "synthetic-session",
                  "session_key": "operation:synthetic-operation", "vm_id": "synthetic-vm"}
        atomic_json(self.home / "data/protocol_operations.json", {record["id"]: record})
        account_directory = hashlib.sha256(b"synthetic-account").hexdigest()[:20]
        atomic_json(self.home / "data/protocol" / account_directory / "chat_sessions.json",
                    {record["session_key"]: {"session_id": record["session_id"],
                                             "node_id": "synthetic-node"}})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/admin/accounts":
            data = {"accounts": [{"id": "synthetic-account", "enabled": True,
                                   "protocol_blocked": self.server.blocked}]}
        elif self.path == "/admin/backend":
            data = {"backend": "protocol", "active_operations": self.server.active}
        else:
            self.send_error(404)
            return
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.posts += 1
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["stream"] is True
        if self.server.mode == "http-error":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"private synthetic error"}')
            return
        self.server.active = 1
        self.server.operation("completed" if self.server.mode == "completed" else "running")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            self.wfile.write(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"partial answer"}}]}\n\n')
            self.wfile.flush()
            if self.server.mode == "completed":
                self.server.active = 0
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
                self.wfile.flush()
                return
            while not self.server.closed.wait(0.02):
                self.wfile.write(b": local heartbeat\n\n")
                self.wfile.flush()
        except (OSError, ConnectionError):
            self.server.operation("unknown")
            self.server.blocked = "synthetic-operation"
            self.server.active = 0


class DisconnectToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="muse-t5-tool-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.home = self.directory / "application"
        self.state = State(self.directory / "live-cases")

    def reserve(self):
        data = {"model": "muse-spark", "stream": True, "timeout": 10,
                "messages": [{"role": "user", "content": "synthetic T5 only"}]}
        self.state.reserve("T5", hashlib.sha256(json.dumps(data).encode()).hexdigest(),
                           "/v1/chat/completions")
        atomic_json(self.state.folder("T5") / "reserved-request.json", data)

    def peer(self, mode):
        peer = Peer(self.home, mode)
        thread = threading.Thread(target=peer.serve_forever, daemon=True)
        thread.start()
        def close():
            peer.closed.set()
            peer.shutdown()
            peer.server_close()
            thread.join(2)
        self.addCleanup(close)
        return peer, Client("http://127.0.0.1:" + str(peer.server_port), "synthetic-service-key", 5)

    def test_reserved_single_submit_disconnects_running_turn_and_preserves_unknown(self):
        self.reserve()
        peer, client = self.peer("running")
        result = execute(self.state, client, self.home, cleanup_timeout=3)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertTrue(result["disconnect_observed"])
        self.assertTrue(result["active_cleanup_observed"])
        self.assertFalse(result["lease_directly_observed"])
        self.assertTrue(result["recovery_required"])
        self.assertEqual(result["consumed"]["text"], 1)
        self.assertEqual(peer.posts, 1)
        private = json.loads((self.state.folder("T5") / "disconnect-receipt.json").read_text())
        self.assertEqual(private["operation_after_disconnect"]["state"], "unknown")
        self.assertEqual(private["session"]["node_id"], "synthetic-node")
        public = json.dumps(result)
        for identifier in ("synthetic-account", "synthetic-operation", "synthetic-session", "synthetic-node"):
            self.assertNotIn(identifier, public)
        with self.assertRaises(CaseError):
            execute(self.state, client, self.home, cleanup_timeout=1)
        self.assertEqual(peer.posts, 1)

    def test_completed_before_running_observation_is_not_observed_not_replayed(self):
        self.reserve()
        peer, client = self.peer("completed")
        result = execute(self.state, client, self.home, cleanup_timeout=1)
        self.assertEqual(result["status"], "NOT_OBSERVED")
        self.assertFalse(result["disconnect_observed"])
        self.assertFalse(result["recovery_required"])
        with self.assertRaises(CaseError):
            execute(self.state, client, self.home, cleanup_timeout=1)
        self.assertEqual(peer.posts, 1)
        self.assertEqual(self.state.summary()["consumed"]["text"], 1)

    def test_missing_intent_never_posts(self):
        peer, client = self.peer("running")
        with self.assertRaises(FileNotFoundError):
            execute(self.state, client, self.home)
        self.assertEqual(peer.posts, 0)
        self.assertEqual(self.state.summary()["consumed"]["text"], 0)

    def test_changed_payload_is_rejected_before_network_or_second_budget_charge(self):
        self.reserve()
        peer, client = self.peer("running")
        path = self.state.folder("T5") / "reserved-request.json"
        data = json.loads(path.read_text())
        data["messages"][0]["content"] = "changed fixture"
        atomic_json(path, data)
        with self.assertRaises(CaseError):
            execute(self.state, client, self.home)
        self.assertEqual(peer.posts, 0)
        self.assertEqual(self.state.summary()["consumed"]["text"], 1)

    def test_http_failure_still_consumes_one_and_never_replays(self):
        self.reserve()
        peer, client = self.peer("http-error")
        result = execute(self.state, client, self.home, cleanup_timeout=1)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["consumed"]["text"], 1)
        self.assertNotIn("private synthetic error", json.dumps(result))
        with self.assertRaises(CaseError):
            execute(self.state, client, self.home)
        self.assertEqual(peer.posts, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
