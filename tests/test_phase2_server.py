"""Metrics wrapper checks: no server, real accounts, Store or network is started."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("phase2_server_under_test", ROOT / "tools/phase2_server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class Sink:
    def __init__(self):
        self.rows = []
        self.fail = False

    def emit(self, event, **fields):
        if self.fail:
            raise OSError("synthetic metric failure")
        self.rows.append({"event": event, **fields})

    def stats(self):
        return {"dropped": 0}


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="phase2-metrics-contract-")
        self.addCleanup(self.temp.cleanup)
        self.token = object()
        self.sink = Sink()
        token = self.token

        class Pool:
            def __init__(self):
                self.account = {"id": "synthetic-account", "cookies": {"token": "NEVER_LOG_COOKIE"}}
                self.last = None
                self.error = None

            def acquire(self, *args, **kwargs):
                self.last = args, kwargs
                if self.error:
                    raise self.error
                return self.account

            def release(self, account_id):
                self.last_release = account_id
                return token

        class Capacity:
            limit = 3

            def acquire(self, *args, **kwargs):
                self.last = args, kwargs
                return token

        class HTTP:
            def __init__(self):
                self._client = object()
                self.response = {"token": "NEVER_LOG_AUTH_TOKEN"}
                self.calls = []

            async def request_json(self, method, path, **kwargs):
                self.calls.append((method, path, kwargs))
                return self.response

        class Gateway:
            connect_calls = []
            connection = None

            def __init__(self):
                self._streams = {1: SimpleNamespace(delivery="not_sent", phase="prepare")}
                self._receiver = SimpleNamespace(done=lambda: False)
                self.is_closed = False
                self.calls = []
                self.error = None

            @classmethod
            async def connect(cls, session, **kwargs):
                cls.connect_calls.append((session, kwargs))
                return cls.connection

            async def _send_stream_frames(self, stream_id, **kwargs):
                self.calls.append((stream_id, kwargs))
                self._streams[stream_id].delivery = "unknown"
                if self.error:
                    raise self.error
                self._streams[stream_id].phase = "receive"
                return token

        def write(path, value):
            if isinstance(value, BaseException):
                raise value
            Path(path).write_text(json.dumps(value), "utf8")
            return token

        self.Pool, self.Capacity, self.HTTP, self.Gateway = Pool, Capacity, HTTP, Gateway
        self.store_module = SimpleNamespace(_write=write)
        self.connection, self.http = Gateway(), HTTP()
        Gateway.connection = self.connection
        runtime = SimpleNamespace(account_id="synthetic-account", revision=2,
                                  conn=self.connection, http=self.http)
        self.engine = SimpleNamespace(_runtimes={"synthetic-account": runtime},
            _active={"operation-1": {"id": "operation-1", "account_id": "synthetic-account",
                                     "session_id": "session-1", "kind": "image"}},
            _account_locks={"synthetic-account": object()}, _tasks={object()})
        application = SimpleNamespace(AccountPoolManager=Pool, ConcurrencyLock=Capacity,
                                      engine=self.engine)
        self.instrumentation = server.Instrumentation(application, self.sink,
            {"/chat/stream": "chat.stream", "/chat/subscribe": "chat.subscribe"})
        self.instrumentation.install(Gateway, HTTP, self.store_module)
        self.addCleanup(self.instrumentation.restore)

    def test_store_wrapper_preserves_return_and_original_exception_when_metrics_fail(self):
        path = Path(self.temp.name) / "accounts.json"
        self.assertIs(self.store_module._write(path, {"value": 7}), self.token)
        event = self.sink.rows[-1]
        self.assertEqual(event["category"], "accounts")
        self.assertEqual(event["bytes_after"], path.stat().st_size)
        self.sink.fail = True
        self.assertIs(self.store_module._write(path, {"value": 8}), self.token)
        error = ValueError("original write error")
        with self.assertRaises(ValueError) as caught:
            self.store_module._write(path, error)
        self.assertIs(caught.exception, error)

    def test_pool_capacity_and_release_results_are_unchanged_and_cookie_is_not_logged(self):
        pool, capacity = self.Pool(), self.Capacity()
        self.assertIs(pool.acquire(timeout=1), pool.account)
        self.assertEqual(pool.last, ((), {"timeout": 1}))
        self.assertIs(pool.release("synthetic-account"), self.token)
        self.assertIs(capacity.acquire(timeout=2), self.token)
        self.assertNotIn("NEVER_LOG_COOKIE", json.dumps(self.sink.rows))
        error = RuntimeError("original acquire error")
        pool.error = error
        with self.assertRaises(RuntimeError) as caught:
            pool.acquire()
        self.assertIs(caught.exception, error)

    def test_wire_submission_classes_and_error_identity_without_logging_prompt(self):
        async def run():
            body = json.dumps({"session_id": "session-1", "node_id": "node-1",
                               "message": "NEVER_LOG_PROMPT"}).encode()
            parameters = {"method": "POST", "path": "/chat/stream", "body": body,
                          "json_body": True, "service": "daemon"}
            self.assertIs(await self.connection._send_stream_frames(1, **parameters), self.token)
            self.assertIs(self.connection.calls[-1][1]["body"], body)
            await self.connection._send_stream_frames(1, **{**parameters, "path": "/chat/subscribe"})
            await self.connection._send_stream_frames(1, **{**parameters, "path": "/unknown"})
            started = [r for r in self.sink.rows if r["event"] == "gateway_write_started"]
            self.assertEqual([r["generation_post"] for r in started], [True, False, False])
            self.assertTrue(started[-1]["unclassified_post"])
            self.assertEqual(started[0]["session_id"], "session-1")
            self.assertEqual(started[0]["operation_id"], "operation-1")
            self.assertNotIn("NEVER_LOG_PROMPT", json.dumps(self.sink.rows))
            error = RuntimeError("original wire error")
            self.connection.error = error
            with self.assertRaises(RuntimeError) as caught:
                await self.connection._send_stream_frames(1, **parameters)
            self.assertIs(caught.exception, error)
            self.assertEqual(self.sink.rows[-1]["delivery"], "unknown")
        asyncio.run(run())

    def test_connect_http_and_backend_sample_do_not_add_requests_or_log_tokens(self):
        async def run():
            session = SimpleNamespace(vm_id="vm-fixture", gateway_url="NEVER_LOG_SIGNED_URL")
            self.assertIs(await self.Gateway.connect(session, timeout=3), self.connection)
            self.assertEqual(len(self.Gateway.connect_calls), 1)
            self.assertIs(await self.http.request_json("POST", "/api/hatch/token",
                json_body={"secret": "NEVER_LOG_REQUEST_PARAM"}), self.http.response)
            before = (len(self.Gateway.connect_calls), len(self.http.calls), len(self.connection.calls))
            self.instrumentation.runtime_sample(time.monotonic())
            self.assertEqual(before, (len(self.Gateway.connect_calls), len(self.http.calls), len(self.connection.calls)))
            sample = self.sink.rows[-1]
            self.assertEqual(sample["event"], "backend_sample")
            self.assertEqual(sample["runtime_count"], 1)
            self.assertEqual(sample["active_connection_count"], 1)
            self.assertEqual(sample["active_receiver_count"], 1)
            self.assertEqual(sample["stream_count"], 1)
            rendered = json.dumps(self.sink.rows)
            for secret in ("NEVER_LOG_AUTH_TOKEN", "NEVER_LOG_REQUEST_PARAM", "NEVER_LOG_SIGNED_URL"):
                self.assertNotIn(secret, rendered)
        asyncio.run(run())


class QueueAndProcTests(unittest.TestCase):
    def test_bounded_queue_reports_drops_and_refuses_existing_metrics_file(self):
        with tempfile.TemporaryDirectory(prefix="phase2-metric-queue-") as directory:
            path = Path(directory) / "metrics.jsonl"
            sink = server.MetricsSink(path, capacity=1, start_writer=False)
            sink.emit("one")
            sink.emit("two")
            self.assertEqual(sink.stats()["dropped"], 1)
            final = sink.close()
            self.assertTrue(final["writer_finished"])
            rows = [json.loads(line) for line in path.read_text('utf8').splitlines()]
            self.assertEqual(rows[-1]["event"], "metrics_summary")
            self.assertEqual(rows[-1]["dropped"], 1)
            with self.assertRaises(FileExistsError):
                server.MetricsSink(path)

    def test_proc_socket_counts_filter_other_process_inodes(self):
        with tempfile.TemporaryDirectory(prefix="phase2-proc-fixture-") as directory:
            proc = Path(directory)
            (proc / "self/fd").mkdir(parents=True)
            (proc / "net").mkdir()
            for fd in ("0", "1", "2"):
                (proc / "self/fd" / fd).touch()
            fields = ["S"] + ["0"] * 49
            fields[11], fields[12] = "50", "25"
            (proc / "self/stat").write_text("1 (fixture process) " + " ".join(fields))
            (proc / "self/statm").write_text("10 3 0")
            (proc / "net/tcp").write_text("header\n"
                "0: 0100007F:48B4 00000000:0000 0A 0:0 0:0 0 0 0 111\n"
                "1: 0100007F:C350 0100007F:01BB 01 0:0 0:0 0 0 0 222\n"
                "2: 0100007F:C351 0100007F:01BB 01 0:0 0:0 0 0 0 333\n")
            links = {"0": "socket:[111]", "1": "socket:[222]", "2": "pipe:[444]"}
            with mock.patch.object(server.os, "readlink", side_effect=lambda p: links[p.name]), \
                 mock.patch.object(server.os, "sysconf", create=True,
                                   side_effect=lambda name: 100 if name == "SC_CLK_TCK" else 4096):
                sample = server.linux_process_sample(18612, proc=proc)
            self.assertEqual(sample["tcp_owned_count"], 2)
            self.assertEqual(sample["tcp_owned_excluding_server_port_count"], 1)
            self.assertEqual(sample["fd_count"], 3)
            self.assertEqual(sample["rss_bytes"], 3 * 4096)
            self.assertEqual(sample["cpu_seconds_total"], 0.75)
            self.assertEqual(sample["tcp_owned_by_state"], {"LISTEN": 1, "ESTABLISHED": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
