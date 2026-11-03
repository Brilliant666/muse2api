"""Budget and parser checks use only temporary files and owned loopback peers."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("live_protocol_case", ROOT / "tools/live_protocol_case.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@contextmanager
def peer(body, status=200, headers=None):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append((self.path, self.rfile.read(int(self.headers.get("Content-Length", "0")))))
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield runner.Client(f"http://127.0.0.1:{server.server_port}", "synthetic-service-key", 2), calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def sse(items):
    return b"".join(("data: " + (item if isinstance(item, str) else json.dumps(item)) + "\n\n").encode()
                    for item in items)


class LiveCaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="muse-live-runner-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.state = runner.State(self.temp.name)

    def test_intent_is_charged_once_and_external_claim_is_atomic(self):
        self.state.reserve("T5", "synthetic-request-digest", "/v1/chat/completions")
        self.assertEqual(self.state.summary()["consumed"]["text"], 1)
        self.state.claim_reserved("T5")
        with self.assertRaises(runner.CaseError):
            self.state.claim_reserved("T5")
        with self.assertRaises(runner.CaseError):
            self.state.reserve("T5", "different")
        self.assertEqual(self.state.summary()["consumed"]["text"], 1)

    def test_caps_and_fail_closed_lock(self):
        for case in runner.CATEGORIES:
            self.state.reserve(case, "synthetic")
        self.assertEqual(self.state.summary()["consumed"], runner.LIMITS)
        with self.assertRaises(runner.CaseError):
            self.state.reserve("T7", "synthetic")
        (self.state.directory / "ledger.lock").write_text("interrupted-owner")
        with self.assertRaises(runner.CaseError):
            self.state.update("T1", status="PASS")

    def test_http_failure_retains_budget_and_cannot_retry(self):
        with peer(b'{"error":"synthetic failure"}', 503) as (client, calls):
            result = runner.send(self.state, client, "T1", runner.payload("T1"))
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["consumed"]["text"], 1)
            with self.assertRaises(runner.CaseError):
                runner.send(self.state, client, "T1", runner.payload("T1"))
            self.assertEqual(len(calls), 1)

    def test_stream_requires_finish_and_done_and_captures_raw_privately(self):
        content = {"choices": [{"delta": {"content": "private-output"}, "finish_reason": None}]}
        with peer(sse([content])) as (client, calls):
            result = runner.send(self.state, client, "T2", runner.payload("T2"))
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(len(calls), 1)
            self.assertIn("private-output", (self.state.folder("T2") / "response.bin").read_text())
            self.assertNotIn("private-output", json.dumps(result))

    def test_chat_stream_success_summary_has_no_content_or_identifiers(self):
        body = sse([{"id": "private-id", "choices": [{"delta": {"content": "hello"}, "finish_reason": None}]},
                    {"id": "private-id", "choices": [{"delta": {}, "finish_reason": "stop"}]}, "[DONE]"])
        with peer(body) as (client, calls):
            result = runner.send(self.state, client, "T2", runner.payload("T2"))
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["characters"], 5)
            self.assertNotIn("hello", json.dumps(result))
            self.assertNotIn("private-id", json.dumps(result))
            self.assertEqual(len(calls), 1)

    def test_responses_stream_order_terminal_and_exact_delta_reconstruction(self):
        result = {"id": "private-response", "status": "completed", "output": [{"content": [{"type": "output_text", "text": "hello"}]}]}
        items = [{"type": "response.created", "response": {"id": "private-response"}},
                 {"type": "response.output_item.added"}, {"type": "response.content_part.added"},
                 {"type": "response.output_text.delta", "delta": "hello"},
                 {"type": "response.output_text.done", "text": "hello"},
                 {"type": "response.output_item.done"}, {"type": "response.completed", "response": result}]
        self.assertTrue(runner.validate_text("T4", sse(items))["terminal"])
        with self.assertRaises(runner.CaseError):
            runner.validate_text("T4", sse(items[:-1]))
        reordered = [items[0], items[2], items[1], *items[3:]]
        with self.assertRaises(runner.CaseError):
            runner.validate_text("T4", sse(reordered))
        result["output"][0]["content"][0]["text"] = "mismatch"
        with self.assertRaises(runner.CaseError):
            runner.validate_text("T4", sse(items))

    def test_redirect_is_not_followed_and_remote_base_is_rejected(self):
        with peer(b"", 307, {"Location": "/second-generation"}) as (client, calls):
            result = runner.send(self.state, client, "T1", runner.payload("T1"))
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(len(calls), 1)
        with self.assertRaises(runner.CaseError):
            runner.Client("https://example.invalid", "synthetic")

    def test_multipart_keeps_two_distinct_references_in_order(self):
        one, two = Path(self.temp.name) / "one.png", Path(self.temp.name) / "two.png"
        one.write_bytes(b"\x89PNG\r\n\x1a\nred-reference")
        two.write_bytes(b"\x89PNG\r\n\x1a\nblue-reference")
        body, kind = runner.multipart_edit(runner.payload("I2"), [one, two])
        self.assertIn("multipart/form-data", kind)
        self.assertEqual(body.count(b'name="image"'), 2)
        self.assertLess(body.index(b"red-reference"), body.index(b"blue-reference"))
        with self.assertRaises(runner.CaseError):
            runner.multipart_edit(runner.payload("I2"), [one, one])


if __name__ == "__main__":
    unittest.main(verbosity=2)
