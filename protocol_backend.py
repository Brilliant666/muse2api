"""Protocol backend for the existing muse2api application.

The application owns account selection, API formatting and Store persistence.
One asyncio loop owns all protocol transports. In-flight operations are recorded
before submission; uncertain outcomes are recovered without generating again.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import copy
import functools
import hashlib
import mimetypes
import os
import queue
import struct
import threading
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, is_dataclass
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from engine import MuseAuthError, MuseEngine, MuseGenerationError


class BackendOperationError(MuseGenerationError):
    def __init__(self, message, *, code="protocol_error", phase="prepare",
                 delivery="not_sent", retryable=False, request_id=None,
                 session_id=None, message_id=None, account_id=None, upstream_status=None):
        super().__init__(message)
        self.code, self.phase, self.delivery = code, phase, delivery
        self.retryable = bool(retryable and delivery == "not_sent")
        self.request_id, self.session_id = request_id, session_id
        self.message_id, self.account_id = message_id, account_id
        self.upstream_status = (upstream_status if type(upstream_status) is int
                                and 400 <= upstream_status <= 599 else None)

    @property
    def task_status(self):
        if self.code == "media_delivery_failed":
            return "delivery_failed"
        return "unknown" if self.delivery in {"unknown", "accepted"} else "failed"

    def public(self):
        result = {key: getattr(self, key) for key in
                  ("code", "phase", "delivery", "retryable", "request_id", "session_id", "message_id", "account_id")}
        if self.upstream_status is not None:
            result["upstream_status"] = self.upstream_status
        return result


def _observed_upstream_status(exc):
    """Preserve typed HTTP status through SDK exception causes; never parse bodies."""
    seen = set()
    for _ in range(8):
        if exc is None or id(exc) in seen:
            break
        seen.add(id(exc))
        for name in ("upstream_status", "status"):
            value = getattr(exc, name, None)
            if type(value) is int and 400 <= value <= 599:
                return value
        exc = exc.__cause__
    return None


def _guard_rate_limited_connection(conn):
    """Do not let optional SDK catches hide an explicit 429 in this operation.

    Only this adapter-owned instance is wrapped. Task-local memory prevents an
    optional registration catch from continuing to submit; it does not poison
    the connection for a later, explicitly initiated recovery task.
    """
    if getattr(conn, "_muse2api_rate_limit_guard", False):
        return conn
    limited_tasks = weakref.WeakKeyDictionary()

    def rejection(args, kwargs):
        path = args[1] if len(args) > 1 else kwargs.get("path", "")
        params = args[2] if len(args) > 2 else kwargs.get("params")
        session_id = params.get("session_id") if isinstance(params, dict) else None
        return BackendOperationError("上游明确限流 (HTTP 429)；停止本操作，勿重复生成",
            code="upstream_rate_limited", phase="history" if "history" in str(path) else "submit",
            delivery="unknown", retryable=False, upstream_status=429,
            session_id=session_id if isinstance(session_id, str) else None)

    def wrap(original):
        @functools.wraps(original)
        async def guarded(*args, **kwargs):
            task = asyncio.current_task()
            if task is not None and task in limited_tasks:
                raise rejection(args, kwargs)
            try:
                return await original(*args, **kwargs)
            except Exception as exc:
                if _observed_upstream_status(exc) != 429:
                    raise
                if task is not None:
                    limited_tasks[task] = True
                raise rejection(args, kwargs) from exc
        return guarded

    for name in ("request", "ack"):
        original = getattr(conn, name, None)
        if callable(original):
            setattr(conn, name, wrap(original))
    conn._muse2api_rate_limit_guard = True
    return conn


# Retained alias for small external validation harnesses.
ProtocolBackendError = BackendOperationError
UnsupportedCapability = BackendOperationError


def _proxy_url(config):
    if not config:
        return None
    if isinstance(config, str):
        return config
    server = str(config.get("server") or "")
    if not server:
        return None
    scheme, marker, address = server.partition("://")
    if not marker:
        raise BackendOperationError("代理地址缺少协议", code="invalid_proxy")
    if config.get("username"):
        auth = quote(str(config["username"]), safe="") + ":" + quote(str(config.get("password") or ""), safe="")
        return scheme + "://" + auth + "@" + address
    return server


def cookie_observations(headers):
    """Return actual expiry/reset/deletion observations from Set-Cookie headers."""
    expires, resets, deleted = {}, set(), set()
    now = time.time()
    for header in headers:
        jar = SimpleCookie()
        try:
            jar.load(header)
        except Exception:
            continue
        for name, morsel in jar.items():
            # A later Set-Cookie for the same name supersedes an earlier deletion/reset.
            expires.pop(name, None)
            resets.discard(name)
            deleted.discard(name)
            expiry = None
            if morsel["max-age"]:
                try:
                    expiry = now + int(morsel["max-age"])
                except ValueError:
                    pass
            elif morsel["expires"]:
                try:
                    expiry = parsedate_to_datetime(morsel["expires"]).timestamp()
                except (ValueError, TypeError, OverflowError):
                    pass
            if not morsel.value or (expiry is not None and expiry <= now):
                deleted.add(name)
            elif expiry is not None:
                expires[name] = int(expiry)
            else:
                resets.add(name)
    return expires, resets, deleted


class _ObservedHTTPClient:
    def __init__(self, wrapped, owner):
        self._wrapped, self._owner = wrapped, owner

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    async def request(self, *args, **kwargs):
        response = await self._wrapped.request(*args, **kwargs)
        getter = getattr(response.headers, "get_list", None)
        if getter:
            self._owner._set_cookie_headers.extend(getter("set-cookie"))
        return response


def _default_transport(settings, cookies, on_cookies):
    from muse2api.cookies import CookieJar
    from muse2api.gateway import HatchConnection
    from muse2api.http import MuseHTTP
    from muse2api.protocol import ProtocolConfig

    class StoreOwnedHTTP(MuseHTTP):
        def __init__(self, *args, **kwargs):
            self._set_cookie_headers = []
            super().__init__(*args, **kwargs)

        def _restore_cached_cookies(self):
            # Store credential_revision is authoritative; no file mtime fallback.
            return None

        def _create_client(self):
            return _ObservedHTTPClient(super()._create_client(), self)

        def _merge_cookies(self):
            if self._client is None:
                return
            observed, resets, deleted = cookie_observations(self._set_cookie_headers)
            self._set_cookie_headers.clear()
            values, expiries = {}, {}
            for cookie in list(self._client.cookies.jar):
                if cookie.name in deleted:
                    self._client.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)
                    continue
                if cookie.value and not cookie.is_expired():
                    values[cookie.name] = cookie.value
                    if cookie.expires and cookie.expires > 0:
                        expiries[cookie.name] = int(cookie.expires)
            expiries.update(observed)
            self.jar = CookieJar(values)
            # A stale callback must propagate, never silently overwrite a new login.
            on_cookies(values, expiries, resets)

        def _persist_cookies(self, *, force=False):
            self._merge_cookies()

        def _persist_session(self, session):
            self._merge_cookies()

        async def connect(self):
            protocol = ProtocolConfig.load()
            session = await self.bootstrap()
            for attempt in range(2):
                conn = None
                try:
                    conn = await HatchConnection.connect(
                        session, lb_host=protocol.lb_host, ws_path=protocol.ws_path,
                        cvm_rv_key=self.settings.cvm_rv_key,
                        attestation_cmd=self.settings.attestation_cmd,
                        ws_client=self.client, impersonate=self.settings.impersonate,
                        user_agent=self.settings.user_agent, proxy=self.proxy,
                        ws_fallback=self.settings.ws_fallback)
                    self._merge_cookies()
                    return session, _guard_rate_limited_connection(conn)
                except BackendOperationError:
                    if conn is not None:
                        await conn.close()
                    raise
                except Exception:
                    if conn is not None:
                        await conn.close()
                    if attempt:
                        raise
                    session = await self.bootstrap(force=True)

    return StoreOwnedHTTP(settings, cookies=cookies)


def _default_settings(account, state_dir):
    from muse2api.config import Settings
    settings = Settings.from_env()
    settings.state_dir = state_dir
    settings.cookies_file = str(state_dir / "unused-cookie-source.txt")
    settings.session_mode = "thread"
    settings.proxy = _proxy_url(account.get("proxy"))
    settings.concurrency_per_account = 1
    return settings


def _default_service(settings, pool):
    from muse2api.chat import ChatService
    return ChatService(settings, pool)


async def _default_media_downloader(conn, ref, destination, **kwargs):
    from muse2api.media import download_media_to
    return await download_media_to(conn, ref, destination, **kwargs)


@dataclass
class _Runtime:
    account_id: str
    revision: int
    settings: object
    http: object
    proxy: str | None
    service: object = None
    pool: object = None
    conn: object = None
    session: object = None
    expected_vm: str | None = None


class _PinnedPool:
    def __init__(self, runtime):
        self.runtime = runtime
        self.settings = runtime.settings
        self.account = SimpleNamespace(id=runtime.account_id, name=runtime.account_id, active_requests=0)

    async def connect(self):
        rt = self.runtime
        if rt.conn is None or rt.conn.is_closed:
            if rt.conn is not None:
                await rt.conn.close()
            session, conn = await rt.http.connect()
            rt.session, rt.conn = session, conn
        if rt.expected_vm and (rt.session is None or rt.session.vm_id != rt.expected_vm):
            await rt.conn.close()
            rt.conn = None
            raise BackendOperationError("账号工作区已变更，原任务结果未确认", code="workspace_changed",
                                        phase="recover", delivery="unknown")
        return self.account, rt.session, rt.conn

    @asynccontextmanager
    async def connection(self):
        self.account.active_requests += 1
        try:
            yield await self.connect()
        finally:
            self.account.active_requests -= 1


def _check_media(path, expected):
    """Reject HTML/JSON/empty payloads and mismatched container signatures."""
    with path.open("rb") as handle:
        head = handle.read(64)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        actual, ext, mime = "image", ".png", "image/png"
    elif head.startswith(b"\xff\xd8\xff"):
        actual, ext, mime = "image", ".jpg", "image/jpeg"
    elif head.startswith((b"GIF87a", b"GIF89a")):
        actual, ext, mime = "image", ".gif", "image/gif"
    elif head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        actual, ext, mime = "image", ".webp", "image/webp"
    elif len(head) >= 12 and head[4:8] == b"ftyp":
        if head[8:12] in {b"avif", b"avis"}:
            actual, ext, mime = "image", ".avif", "image/avif"
        elif head[8:12] in {b"heic", b"heix", b"mif1", b"msf1"}:
            raise BackendOperationError("尚未验证 HEIC/HEIF 媒体交付", code="media_delivery_failed", phase="download", delivery="completed")
        else:
            actual, ext, mime = "video", ".mp4", "video/mp4"
    elif head.startswith(b"\x1a\x45\xdf\xa3"):
        actual, ext, mime = "video", ".webm", "video/webm"
    else:
        raise BackendOperationError("下载文件未通过媒体类型检查", code="media_delivery_failed", phase="download", delivery="completed")
    if actual != expected:
        raise BackendOperationError("下载文件类型与请求不一致", code="media_delivery_failed", phase="download", delivery="completed")
    dimensions = (None, None)
    if ext == ".png" and len(head) >= 24:
        dimensions = struct.unpack(">II", head[16:24])
    return ext, mime, dimensions


class ProtocolBackend:
    backend_name = "protocol"
    proc = None
    page = None

    def __init__(self, store, cfg, *, state_dir=None, settings_factory=None,
                 transport_factory=None, service_factory=None, media_downloader=None):
        self.store, self.cfg = store, cfg
        self.state_dir = Path(state_dir or (Path(cfg.data_dir) / "protocol"))
        self.settings_factory = settings_factory or _default_settings
        self.transport_factory = transport_factory or _default_transport
        self.service_factory = service_factory or _default_service
        self.media_downloader = media_downloader or _default_media_downloader
        self._local = threading.local()
        self._lifecycle = threading.Lock()
        self._thread = self._loop = None
        self._runtimes, self._account_locks = {}, {}
        self._last_http_renew = {}
        self._active, self._tasks = {}, set()
        self._claims_lock, self._recovering = threading.Lock(), set()
        self._stopping = False
        self.proxy_resolver = None

    def _ensure_local(self):
        return self._local

    @property
    def current_acc_id(self):
        return getattr(self._local, "account_id", None)

    def bind_account(self, account):
        self._local.account_id = account["id"]
        self._local.proxy_snapshot = copy.deepcopy(account.get("proxy"))

    def unbind_account(self):
        for name in ("account_id", "proxy_snapshot"):
            if hasattr(self._local, name):
                delattr(self._local, name)

    def start(self):
        with self._lifecycle:
            if self._stopping:
                raise BackendOperationError("协议后端正在停止", code="backend_stopping")
            if self._thread and self._thread.is_alive():
                return
            ready = threading.Event()

            def loop_main():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                ready.set()
                loop.run_forever()
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()

            self._thread = threading.Thread(target=loop_main, name="muse-protocol-loop", daemon=True)
            self._thread.start()
            if not ready.wait(5):
                raise BackendOperationError("协议事件循环启动失败", code="backend_start_failed")

    def _submit(self, coro):
        try:
            self.start()
            if threading.current_thread() is self._thread:
                raise BackendOperationError("同步接口不能阻塞协议事件循环", code="loop_reentry")
            return asyncio.run_coroutine_threadsafe(coro, self._loop)
        except BaseException:
            coro.close()
            raise

    def _map_error(self, exc, op=None):
        if isinstance(exc, BackendOperationError):
            err = exc
        else:
            known_delivery = getattr(exc, "delivery", None)
            delivery = known_delivery or (op or {}).get("delivery", "not_sent")
            code = getattr(exc, "code", "protocol_error")
            upstream_status = _observed_upstream_status(exc)
            if isinstance(exc, (TimeoutError, concurrent.futures.TimeoutError)):
                code = "operation_timeout"
            if isinstance(exc, (asyncio.CancelledError, concurrent.futures.CancelledError)):
                code = "operation_cancelled"
            if type(exc).__name__ == "MuseAuthError":
                code = "session_unauthorized" if "HTTP 401" in str(exc) else "upstream_access_denied"
            if upstream_status in {401, 403, 429}:
                code = {401: "session_unauthorized", 403: "upstream_access_denied",
                        429: "upstream_rate_limited"}[upstream_status]
            err = BackendOperationError(
                "协议操作未完成 (" + code + ")；" + ("请查询原任务结果，勿重复提交" if delivery != "not_sent" else "尚未确认提交"),
                code=code, phase=getattr(exc, "phase", (op or {}).get("phase", "prepare")),
                delivery=delivery, retryable=getattr(exc, "retryable", False),
                session_id=getattr(exc, "session_id", None), message_id=getattr(exc, "message_id", None),
                upstream_status=upstream_status)
        if op:
            err.request_id, err.account_id = op["id"], op["account_id"]
            err.session_id = err.session_id or op.get("session_id")
            err.message_id = err.message_id or op.get("message_id")
        return err

    def _record_error(self, op, exc):
        err = self._map_error(exc, op)
        if op.get("phase") == "download" and err.delivery != "not_sent":
            err.code, err.delivery = "media_delivery_failed", "completed"
        self.store.update_protocol_operation(op["id"], state=err.task_status, error=str(err),
            code=err.code, phase=err.phase, delivery=err.delivery,
            session_id=err.session_id, message_id=err.message_id,
            upstream_status=err.upstream_status)
        return err

    def _call(self, coro, timeout, op=None):
        future = self._submit(coro)
        try:
            return future.result(timeout=timeout)
        except BaseException as exc:
            if not future.done():
                if op:
                    self._record_error(op, exc)
                future.cancel()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise self._map_error(exc, op) from None

    def _snapshot(self, account_id, allow_blocked=False):
        account = self.store.get_account(account_id)
        if not account or not account.get("enabled", True):
            raise BackendOperationError("选定账号不存在或已禁用", code="account_unavailable")
        if account.get("protocol_blocked") and not allow_blocked:
            raise BackendOperationError("该账号存在结果未确认的任务，请先查询恢复或明确放弃", code="account_outcome_unknown")
        if not (account.get("cookies") or {}).get("hatch_sess"):
            raise BackendOperationError("账号缺少 hatch_sess", code="missing_session")
        return account

    async def _runtime(self, account):
        aid, revision = account["id"], int(account.get("credential_revision", 1))
        existing = self._runtimes.get(aid)
        if existing and existing.revision == revision:
            return existing
        if existing:
            await self._close_runtime(existing)
        directory = self.state_dir / hashlib.sha256(aid.encode()).hexdigest()[:20]
        directory.mkdir(parents=True, exist_ok=True)
        settings = self.settings_factory(account, directory)
        settings.impersonate = getattr(self.cfg, "protocol_impersonate", "chrome")

        def persist(values, expires=None, resets=()):
            if not self.store.persist_protocol_cookies(aid, revision, values, expires, resets):
                raise BackendOperationError("账号凭据已被更新，旧会话停止写回", code="credential_revision_changed")

        http = self.transport_factory(settings, account["cookies"], persist)
        rt = _Runtime(aid, revision, settings, http, _proxy_url(account.get("proxy")))
        rt.pool = _PinnedPool(rt)
        rt.service = self.service_factory(settings, rt.pool)
        self._runtimes[aid] = rt
        return rt

    @asynccontextmanager
    async def _owned_runtime(self, account_id, allow_blocked=False):
        lock = self._account_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            yield await self._runtime(self._snapshot(account_id, allow_blocked))

    async def _renew(self, aid, wake_vm, force):
        async with asyncio.timeout(90):
            async with self._owned_runtime(aid, allow_blocked=True) as rt:
                if wake_vm:
                    session = await rt.http.bootstrap(force=force)
                    info = {"status": "assigned", "vm_id": session.vm_id, "vm_state": "RUNNING"}
                else:
                    info = await rt.http.session_info()
                self._last_http_renew[aid] = time.time()
                account = self.store.get_account(aid)
                return {"ok": info.get("status") == "assigned", **info,
                        "wake_ok": bool(wake_vm and info.get("vm_state") == "RUNNING"),
                        "cookies": rt.http.jar.copy(), "cookies_exp": account.get("cookies_exp") or {}}

    def renew_account(self, aid, *, wake_vm=True, force=False):
        return self._call(self._renew(aid, wake_vm, force), 95)

    def renew_session_http(self, cookies, expires=None, wake_vm=True, proxy=None):
        if not self.current_acc_id:
            raise BackendOperationError("维护操作必须明确绑定账号", code="missing_account_binding")
        return self.renew_account(self.current_acc_id, wake_vm=wake_vm, force=True)

    def refresh(self, cookies, expires=None, account_id=None):
        return self.renew_account(account_id or self.current_acc_id, force=True)

    def ensure_page(self, cookies, expires=None, account_id=None):
        return self.renew_account(account_id or self.current_acc_id)

    def close_current_page(self):
        return None

    def reset_thread(self, for_chat=False):
        return None

    def read_cookies(self):
        account = self.store.get_account(self.current_acc_id) or {}
        return {name: {"value": value, "expires": (account.get("cookies_exp") or {}).get(name, -1)}
                for name, value in (account.get("cookies") or {}).items()}

    def _normalize_image(self, image):
        return MuseEngine._normalize_image(self, image)

    def quota(self, cookies, expires=None, account_id=None):
        raise BackendOperationError("协议后端尚未验证额度查询；原缓存保留", code="quota_unsupported")

    def _operation(self, aid, kind, prompt, operation_id=None):
        self._snapshot(aid)
        rid = operation_id or "op_" + uuid.uuid4().hex
        try:
            return self.store.begin_protocol_operation(rid, aid, kind, prompt,
                                                       task_id=operation_id if operation_id and operation_id.startswith("task_") else None)
        except ValueError:
            raise BackendOperationError("该任务已有执行记录，请查询恢复，不能再次生成", code="operation_already_exists", request_id=rid) from None

    def _stage(self, op, **changes):
        op.update(changes)
        self.store.update_protocol_operation(op["id"], **changes)

    async def _prepare_run(self, rt, op):
        rt.expected_vm = None
        _, session, _ = await rt.pool.connect()
        latest = self.store.get_account(rt.account_id)
        if not latest or int(latest.get("credential_revision", 1)) != rt.revision:
            raise BackendOperationError("提交前账号凭据已更新，停止旧会话", code="credential_revision_changed")
        key = "operation:" + op["id"]
        state = rt.service.sessions.get(key)
        rt.expected_vm = session.vm_id
        self._stage(op, session_key=key, session_id=state.session_id, vm_id=session.vm_id,
                    state="running", phase="submit", delivery="unknown")
        return key

    async def _execute(self, op, timeout, action):
        task = asyncio.current_task()
        self._tasks.add(task)
        self._active[op["id"]] = op
        try:
            async with asyncio.timeout(timeout):
                async with self._owned_runtime(op["account_id"]) as rt:
                    key = await self._prepare_run(rt, op)
                    return await action(rt, key)
        except BaseException as exc:
            err = self._record_error(op, exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise err from None
        finally:
            self._tasks.discard(task)
            self._active.pop(op["id"], None)

    def chat_stream(self, cookies, prompt, expires=None, timeout=None, account_id=None,
                    stop_event=None, operation_id=None):
        timeout = float(timeout or self.cfg.chat_timeout)
        aid = account_id or self.current_acc_id
        op = self._operation(aid, "chat", prompt, operation_id)
        messages = queue.Queue(maxsize=128)
        done = threading.Event()

        def push(delta):
            try:
                messages.put_nowait(("delta", delta))
            except queue.Full:
                raise BackendOperationError("客户端读取过慢，任务结果待查询", code="consumer_too_slow", phase="stream", delivery="unknown")

        async def run(rt, key):
            result = await rt.service.run(prompt, on_delta=push, session_key_value=key, timeout=timeout)
            self._stage(op, state="completed", phase="complete", delivery="completed",
                        message_id=result.message_id, result={"text": result.text})

        future = self._submit(self._execute(op, timeout, run))
        future.add_done_callback(lambda _: done.set())
        try:
            while not done.is_set() or not messages.empty():
                if stop_event and stop_event.is_set():
                    return
                try:
                    kind, value = messages.get(timeout=0.1)
                    yield value
                except queue.Empty:
                    continue
            future.result()
        except (GeneratorExit, KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            raise self._map_error(exc, op) from None
        finally:
            if not future.done():
                self._record_error(op, concurrent.futures.CancelledError())
                future.cancel()

    def chat(self, cookies, prompt, expires=None, timeout=None, account_id=None):
        return "".join(self.chat_stream(cookies, prompt, expires, timeout, account_id))

    def _reference_items(self, references, prompt):
        items = []
        for index, image in enumerate(references):
            encoded, mime = self._normalize_image(image)
            base64.b64decode(encoded, validate=True)
            items.append({"type": "image", "mime_type": mime, "data_base64": encoded,
                          "filename": f"reference-{index + 1}" + (mimetypes.guess_extension(mime) or ".png")})
        if items:
            items.append({"type": "text", "text": prompt})
        return items or None

    async def _deliver(self, rt, op, ref, timeout):
        self._stage(op, phase="download", delivery="completed")
        directory = Path(self.cfg.media_dir)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / (op["id"] + ".pending")
        try:
            await self.media_downloader(rt.conn, ref, temporary, proxy=rt.proxy,
                impersonate=rt.settings.impersonate,
                max_bytes=getattr(self.cfg, "protocol_media_max_bytes", 256 * 1024 * 1024), timeout=timeout)
            actual_size = temporary.stat().st_size
            if actual_size < 1 or actual_size > getattr(self.cfg, "protocol_media_max_bytes", 256 * 1024 * 1024):
                raise ValueError("media size outside allowed bounds")
            ext, mime, (width, height) = _check_media(temporary, op["kind"])
            name = uuid.uuid4().hex + ext
            path = directory / name
            os.replace(temporary, path)
            result = {"path": str(path), "filename": name, "size": actual_size, "ext": ext,
                      "mime": mime, "kind": op["kind"], "via": "python-protocol",
                      "attachment": ref.identity, "w": width, "h": height,
                      "operation_id": op["id"]}
            self._stage(op, state="completed", phase="complete", delivery="completed", result=result)
            return result
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            upstream_status = _observed_upstream_status(exc)
            detail = f"；上游状态 HTTP {upstream_status}" if upstream_status is not None else ""
            raise BackendOperationError("生成已完成，但媒体交付失败；可重试下载，勿重复生成" + detail,
                code="media_delivery_failed", phase="download", delivery="completed",
                upstream_status=upstream_status) from None
        finally:
            temporary.unlink(missing_ok=True)

    def generate(self, cookies, prompt, expect="image", timeout=240, expires=None,
                 account_id=None, on_progress=None, reference_image=None,
                 reference_images=None, stop_event=None, operation_id=None):
        if stop_event and stop_event.is_set():
            raise BackendOperationError("任务在提交前已取消", code="cancelled_before_submit")
        references = list(reference_images or ([] if reference_image is None else [reference_image]))
        items = self._reference_items(references, prompt)
        op = self._operation(account_id or self.current_acc_id, expect, prompt, operation_id)

        async def run(rt, key):
            if on_progress:
                on_progress(15)
            result = await rt.service.run_with_media(prompt, kind=expect, timeout=timeout,
                                                     items=items, session_key_value=key)
            candidates = [ref for ref in result.media if ref.kind == expect]
            if not candidates:
                raise BackendOperationError("上游回复已完成，但没有所需媒体", code="media_missing", phase="result", delivery="completed")
            ref = candidates[0]
            serialized = asdict(ref) if is_dataclass(ref) else dict(vars(ref))
            self._stage(op, media=serialized, message_id=result.message_id, delivery="completed")
            return await self._deliver(rt, op, ref, timeout)

        return self._call(self._execute(op, timeout, run), timeout + 5, op)

    async def _recover(self, op, timeout):
        async with asyncio.timeout(timeout):
            async with self._owned_runtime(op["account_id"], allow_blocked=True) as rt:
                rt.expected_vm = op.get("vm_id")
                await rt.pool.connect()
                self._stage(op, state="recovering")
                if not op.get("media"):
                    if not op.get("session_id"):
                        raise BackendOperationError("没有可查询的上游会话标识；请确认后明确放弃", code="recovery_session_missing", phase="recover", delivery="unknown")
                    result = await rt.service.recover_thread(op["session_id"], prompt=op["prompt"],
                        message_id=op.get("message_id") or "", timeout=timeout, want_media=op["kind"] != "chat")
                    if op["kind"] == "chat":
                        self._stage(op, state="completed", delivery="completed", phase="complete", result={"text": result.text})
                        return {"text": result.text}
                    candidates = [ref for ref in result.media if ref.kind == op["kind"]]
                    if not candidates:
                        raise BackendOperationError("原任务未返回目标媒体，禁止重新生成", code="media_missing", phase="recover", delivery="completed")
                    ref = candidates[0]
                    self._stage(op, media=asdict(ref) if is_dataclass(ref) else dict(vars(ref)), delivery="completed")
                else:
                    fields = {k: v for k, v in op["media"].items()
                              if k in {"kind", "path", "url", "mime", "label", "byte_len", "account_id"}}
                    fields.setdefault("path", None)
                    fields.setdefault("url", None)
                    fields["identity"] = fields["path"] or fields["url"] or "stored-media"
                    ref = SimpleNamespace(**fields)
                return await self._deliver(rt, op, ref, timeout)

    def recover_operation(self, request_id, timeout=60):
        op = self.store.get_protocol_operation(request_id)
        if not op:
            raise BackendOperationError("执行记录不存在", code="operation_not_found")
        if op.get("state") == "completed":
            return op.get("result") or {}
        with self._claims_lock:
            if request_id in self._active or request_id in self._recovering:
                raise BackendOperationError("原操作仍在执行", code="operation_busy")
            self._recovering.add(request_id)

        async def tracked():
            task = asyncio.current_task()
            self._tasks.add(task)
            self._active[request_id] = op
            try:
                return await self._recover(op, max(1, min(timeout, 300)))
            finally:
                self._tasks.discard(task)
                self._active.pop(request_id, None)
        try:
            return self._call(tracked(), max(1, min(timeout, 300)) + 5, op)
        except BaseException as exc:
            raise self._record_error(op, exc) from None
        finally:
            with self._claims_lock:
                self._recovering.discard(request_id)

    def abandon_operation(self, request_id):
        op = self.store.get_protocol_operation(request_id)
        if not op:
            raise BackendOperationError("执行记录不存在", code="operation_not_found")
        with self._claims_lock:
            if request_id in self._active or request_id in self._recovering:
                raise BackendOperationError("操作仍在执行，不能解除占用", code="operation_busy")
            # This is an explicit operator choice, not a claim that Muse stopped the job.
            self.store.update_protocol_operation(request_id, state="failed", phase="operator_abandoned",
                delivery=op.get("delivery", "unknown"), error="管理员明确放弃恢复；不表示上游任务已取消")
        return {"abandoned": True, "upstream_cancelled": False, "operation_id": request_id}

    async def _close_runtime(self, rt):
        try:
            if rt.conn is not None:
                await rt.conn.close()
        finally:
            await rt.http.close()

    async def _shutdown(self, timeout):
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=max(1, timeout / 2))
        for rt in list(self._runtimes.values()):
            try:
                await asyncio.wait_for(self._close_runtime(rt), timeout=3)
            except Exception:
                pass
        self._runtimes.clear()

    def stop(self):
        if not self._thread or not self._thread.is_alive():
            return
        self._stopping = True
        timeout = getattr(self.cfg, "protocol_shutdown_timeout", 15)
        for op in list(self._active.values()):
            self._record_error(op, concurrent.futures.CancelledError())
        future = asyncio.run_coroutine_threadsafe(self._shutdown(timeout), self._loop)
        try:
            future.result(timeout=timeout)
        except Exception:
            future.cancel()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)

    def diagnostics(self):
        return {"backend": "protocol", "browser_running": False,
                "running": bool(self._thread and self._thread.is_alive() and not self._stopping),
                "runtime_count": len(self._runtimes), "active_operations": len(self._active),
                "quota_supported": False, "browser_controls": False,
                "updater_supported": False, "live_validation": "required"}
