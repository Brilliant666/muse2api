"""Before-service Linux identity/session preflight. No generation or Noise.

CLI: python tools/phase2_linux_preflight.py --private-root /phase2-private
The operator must stop all other holders of this Store before invoking it.
Only one Store is constructed; importing this module constructs none.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import sys
import time

try:
    from tools.phase2_account_preflight import (
        PreflightValidationError, _json, _stamp, distinct_identities_verified,
        identity_from_html, private_path, validate_mapping, validate_paths,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {"tools", "tools.phase2_account_preflight"}:
        raise
    from phase2_account_preflight import (
        PreflightValidationError, _json, _stamp, distinct_identities_verified,
        identity_from_html, private_path, validate_mapping, validate_paths,
    )

ALLOWED_URLS = frozenset({"https://muse.ai/", "https://muse.ai/api/session"})


class GetOnlyError(RuntimeError):
    pass


class IdentityMismatch(RuntimeError):
    pass


class GetOnlyClient:
    """Keep the Backend observed Cookie client; constrain every native request."""

    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.calls = []
        self.blocked_attempts = 0

    @property
    def cookies(self):
        return self.wrapped.cookies

    async def close(self):
        await self.wrapped.close()

    async def request(self, method, url, **kwargs):
        params = kwargs.get("params")
        valid_params = params in (None, {}) if url == "https://muse.ai/" else params == {"vm_state": "live"}
        if (method != "GET" or url not in ALLOWED_URLS or not valid_params
                or any(kwargs.get(k) is not None for k in ("json", "data", "content", "files"))):
            self.blocked_attempts += 1
            raise GetOnlyError("Only the two no-generation GET routes are permitted")
        kwargs.update(allow_redirects=False, max_redirects=0)
        kwargs["timeout"] = min(float(kwargs.get("timeout") or 20), 20)
        call = {"method": "GET", "route": "/" if url.endswith("/") else "/api/session"}
        self.calls.append(call)
        response = await self.wrapped.request("GET", url, **kwargs)
        call["status"] = response.status_code
        return response


def validate_windows_evidence(mapping, windows):
    mapping = validate_mapping(mapping)
    if (not isinstance(windows, dict) or windows.get("status") != "PASS"
            or windows.get("upstream_identity_verified") is not True
            or windows.get("all_current_quotas") is not True
            or windows.get("all_have_weekly_remaining") is not True
            or windows.get("all_cookies_persisted") is not True
            or windows.get("identity_observed_platform") != "Windows isolated browser"):
        raise PreflightValidationError("A completed Windows identity/quota PASS is required")
    rows = windows.get("accounts")
    if not distinct_identities_verified(rows, len(mapping)):
        raise PreflightValidationError("Windows principals are not independently verified")
    by_id = {row.get("account_id"): row for row in rows}
    if set(by_id) != {row["account_id"] for row in mapping}:
        raise PreflightValidationError("Windows account IDs differ from mapping")
    for row in mapping:
        observed = by_id[row["account_id"]]
        if (observed.get("alias") != row["alias"] or observed.get("enabled", True) is not True
                or observed.get("identity_hash_scheme") != "round-private HMAC-SHA256"
                or observed.get("quota_current") is not True or observed.get("cookies_persisted") is not True):
            raise PreflightValidationError("Windows alias/identity/quota proof differs")
    return by_id


def validate_linux_cfg(cfg, root):
    root = Path(root).resolve()
    if cfg.backend != "protocol":
        raise PreflightValidationError("Protocol backend is required")
    for name in ("base_dir", "data_dir", "media_dir"):
        value = Path(getattr(cfg, name))
        if not value.is_absolute() or not value.resolve().is_relative_to(root):
            raise PreflightValidationError("Linux Store paths must remain private")


@contextmanager
def exclusive_preflight(root):
    """Prevent overlapping copies of this preflight, before any Store opens."""
    lock = private_path(root, "linux-preflight.lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


async def inspect_account(backend, row, expected, salt, directory, headers_factory):
    aid = row["account_id"]
    item = {"account_id": aid, "alias": row["alias"], "enabled": True,
            "identity_verified": False, "windows_identity_match": False,
            "linux_quota_queried": False, "noise_gateway_tested": False,
            "cookies_persisted": False,
            "quota_evidence": {"platform": "windows", "source": "account-preflight.json",
                "checked_at": expected.get("checked_at"),
                "windows_ui_current": expected["quota_current"], "quota": expected.get("quota", {})}}
    runtime = gate = None
    try:
        async with backend._owned_runtime(aid) as runtime:
            gate = GetOnlyClient(runtime.http.client)
            runtime.http._client = gate
            # This calls session_info only: no bootstrap/wake/token/Noise.
            session = await runtime.http.session_info()
            _json(directory / "session-info.json", session)
            item.update(session_info_observed=True, session_http_success=True, workspace_status=session.get("status"),
                        vm_state=session.get("vm_state"))
            headers = headers_factory(runtime.settings.impersonate)
            headers["Accept"] = "text/html,application/xhtml+xml"
            if getattr(runtime.settings, "user_agent", None):
                headers["User-Agent"] = runtime.settings.user_agent
            try:
                response = await runtime.http.client.request("GET", "https://muse.ai/",
                    headers=headers, timeout=20)
            finally:
                # Reuse the Backend's observed Set-Cookie + credential_revision CAS.
                runtime.http._merge_cookies()
            if response.status_code != 200 or str(getattr(response, "url", "https://muse.ai/")) != "https://muse.ai/":
                raise GetOnlyError("Homepage response was not the allowed HTTP 200")
            if "text/html" not in response.headers.get("content-type", "").lower():
                raise GetOnlyError("Homepage response is not HTML")
            html = response.text
            if not isinstance(html, str) or len(html.encode("utf-8")) > 8 * 1024 * 1024:
                raise GetOnlyError("Homepage HTML exceeds its bounded input")
            (directory / "identity-bootstrap.html").write_text(html, encoding="utf-8")
            _, digest = identity_from_html(html, salt)
            if not hmac.compare_digest(digest, expected["upstream_identity_sha256"]):
                raise IdentityMismatch("Linux principal differs from the Windows proof")
            item.update(identity_verified=True, windows_identity_match=True,
                upstream_identity_sha256=digest, identity_hash_scheme="round-private HMAC-SHA256",
                identity_source="Linux native curl HTTPS HTML: authenticated viewerId == prelude.user.identifier",
                html_utf8_sha256=hashlib.sha256(html.encode("utf-8")).hexdigest(),
                cookies_persisted=True, status="PASS")
    except Exception as exc:
        item.update(status="BLOCKED", error_type=type(exc).__name__)
        (directory / "error.txt").write_text(str(exc), encoding="utf-8")
    finally:
        if gate is not None:
            item["http_request_intents"] = gate.calls
            item["blocked_request_attempts"] = gate.blocked_attempts
        if runtime is not None:
            try:
                await backend._close_runtime(runtime)
            except Exception as close_error:
                item.update(status="BLOCKED", cleanup_error_type=type(close_error).__name__)
                # Keep the runtime reachable for backend.stop() to retry cleanup.
            else:
                backend._runtimes.pop(aid, None)
    _json(directory / "result.json", item)
    return item


def run_preflight(private_root):
    if not sys.platform.startswith("linux"):
        raise PreflightValidationError("This proof requires actual Linux")
    repo = Path(__file__).resolve().parents[1]
    _, root = validate_paths(repo, private_root)
    # The container's explicit env must select the private Store before config's
    # dotenv loader runs. Do not default to a source checkout's data.
    home = Path(os.environ.get("MUSE2API_HOME", ""))
    if not home.is_absolute() or not home.resolve().is_relative_to(root):
        raise PreflightValidationError("Explicit private MUSE2API_HOME required")
    mapping = validate_mapping(json.loads(private_path(root, "account-map.json").read_text("utf-8")))
    windows_bytes = private_path(root, "account-preflight.json").read_bytes()
    windows = json.loads(windows_bytes)
    expected = validate_windows_evidence(mapping, windows)
    salt = private_path(root, "identity-salt.bin").read_bytes()
    if len(salt) < 32:
        raise PreflightValidationError("Identity salt needs at least 32 bytes")
    with exclusive_preflight(root):
        sys.path.insert(0, str(repo))
        from config import Config
        from store import Store
        from protocol_backend import ProtocolBackend
        from muse2api.http import get_base_headers
        cfg = Config()
        validate_linux_cfg(cfg, root)
        store = Store(cfg)
        validate_mapping(mapping, store.accounts)
        if any(not account.get("enabled", True) for account in store.accounts):
            raise PreflightValidationError("Mapped Linux pool must be enabled")
        backend = ProtocolBackend(store, cfg)
        output = private_path(root, "linux-preflight")
        output.mkdir(exist_ok=True)
        result = {"schema_version": 2, "platform": "linux", "backend": "protocol",
            "checked_at": _stamp(), "accounts": [], "upstream_identity_verified": False,
            "generation_requests": 0, "approval_requests": 0, "generation_post_requests": 0,
            "transport": "Backend-owned native curl HTTP; no Noise connection",
            "noise_gateway_tested": False, "linux_quota_queried": False,
            "acceptance_scope": "identity match and session HTTP readability; no VM readiness or generation claim",
            "store_ownership": "single standalone Store before service; preflight lock held",
            "windows_quota_evidence": {"source": "account-preflight.json", "platform": "windows",
                "sha256": hashlib.sha256(windows_bytes).hexdigest(), "status": windows["status"]},
            "allowed_methods": ["GET"], "allowed_urls": sorted(ALLOWED_URLS)}
        previous_logging = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            for row in mapping:
                directory = private_path(root, "linux-preflight", row["alias"])
                directory.mkdir(exist_ok=True)
                item = backend._call(inspect_account(backend, row, expected[row["account_id"]],
                    salt, directory, get_base_headers), 60)
                result["accounts"].append(item)
                _json(root / "preflight.json", result)
                print(json.dumps({"alias": row["alias"], "status": item["status"],
                                  "error_type": item.get("error_type")}), flush=True)
            result["upstream_identity_verified"] = (
                distinct_identities_verified(result["accounts"], len(mapping))
                and all(item["windows_identity_match"] and item["status"] == "PASS"
                        for item in result["accounts"]))
            result["status"] = "PASS" if result["upstream_identity_verified"] else "BLOCKED"
            result["finished_at"] = _stamp()
            _json(root / "preflight.json", result)
            return result
        finally:
            backend.stop()
            logging.disable(previous_logging)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Linux no-generation native identity/session preflight")
    parser.add_argument("--private-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_preflight(args.private_root)
        return 0 if result["status"] == "PASS" else 2
    except Exception as exc:
        print(json.dumps({"alias": None, "status": "BLOCKED",
                          "error_type": type(exc).__name__}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
