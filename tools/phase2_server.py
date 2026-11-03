"""Single-worker protocol acceptance server with private, bounded metrics.

Example (separate Linux test data, no production credentials):
  PYTHONPATH=/app MUSE_BACKEND=protocol MUSE2API_HOME=/private/application \
    python /tools/phase2_server.py --metrics /private/L1-metrics.jsonl \
    --host 127.0.0.1 --port 18612 --workers 1

Gateway counters distinguish call entry, local send/flush completion and unknown
writes. They do not prove upstream acceptance. Any dropped metrics invalidate
exact event counts. No sampler sends requests or owns an additional Store.
SIGTERM/SIGINT request graceful shutdown (exit 0) while this wrapper owns their
handlers; repeated SIGINT retains Uvicorn's force-exit behavior. Other signals
are untouched. SIGKILL cannot guarantee a metrics completion record.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import functools
import hashlib
import importlib
import importlib.metadata
import inspect
import itertools
import json
import logging
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import weakref


class MetricsSink:
    def __init__(self, path, capacity=4096, *, start_writer=True):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.file = os.fdopen(descriptor, "w", encoding="utf-8", buffering=1)
        self.queue = queue.Queue(maxsize=capacity)
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.enqueued = self.written = self.dropped = self.writer_errors = 0
        self.thread = None
        if start_writer:
            self.thread = threading.Thread(target=self._drain, name="phase2-metrics-writer", daemon=True)
            self.thread.start()

    def emit(self, event, **fields):
        row = {"event": event, "time_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns(),
               "thread_id": threading.get_ident(), **fields}
        try:
            self.queue.put_nowait(row)
            with self.lock:
                self.enqueued += 1
        except queue.Full:
            with self.lock:
                self.dropped += 1

    def stats(self):
        with self.lock:
            return {"enqueued": self.enqueued, "written": self.written,
                    "dropped": self.dropped, "writer_errors": self.writer_errors,
                    "queued": self.queue.qsize()}

    def _write(self, row):
        try:
            self.file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            with self.lock:
                self.written += 1
        except Exception:
            with self.lock:
                self.writer_errors += 1
                self.dropped += 1

    def _drain(self):
        while not self.stop.is_set() or not self.queue.empty():
            try:
                row = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self._write(row)
        self._write({"event": "metrics_summary", "time_ns": time.time_ns(), **self.stats()})
        try:
            self.file.close()
        except Exception:
            with self.lock:
                self.writer_errors += 1

    def close(self):
        self.stop.set()
        if self.thread is None:
            self._drain()
        else:
            self.thread.join(timeout=5)
        return {**self.stats(), "writer_finished": self.thread is None or not self.thread.is_alive()}


TCP_STATES = {"01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1",
              "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT",
              "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING"}


def linux_process_sample(server_port, *, proc=Path("/proc")):
    """Read only this process' sockets; /proc/net itself is namespace-wide."""
    descriptors = list((proc / "self/fd").iterdir())
    inodes = set()
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
            if target.startswith("socket:[") and target.endswith("]"):
                inodes.add(target[8:-1])
        except OSError:
            pass
    rows, states = [], collections.Counter()
    owned_tcp_count = other_tcp_count = 0
    for family in ("tcp", "tcp6"):
        path = proc / "net" / family
        if not path.exists():
            continue
        for line in path.read_text().splitlines()[1:]:
            values = line.split()
            if len(values) < 10 or values[9] not in inodes:
                continue
            local_port = int(values[1].rsplit(":", 1)[1], 16)
            remote_port = int(values[2].rsplit(":", 1)[1], 16)
            state = TCP_STATES.get(values[3], values[3])
            owned_tcp_count += 1
            other_tcp_count += local_port != server_port
            states[state] += 1
            if len(rows) < 512:
                rows.append({"inode": values[9], "family": family, "state": state,
                             "local_port": local_port, "remote_port": remote_port})
    fields = (proc / "self/stat").read_text().rsplit(")", 1)[1].split()
    ticks = os.sysconf("SC_CLK_TCK")
    pages = int((proc / "self/statm").read_text().split()[1])
    return {"rss_bytes": pages * os.sysconf("SC_PAGESIZE"),
            "cpu_seconds_total": (int(fields[11]) + int(fields[12])) / ticks,
            "fd_count": len(descriptors), "socket_fd_inode_count": len(inodes),
            "tcp_owned_count": owned_tcp_count, "tcp_owned_by_state": dict(states),
            "tcp_owned_excluding_server_port_count": other_tcp_count,
            "tcp_details": rows, "tcp_details_truncated": owned_tcp_count > len(rows),
            "tcp_scope": "FD-owned TCP only; includes HTTP and gateway, not a gateway-only count"}


class Instrumentation:
    def __init__(self, application, sink, routes):
        self.application, self.sink = application, sink
        self.routes = routes  # path -> protocol route name; no dynamic discovery requests
        self.patches = []
        self.connections = weakref.WeakSet()
        self.connection_vm = weakref.WeakKeyDictionary()
        self.calls = itertools.count(1)
        self.hook_errors = 0

    def emit(self, event, **fields):
        try:
            self.sink.emit(event, **fields)
        except Exception:
            self.hook_errors += 1  # metrics must not alter an application result

    def patch(self, owner, name, replacement):
        self.patches.append((owner, name, inspect.getattr_static(owner, name)))
        setattr(owner, name, replacement)

    def restore(self):
        for owner, name, original in reversed(self.patches):
            setattr(owner, name, original)
        self.patches.clear()

    def context(self, connection=None, http=None):
        engine = self.application.engine
        for runtime in list(engine._runtimes.values()):
            if ((connection is not None and runtime.conn is connection) or
                    (http is not None and runtime.http is http)):
                operations = [op for op in list(engine._active.values())
                              if op.get("account_id") == runtime.account_id]
                result = {"account_id": runtime.account_id, "credential_revision": runtime.revision,
                          "operation_ids": [op["id"] for op in operations],
                          "operation_kinds": [op.get("kind") for op in operations]}
                if len(operations) == 1:
                    result.update(operation_id=operations[0]["id"],
                                  operation_session_id=operations[0].get("session_id"))
                return result
        return {"account_id": None, "operation_ids": [], "context_unavailable": True}

    def safe_context(self, **kwargs):
        try:
            return self.context(**kwargs)
        except Exception:
            self.hook_errors += 1
            return {"context_unavailable": True}

    def wire_fields(self, connection, stream_id, method, path, body):
        parsed = urlsplit(path)
        route = self.routes.get(parsed.path)
        generation = method.upper() == "POST" and route == "chat.stream"
        fields = {"connection_id": id(connection), "stream_id": stream_id,
                  "method": method.upper(), "route": route or "unclassified",
                  "path": parsed.path, "generation_post": generation,
                  "unclassified_post": method.upper() == "POST" and route is None,
                  "body_bytes": len(body), "vm_id": self.connection_vm.get(connection),
                  **self.safe_context(connection=connection)}
        identity = parse_qs(parsed.query)
        if body and len(body) <= 65536:
            try:
                decoded = json.loads(body)
                if isinstance(decoded, dict):
                    identity.update({key: [decoded[key]] for key in ("session_id", "node_id") if key in decoded})
            except (ValueError, UnicodeError):
                pass
        fields["body_identity_parse_skipped"] = len(body) > 65536
        for key in ("session_id", "node_id"):
            value = (identity.get(key) or [None])[0]
            if isinstance(value, str):
                fields[key] = value
        return fields

    def install(self, gateway, http, store_module):
        controller = self
        original_write = store_module._write

        @functools.wraps(original_write)
        def write(path, obj):
            try:
                category = {"accounts.json": "accounts", "tasks.json": "tasks",
                            "protocol_operations.json": "operations"}.get(Path(path).name, "other")
            except Exception:
                category = "unavailable"
                controller.hook_errors += 1
            error = None
            started = time.monotonic()
            try:
                return original_write(path, obj)
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                elapsed_ms = (time.monotonic() - started) * 1000
                try:
                    size = os.path.getsize(path)
                except Exception:
                    size = None
                controller.emit("store_write", category=category, bytes_after=size, error_type=error,
                    elapsed_ms=elapsed_ms,
                    durability_scope="Existing _write/replace completion; no added fsync")

        self.patch(store_module, "_write", write)
        pool_class = self.application.AccountPoolManager
        original_acquire, original_release = pool_class.acquire, pool_class.release

        @functools.wraps(original_acquire)
        def acquire(pool, *args, **kwargs):
            started, call_id = time.monotonic(), next(controller.calls)
            requested = args[0] if args else kwargs.get("account_id")
            try:
                account = original_acquire(pool, *args, **kwargs)
            except BaseException as exc:
                controller.emit("account_acquire", call_id=call_id, requested_account_id=requested,
                    acquired=False, elapsed_ms=(time.monotonic() - started) * 1000,
                    error_type=type(exc).__name__)
                raise
            controller.emit("account_acquire", call_id=call_id, requested_account_id=requested,
                account_id=account.get("id") if isinstance(account, dict) else None, acquired=True,
                elapsed_ms=(time.monotonic() - started) * 1000,
                timing_scope="Acquire wall time includes selection and Store write")
            return account

        @functools.wraps(original_release)
        def release(pool, account_id):
            result = original_release(pool, account_id)
            controller.emit("account_release", account_id=account_id)
            return result

        self.patch(pool_class, "acquire", acquire)
        self.patch(pool_class, "release", release)
        capacity_class = self.application.ConcurrencyLock
        original_capacity = capacity_class.acquire

        @functools.wraps(original_capacity)
        def capacity(owner, *args, **kwargs):
            started = time.monotonic()
            try:
                result = original_capacity(owner, *args, **kwargs)
            except BaseException as exc:
                controller.emit("capacity_acquire", acquired=False,
                    elapsed_ms=(time.monotonic() - started) * 1000, error_type=type(exc).__name__)
                raise
            controller.emit("capacity_acquire", acquired=bool(result),
                elapsed_ms=(time.monotonic() - started) * 1000,
                limit=getattr(owner, "limit", None))
            return result

        self.patch(capacity_class, "acquire", capacity)
        original_frames = gateway._send_stream_frames

        @functools.wraps(original_frames)
        async def frames(connection, stream_id, **kwargs):
            call_id = next(controller.calls)
            try:
                fields = controller.wire_fields(connection, stream_id, kwargs["method"],
                                               kwargs["path"], kwargs["body"])
            except Exception:
                controller.hook_errors += 1
                fields = {"metadata_unavailable": True, "connection_id": id(connection)}
            controller.emit("gateway_write_started", call_id=call_id, **fields)
            started = time.monotonic()
            try:
                result = await original_frames(connection, stream_id, **kwargs)
            except BaseException as exc:
                stream = getattr(connection, "_streams", {}).get(stream_id)
                controller.emit("gateway_write_error", call_id=call_id, **fields,
                    elapsed_ms=(time.monotonic() - started) * 1000, error_type=type(exc).__name__,
                    delivery=getattr(stream, "delivery", None), phase=getattr(stream, "phase", None))
                raise
            controller.emit("gateway_write_finished", call_id=call_id, **fields,
                elapsed_ms=(time.monotonic() - started) * 1000,
                local_send_flush_completed=True, upstream_acceptance_not_proved=True)
            return result

        self.patch(gateway, "_send_stream_frames", frames)
        original_connect = inspect.getattr_static(gateway, "connect").__func__

        @functools.wraps(original_connect)
        async def connect(cls, session, **kwargs):
            call_id = next(controller.calls)
            controller.emit("noise_handshake_started", call_id=call_id, vm_id=getattr(session, "vm_id", None))
            started = time.monotonic()
            try:
                connection = await original_connect(cls, session, **kwargs)
            except BaseException as exc:
                controller.emit("noise_handshake_error", call_id=call_id,
                    error_type=type(exc).__name__, elapsed_ms=(time.monotonic() - started) * 1000)
                raise
            try:
                controller.connections.add(connection)
                controller.connection_vm[connection] = getattr(session, "vm_id", None)
            except Exception:
                controller.hook_errors += 1
            controller.emit("noise_handshake_finished", call_id=call_id, connection_id=id(connection),
                vm_id=getattr(session, "vm_id", None), elapsed_ms=(time.monotonic() - started) * 1000)
            return connection

        self.patch(gateway, "connect", classmethod(connect))
        original_http = http.request_json

        @functools.wraps(original_http)
        async def request_json(owner, method, path, **kwargs):
            try:
                category = {"/api/session": "session", "/api/hatch/vm/wake": "wake",
                            "/api/hatch/token": "authorization",
                            "/api/hatch/noise-notary-token": "authorization"}.get(urlsplit(path).path, "other")
                fields = {"method": method.upper(), "path": urlsplit(path).path,
                          "category": category, **controller.safe_context(http=owner)}
            except Exception:
                controller.hook_errors += 1
                fields = {"metadata_unavailable": True}
            started = time.monotonic()
            try:
                result = await original_http(owner, method, path, **kwargs)
            except BaseException as exc:
                controller.emit("http_control", **fields, ok=False, error_type=type(exc).__name__,
                    elapsed_ms=(time.monotonic() - started) * 1000)
                raise
            controller.emit("http_control", **fields, ok=True,
                elapsed_ms=(time.monotonic() - started) * 1000)
            return result

        self.patch(http, "request_json", request_json)

    def runtime_sample(self, scheduled_at):
        """Called only on Backend's owning loop; never opens a connection."""
        engine = self.application.engine
        rows, connected, receivers, streams = [], 0, 0, 0
        for runtime in list(engine._runtimes.values()):
            connection = runtime.conn
            active = connection is not None and not connection.is_closed
            receiver = getattr(connection, "_receiver", None)
            receiver_active = receiver is not None and not receiver.done()
            count = len(getattr(connection, "_streams", {}))
            connected += active
            receivers += receiver_active
            streams += count
            if len(rows) < 512:
                rows.append({"account_id": runtime.account_id, "credential_revision": runtime.revision,
                    "connection_id": id(connection) if connection is not None else None,
                    "connection_active": active, "receiver_active": receiver_active,
                    "stream_count": count, "http_client_present": runtime.http._client is not None})
        known = list(self.connections)
        self.emit("backend_sample", loop_lag_ms=(time.monotonic() - scheduled_at) * 1000,
            runtime_count=len(engine._runtimes), account_lock_count=len(engine._account_locks),
            active_operation_count=len(engine._active), owned_task_count=len(engine._tasks),
            active_connection_count=connected, active_receiver_count=receivers,
            stream_count=streams, known_connection_count=len(known),
            known_active_connection_count=sum(not c.is_closed for c in known),
            runtime_details=rows, runtime_details_truncated=len(engine._runtimes) > len(rows),
            hook_error_count=self.hook_errors, metric_queue=self.sink.stats())


class Sampler:
    def __init__(self, instrumentation, interval, server_port):
        self.instrumentation, self.interval, self.server_port = instrumentation, interval, server_port
        self.stop_event = threading.Event()
        self.pending = threading.Event()
        self.previous = None
        self.thread = threading.Thread(target=self._run, name="phase2-resource-sampler", daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        while not self.stop_event.wait(self.interval):
            controller = self.instrumentation
            try:
                now = time.monotonic()
                process = linux_process_sample(self.server_port)
                cpu = process["cpu_seconds_total"]
                process["cpu_percent_one_core_scale"] = (100 * (cpu-self.previous[1]) /
                    (now-self.previous[0])) if self.previous else None
                self.previous = now, cpu
                controller.emit("process_sample", **process, backend_callback_pending=self.pending.is_set(),
                                metric_queue=controller.sink.stats())
            except Exception as exc:
                controller.emit("sample_error", sample="process", error_type=type(exc).__name__)
            loop = getattr(controller.application.engine, "_loop", None)
            if loop is None or not loop.is_running() or self.pending.is_set():
                continue
            self.pending.set()
            scheduled_at = time.monotonic()

            def snapshot(scheduled=scheduled_at):
                try:
                    controller.runtime_sample(scheduled)
                except Exception as exc:
                    controller.emit("sample_error", sample="backend", error_type=type(exc).__name__)
                finally:
                    self.pending.clear()
            try:
                loop.call_soon_threadsafe(snapshot)
            except RuntimeError:
                self.pending.clear()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", "--metrics-jsonl", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18612)
    parser.add_argument("--workers", type=int, choices=(1,), default=1)
    parser.add_argument("--sample-seconds", type=int, choices=(1, 5), default=5)
    parser.add_argument("--metric-capacity", type=int, default=4096)
    args = parser.parse_args(argv)
    if not args.metrics.is_absolute() or args.metrics.suffix != ".jsonl":
        parser.error("--metrics must be an absolute private .jsonl path; existing files are refused")
    if args.metric_capacity < 16 or not 0 < args.port < 65536:
        parser.error("metric capacity must be >=16 and port must be valid")
    if os.environ.get("MUSE_BACKEND") != "protocol" or not os.environ.get("MUSE2API_HOME"):
        parser.error("Set explicit MUSE_BACKEND=protocol and an isolated MUSE2API_HOME")
    # Logs may contain gateway URLs or raw IDs. Structured private metrics are
    # the wrapper's diagnostics; stdout never receives params, Cookie or URLs.
    logging.disable(logging.CRITICAL)
    sink = MetricsSink(args.metrics, args.metric_capacity)
    instrumentation = sampler = None
    previous_signal_handlers = {}
    try:
        application = importlib.import_module("app")  # exactly one application/Store
        store_module = importlib.import_module("store")
        from muse2api.gateway import HatchConnection
        from muse2api.http import MuseHTTP
        from muse2api.protocol import ProtocolConfig
        import uvicorn
        protocol = ProtocolConfig.load()
        instrumentation = Instrumentation(application, sink, {r.path: name for name, r in protocol.routes.items()})
        instrumentation.install(HatchConnection, MuseHTTP, store_module)
        source = Path(application.__file__)
        versions = {}
        for distribution in ("muse2api-http", "curl_cffi", "cryptography", "uvicorn", "fastapi"):
            try:
                versions[distribution] = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                versions[distribution] = None
        sink.emit("server_configuration", pid=os.getpid(), workers=1, versions=versions,
            concurrency=application.CFG.max_concurrency, backend=application.CFG.backend,
            sample_seconds=args.sample_seconds, metrics_capacity=args.metric_capacity,
            python=sys.version.split()[0], platform=sys.platform, application_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            gateway_sha256=hashlib.sha256(Path(inspect.getfile(HatchConnection)).read_bytes()).hexdigest(),
            submission_count_scope="gateway call entry vs local send/flush; not upstream acceptance",
            exact_count_requires_zero_dropped_metrics=True,
            exact_count_requires_zero_hook_errors=True,
            unknown_posts_must_be_reviewed=True)
        sampler = Sampler(instrumentation, args.sample_seconds, args.port)
        sampler.start()
        server = uvicorn.Server(uvicorn.Config(application.app, host=args.host, port=args.port,
            workers=1, access_log=False, log_config=None))
        # Uvicorn replays captured signals after restoring its previous handlers.
        # Let that replay call this same Server instead of SIG_DFL terminating
        # the process before our sampler/writer finally blocks can run.
        for handled_signal in (signal.SIGTERM, signal.SIGINT):
            previous_signal_handlers[handled_signal] = signal.signal(handled_signal, server.handle_exit)
        server.run()
    finally:
        try:
            if sampler is not None:
                sampler.close()
            if instrumentation is not None:
                instrumentation.emit("server_exit", hook_error_count=instrumentation.hook_errors)
                instrumentation.restore()
            counts = sink.close()
            print(json.dumps({"phase2_metrics": counts}), file=sys.stderr)
        finally:
            # Restore the caller's signal policy only after bounded metric drain.
            for handled_signal, previous in previous_signal_handlers.items():
                signal.signal(handled_signal, previous)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Do not leak a URL-bearing upstream exception through a traceback.
        print(json.dumps({"phase2_server_error_type": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(2)
