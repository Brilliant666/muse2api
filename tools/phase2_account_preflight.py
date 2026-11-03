"""No-generation account/identity/quota preflight.

CLI: python tools/phase2_account_preflight.py WORKTREE PRIVATE_ROOT
Import has no config/Store/browser, credential, environment or network effects.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit


class PreflightValidationError(ValueError):
    pass


def validate_paths(worktree, private_root):
    repo, root = Path(worktree), Path(private_root)
    if not repo.is_absolute() or not root.is_absolute():
        raise PreflightValidationError("Explicit absolute directories required")
    repo, root = repo.resolve(), root.resolve()
    if not repo.is_dir() or not root.is_dir() or root == Path(root.anchor):
        raise PreflightValidationError("Expected existing dedicated directories")
    if root == repo or root.is_relative_to(repo):
        raise PreflightValidationError("Private directory cannot be in worktree")
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise PreflightValidationError("Private directory cannot be in Git tree")
    return repo, root


def private_path(root, *parts):
    root = Path(root).resolve()
    path = root.joinpath(*parts).resolve()
    if path == root or not path.is_relative_to(root):
        raise PreflightValidationError("Private path escapes its directory")
    return path


def credential_revision(account):
    value = account.get("credential_revision", 1)
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)) or int(value) < 1:
        raise PreflightValidationError("Invalid credential revision")
    return int(value)


def validate_mapping(mapping, accounts=None):
    """Inspect metadata only; Cookie/VM differences never establish identity."""
    if not isinstance(mapping, list) or not 1 <= len(mapping) <= 10:
        raise PreflightValidationError("Account count must be between 1 and 10")
    ids, aliases = [], []
    for row in mapping:
        if not isinstance(row, dict):
            raise PreflightValidationError("Invalid mapping row")
        aid, alias = row.get("account_id"), row.get("alias")
        if (not isinstance(aid, str) or not aid or aid != aid.strip()
                or len(aid) > 128 or any(ord(c) < 33 or ord(c) == 127 for c in aid)):
            raise PreflightValidationError("Invalid account identifier")
        if (not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", alias)
                or alias.endswith(".") or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", alias.split(".")[0])):
            raise PreflightValidationError("Alias is not a safe path component")
        ids.append(aid)
        aliases.append(alias.casefold())
    if len(set(ids)) != len(ids) or len(set(aliases)) != len(aliases):
        raise PreflightValidationError("Account IDs and aliases must be unique")
    if accounts is not None:
        if not isinstance(accounts, list) or len(accounts) != len(mapping):
            raise PreflightValidationError("Store count differs from mapping")
        actual = []
        for account in accounts:
            if not isinstance(account, dict) or not isinstance(account.get("id"), str):
                raise PreflightValidationError("Invalid Store metadata")
            actual.append(account["id"])
            credential_revision(account)
        if len(set(actual)) != len(actual) or set(actual) != set(ids):
            raise PreflightValidationError("Store IDs differ from mapping")
    return [dict(row) for row in mapping]


def identity_from_html(html, salt):
    if not isinstance(salt, bytes) or len(salt) < 32:
        raise PreflightValidationError("Identity salt needs at least 32 bytes")
    from tools.phase2_identity import authenticated_viewer_id
    principal = authenticated_viewer_id(html)
    return principal, hmac.new(salt, principal.encode(), hashlib.sha256).hexdigest()


def distinct_identities_verified(rows, expected_count):
    if not isinstance(rows, list) or len(rows) != expected_count or not 1 <= expected_count <= 10:
        return False
    hashes = [row.get("upstream_identity_sha256") for row in rows]
    return (all(row.get("identity_verified") is True for row in rows)
            and all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in hashes)
            and len(set(hashes)) == expected_count)


def cdp_url(cfg):
    port = getattr(cfg, "cdp_port", None)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise PreflightValidationError("Invalid configured CDP port")
    return f"http://127.0.0.1:{port}/json/version"


def validate_runtime_paths(cfg, root):
    root = Path(root).resolve()
    for name in ("base_dir", "data_dir", "profile_dir", "download_dir", "media_dir"):
        path = Path(getattr(cfg, name))
        if not path.is_absolute() or not path.resolve().is_relative_to(root):
            raise PreflightValidationError("Runtime storage must stay in private directory")
    site = urlsplit(cfg.site_url)
    if (site.scheme != "https" or site.hostname not in {"muse.ai", "www.muse.ai"}
            or site.port not in (None, 443) or site.username or site.password):
        raise PreflightValidationError("Unexpected authenticated site origin")
    cdp_url(cfg)


def _stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_preflight(worktree, private_root):
    repo, root = validate_paths(worktree, private_root)
    mapping = validate_mapping(json.loads(private_path(root, "account-map.json").read_text("utf-8")))
    salt = private_path(root, "identity-salt.bin").read_bytes()
    if len(salt) < 32:
        raise PreflightValidationError("Identity salt needs at least 32 bytes")
    from dotenv import dotenv_values
    env = {k: v for k, v in dotenv_values(private_path(root, "windows-preflight.env")).items() if v is not None}
    home = Path(env.get("MUSE2API_HOME", ""))
    if not home.is_absolute() or not home.resolve().is_relative_to(root):
        raise PreflightValidationError("Explicit private MUSE2API_HOME required")
    for name in list(os.environ):
        if name.startswith(("MUSE_", "MUSE2API_")):
            del os.environ[name]
    os.environ.update(env)
    sys.path.insert(0, str(repo))
    from config import Config
    from store import Store
    from engine import MuseEngine
    cfg = Config()
    validate_runtime_paths(cfg, root)
    store = Store(cfg)  # One Store only, in this explicit standalone run.
    mapping = validate_mapping(mapping, store.accounts)
    out = private_path(root, "account-preflight")
    out.mkdir(exist_ok=True)
    result = {"schema_version": 1,
        "identity_observed_platform": "Windows isolated browser" if os.name == "nt" else "Linux isolated browser",
        "backend_generation_target": "linux protocol", "checked_at": _stamp(),
        "upstream_identity_verified": False, "accounts": [], "generation_requests": 0,
        "expected_account_count": len(mapping)}
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    engine = None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(cdp_url(cfg), timeout=10) as response:
            if response.status != 200:
                raise PreflightValidationError("Configured isolated CDP unavailable")
        engine = MuseEngine(cfg)
        engine.start()
        for row in mapping:
            aid = row["account_id"]
            account = store.get_account(aid)
            if not account:
                raise PreflightValidationError("Mapped account disappeared")
            revision = credential_revision(account)
            directory = private_path(root, "account-preflight", row["alias"])
            directory.mkdir(exist_ok=True)
            item = {"account_id": aid, "alias": row["alias"], "enabled": account.get("enabled", True),
                    "identity_verified": False, "quota_current": False, "cookies_persisted": False}
            try:
                engine.ensure_page(account["cookies"], account.get("cookies_exp"), account_id=aid)
                engine.page.send("Emulation.setDeviceMetricsOverride",
                    {"width": 1440, "height": 2400, "deviceScaleFactor": 1, "mobile": False})
                time.sleep(2)
                resources = engine.page.js("JSON.stringify(performance.getEntriesByType('resource').map(x=>x.name).filter(x=>x.includes('/api/')))")
                (directory / "resource-urls.json").write_text(str(resources), "utf-8")
                engine._quota_click(engine._QUOTA_SETTINGS_JS, "Settings entry unavailable")
                engine._quota_click(engine._QUOTA_MENU_JS, "Settings item unavailable")
                deadline = time.monotonic() + 15
                while True:
                    text = engine.page.js(engine._QUOTA_DIALOG_JS) or ""
                    quota = engine._parse_quota(text)
                    if quota["found"]:
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError("No current quota in settings dialog")
                    time.sleep(.2)
                emails = engine.page.js("JSON.stringify([...document.querySelectorAll('[role=dialog] input[type=email],[aria-modal=true] input[type=email]')].filter(e=>e.getClientRects().length).map(e=>e.value))")
                extra = json.loads(emails) if isinstance(emails, str) else []
                candidates = set(re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text + "\n" + "\n".join(extra)))
                candidates = {x.lower() for x in candidates if not x.lower().endswith("@muse.ai")}
                (directory / "settings-dialog.txt").write_text(text, "utf-8")
                _json(directory / "identity-candidates.json", sorted(candidates))
                _json(directory / "quota.json", quota)
                item.update(quota_current=True, quota={k: v for k, v in quota.items() if k != "raw"}, checked_at=_stamp())
                if engine.page.js("location.origin") not in {"https://muse.ai", "https://www.muse.ai"}:
                    raise PreflightValidationError("Authenticated HTML origin not verified")
                html = engine.page.js("document.documentElement.outerHTML") or ""
                (directory / "identity-bootstrap.html").write_text(html, "utf-8")
                from tools.phase2_identity import IdentityUnverified
                try:
                    principal, digest = identity_from_html(html, salt)
                    _json(directory / "principal.json", {"principal": principal,
                        "source": "authenticated viewerId == prelude.user.identifier"})
                    item.update(identity_verified=True,
                        identity_source="authenticated AuthContextProvider viewerId and prelude.user.identifier",
                        upstream_identity_sha256=digest, identity_hash_scheme="round-private HMAC-SHA256")
                except IdentityUnverified:
                    item["identity_candidate_count"] = 0
                extra_left = re.search(r"剩余\s*([^）\n)]+)", text)
                if extra_left:
                    item["quota"]["extra_left_display"] = extra_left.group(1).strip()
                item["quota"]["weekly_remaining_pct"] = 100 - item["quota"]["weekly_used_pct"]
            except Exception as exc:
                item["error_type"] = type(exc).__name__
                (directory / "error.txt").write_text(str(exc), "utf-8")
                if engine.page:
                    try:
                        state = engine.page.js("JSON.stringify({url:location.href,body:document.body.innerText,buttons:[...document.querySelectorAll('button,[role=button]')].map(e=>({text:e.innerText,aria:e.getAttribute('aria-label'),testid:e.getAttribute('data-testid'),visible:!!e.getClientRects().length}))})")
                        (directory / "ui-state.json").write_text(str(state), "utf-8")
                    except Exception as capture_error:
                        item["capture_error_type"] = type(capture_error).__name__
            finally:
                try:
                    observed = engine.read_cookies()
                    if observed:
                        values = {k: v["value"] for k, v in observed.items() if v["value"]}
                        expires = {k: v["expires"] for k, v in observed.items() if v["expires"] > 0}
                        item["cookies_persisted"] = store.persist_protocol_cookies(aid, revision, values, expires)
                except Exception as persist_error:
                    item["cookie_persist_error_type"] = type(persist_error).__name__
                finally:
                    engine.close_current_page()
            result["accounts"].append(item)
            _json(root / "account-preflight.json", result)
            print(json.dumps({"alias": row["alias"], "error_type": item.get("error_type"),
                              "weekly_used_pct": item.get("quota", {}).get("weekly_used_pct")}), flush=True)
        result.update(
            upstream_identity_verified=distinct_identities_verified(result["accounts"], len(mapping)),
            all_current_quotas=all(x["quota_current"] for x in result["accounts"]),
            all_have_weekly_remaining=all(x.get("quota", {}).get("weekly_remaining_pct", 0) > 0 for x in result["accounts"]),
            all_cookies_persisted=all(x["cookies_persisted"] for x in result["accounts"]),
            finished_at=_stamp())
        result["status"] = ("PASS" if result["upstream_identity_verified"]
            and result["all_current_quotas"] and result["all_have_weekly_remaining"]
            and result["all_cookies_persisted"] and not any(x.get("error_type") for x in result["accounts"])
            else "BLOCKED")
        _json(root / "account-preflight.json", result)
        return result
    finally:
        if engine is not None:
            engine.stop()
        logging.disable(previous_logging)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Isolated 1..10-account no-generation preflight")
    parser.add_argument("worktree", type=Path)
    parser.add_argument("private_root", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_preflight(args.worktree, args.private_root)
        return 0 if result["status"] == "PASS" else 2
    except Exception as exc:
        print(json.dumps({"alias": None, "error_type": type(exc).__name__,
                          "weekly_used_pct": None}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
