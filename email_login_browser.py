"""Dedicated, bounded browser login. This module never imports app/config/Store.

All returned messages are fixed strings. AUTHENTICATED contains secrets and is
an internal driver result: the manager must persist it privately and redact it.
No logging, screenshots, raw HTML files, OTP persistence, or automatic resend.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit

from cdp import CDP
from muse_identity import IdentityUnverified, authenticated_viewer_id

_SITE = "https://muse.ai/"
_HOSTS = {"muse.ai", "www.muse.ai"}
_SNAPSHOT = r"""/* muse-email:snapshot */
(() => {
  const visible = e => !!e && !!e.getClientRects().length &&
    getComputedStyle(e).visibility !== 'hidden';
  const all = s => Array.from(document.querySelectorAll(s)).filter(visible);
  const email = all('input[autocomplete="username webauthn"]')[0];
  const otp = all('input[autocomplete="one-time-code"]')[0];
  const field = otp || email;
  const form = field && field.closest('form');
  const submit = (form ? Array.from(form.querySelectorAll('button[type="submit"],input[type="submit"]'))
    : all('button[type="submit"],input[type="submit"]')).find(visible);
  const label = submit ? (submit.innerText || submit.value || '').trim() : '';
  const body = (document.body && document.body.innerText || '').slice(0,16000);
  const titles = all('h1,h2,[role="heading"]').map(e => (e.innerText || '').replace(/\s+/g,' ').trim());
  const signupHeading = titles.some(t =>
    /创建(?:一个)?(?:新)?账号|注册账号|create (?:an? )?account|sign up|授权访问|authorize access/i.test(t) &&
    !/^(?:(?:log in|sign in)\s+or\s+(?:create (?:an? )?account|sign up)|(?:登录|登入)\s*(?:或|或者|\/)\s*(?:创建(?:一个)?(?:新)?账号|注册(?:账号)?))[.!。！]?$/i.test(t));
  const requiredConsent = all('input[type="checkbox"][required]').length > 0;
  const captcha = all('iframe[src*="captcha"],iframe[src*="challenges.cloudflare.com"],[data-sitekey],.g-recaptcha,.h-captcha').length > 0 ||
    /verify (?:you are|that you are) human|人机验证|请完成安全验证|请验证您是人类/i.test(body);
  const extraStep = requiredConsent ||
    signupHeading ||
    /^(?:同意并继续|接受条款|授权|创建账号|注册|sign up|create account|authorize|allow access)$/i.test(label) ||
    all('input[type="password"]').length > 0 ||
    /^\/(?:signup|register|consent|terms)(?:\/|$)/i.test(location.pathname);
  const reactReady = !!email && Object.keys(email).some(k => k.startsWith('__reactProps$') || k.startsWith('__reactFiber$'));
  const restartReady = performance.getEntriesByType('resource').some(e => {
    try { return new URL(e.name).origin === location.origin &&
      new URL(e.name).pathname === '/api/auth/native/restart' && e.responseEnd > 0; }
    catch (_) { return false; }
  });
  const challengeScope = otp && otp.closest('form') && otp.closest('form').parentElement;
  const challengeEmails = challengeScope ? Array.from(new Set((challengeScope.innerText || '').match(/[A-Za-z0-9.!#$%&'*+/=?^_{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g) || [])) : [];
  const sending = !!field && !!submit && (submit.getAttribute('aria-busy') === 'true' ||
    /正在发送验证码|正在验证|正在登录|验证中|登录中|sending (?:a )?(?:code|verification)|verifying|logging in|signing in/i.test(label));
  const invalid = !!otp && (otp.getAttribute('aria-invalid') === 'true' ||
    /验证码(?:不正确|错误|无效|已过期)|无效(?:的)?验证码|invalid (?:verification )?code|incorrect code|code (?:has )?expired/i.test(body));
  return {url:location.href, blocked:captcha || extraStep, sending,
    auth_ready:reactReady && restartReady, challenge_emails:challengeEmails,
    email_ready:!!email && !email.disabled && !!submit,
    otp_ready:!!otp && !otp.disabled && otp.maxLength === 6 && !!submit &&
      /^(?:下一步|next|verify|验证|continue)$/i.test(label),
    invalid_code:invalid};
})()
"""
# Validate the origin and field again in the *same* evaluation as the mutation.
_SUBMIT = r"""/* muse-email:SUBMIT */
(() => {
  if (location.protocol !== 'https:' || !['muse.ai','www.muse.ai'].includes(location.hostname) ||
      (location.port && location.port !== '443')) return {blocked:true};
  const e = document.querySelector(SELECTOR);
  const visible = n => !!n && !!n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden';
  const form = e && e.closest('form');
  const b = (form ? Array.from(form.querySelectorAll('button[type="submit"],input[type="submit"]'))
    : Array.from(document.querySelectorAll('button[type="submit"],input[type="submit"]'))).find(visible);
  if (!visible(e) || e.disabled || !b) return {submitted:false};
  e.focus(); e.select();
  if (e.selectionStart !== 0 || e.selectionEnd !== e.value.length) return {prepared:false};
  return {prepared:true};
})()
"""
_CLICK = r"""/* muse-email:click */
(() => {
  if (location.protocol !== 'https:' || !['muse.ai','www.muse.ai'].includes(location.hostname) ||
      (location.port && location.port !== '443')) return {blocked:true};
  const e = document.querySelector(SELECTOR), form = e && e.closest('form');
  const visible = n => !!n && !!n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden';
  const b = (form ? Array.from(form.querySelectorAll('button[type="submit"],input[type="submit"]'))
    : Array.from(document.querySelectorAll('button[type="submit"],input[type="submit"]'))).find(visible);
  if (!visible(e) || e.disabled || e.value !== VALUE || !b || b.disabled) return {submitted:false};
  b.click(); return {submitted:true};
})()
"""


def browser_available(cfg):
    """Check executable availability without constructing/starting a driver."""
    executable = str(getattr(cfg, "chromium", "") or "")
    return bool(executable and (Path(executable).is_file() or shutil.which(executable)))


def _allowed_url(value):
    try:
        u = urlsplit(value)
        return (u.scheme == "https" and u.hostname in _HOSTS
                and u.port in (None, 443) and not u.username and not u.password)
    except (ValueError, TypeError):
        return False


class _Blocked(Exception):
    pass


class _Stopped(Exception):
    pass


class EmailLoginBrowser:
    """One owned Chrome, with a fresh context for each explicit begin()."""

    def __init__(self, cfg, *, cdp_factory=None, popen=None, clock=None, sleep=None):
        self.cfg = cfg
        self._cdp = cdp_factory or CDP
        self._popen = popen or subprocess.Popen
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._proc = self._browser = self._page = None
        self._profile = self._context = self._port = None
        self._profile_parent = self._profile_created = None
        self._phase = "IDLE"
        self._codes = set()
        self._identity_reloaded = False
        self._email = None
        self._challenge_matched = False

    def available(self):
        """Probe only the configured executable; do not launch or attach."""
        return browser_available(self.cfg)

    def request_stop(self):
        """Signal shutdown without acquiring the operation lock or using CDP."""
        self._stop_requested.set()

    def _check_stop(self):
        if self._stop_requested.is_set():
            raise _Stopped()

    def _start(self):
        self._check_stop()
        if self._proc and self._proc.poll() is None and self._browser:
            return
        self.close()
        if not self.available():
            raise _Blocked()
        # Never reuse an engine profile, fixed port, old lock, or running Chrome.
        root = getattr(self.cfg, "profile_root", None)
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
        self._profile = Path(tempfile.mkdtemp(prefix="muse-email-login-", dir=root))
        self._profile_parent = self._profile.parent.resolve()
        self._profile_created = self._profile.resolve()
        args = [str(self.cfg.chromium), "--headless=new", "--disable-gpu",
                "--disable-dev-shm-usage", "--disable-background-networking",
                "--no-first-run", "--no-default-browser-check",
                "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                "--remote-allow-origins=*", "--window-size=1280,900",
                f"--user-data-dir={self._profile}", "about:blank"]
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            args.insert(1, "--no-sandbox")
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            kwargs.update(startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self._check_stop()
            self._proc = self._popen(args, **kwargs)
            deadline = self._clock() + 20
            active = self._profile / "DevToolsActivePort"
            while self._clock() < deadline:
                self._check_stop()
                if self._proc.poll() is not None:
                    raise _Blocked()
                try:
                    rows = active.read_text(encoding="utf-8").splitlines()
                    port = int(rows[0])
                    browser_path = rows[1]
                    if not (0 < port < 65536 and re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", browser_path)):
                        raise _Blocked()
                    self._port = port
                    self._browser = self._cdp(f"ws://127.0.0.1:{port}{browser_path}", timeout=2)
                    return
                except (OSError, IndexError, ValueError):
                    self._sleep(0.1)
            raise _Blocked()
        except Exception:
            self.close()
            raise

    def _open(self):
        try:
            self._open_unchecked()
        except Exception:
            self.close()
            raise

    def _open_unchecked(self):
        self._start()
        self._check_stop()
        result = self._browser.send("Target.createBrowserContext", timeout=3)
        self._context = result.get("result", {}).get("browserContextId")
        if not self._context:
            raise _Blocked()
        self._check_stop()
        target = self._browser.send("Target.createTarget",
            {"url": "about:blank", "browserContextId": self._context}, timeout=3)
        ident = target.get("result", {}).get("targetId")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9-]+", ident):
            raise _Blocked()
        self._check_stop()
        self._page = self._cdp(f"ws://127.0.0.1:{self._port}/devtools/page/{ident}", timeout=2)
        self._check_stop()
        self._page.send("Page.enable", timeout=3)
        self._page.send("Runtime.enable", timeout=3)
        self._check_stop()
        self._page.send("Page.navigate", {"url": _SITE}, timeout=3)

    def _snapshot(self, allow_initial_blank=False):
        self._check_stop()
        value = self._page.js(_SNAPSHOT, timeout=3)
        if (allow_initial_blank and isinstance(value, dict) and value.get("url") == "about:blank"):
            return {}
        if not isinstance(value, dict) or not _allowed_url(value.get("url")):
            raise _Blocked()
        if value.get("blocked"):
            raise _Blocked()
        return value

    def _submit(self, selector, value, marker):
        self._check_stop()
        script = _SUBMIT.replace("SUBMIT", marker).replace("SELECTOR", json.dumps(selector)).replace("VALUE", json.dumps(value))
        result = self._page.js(script, timeout=3)
        if not isinstance(result, dict) or result.get("blocked"):
            raise _Blocked()
        if result.get("prepared") is not True:
            return False
        # Trusted native input replaces the explicitly selected current value.
        # Nothing is retried if any CDP response is lost.
        self._snapshot()
        self._check_stop()
        self._page.send("Input.insertText", {"text": value}, timeout=3)
        self._check_stop()
        if marker == "code-submit":
            # Official OTP onChange completes at six digits. The input itself
            # may already have sent confirm-otp; never also click or resend.
            return True
        click = _CLICK.replace("SELECTOR", json.dumps(selector)).replace("VALUE", json.dumps(value))
        self._check_stop()
        result = self._page.js(click, timeout=3)
        if not isinstance(result, dict) or result.get("blocked"):
            raise _Blocked()
        return result.get("submitted") is True

    def _match_challenge(self, state):
        targets = state.get("challenge_emails")
        if (not isinstance(targets, list) or not targets
                or any(not isinstance(x, str) for x in targets)
                or {x.casefold() for x in targets} != {self._email.casefold()}):
            raise _Blocked()
        self._challenge_matched = True

    def _terminal(self, status):
        self.close_session()
        return {"status": status, "message": {
            "BLOCKED": "登录遇到额外步骤或安全条件未满足，请人工检查。",
            "UNKNOWN": "登录结果尚未确认；未自动重发或重试。",
        }[status]}

    def begin(self, email):
        with self._lock:
            self.close_session()
            if self._stop_requested.is_set():
                return self._terminal("UNKNOWN")
            if (not isinstance(email, str) or len(email) > 254
                    or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", email)):
                return self._terminal("BLOCKED")
            try:
                self._open()
                self._email = email
                ready_since = None
                deadline = self._clock() + 30
                submitted = False
                while self._clock() < deadline:
                    self._check_stop()
                    state = self._snapshot(allow_initial_blank=not submitted)
                    if state.get("otp_ready") and submitted and not state.get("sending"):
                        self._match_challenge(state)
                        self._phase = "WAITING_FOR_CODE"
                        return {"status": "WAITING_FOR_CODE", "code_length": 6}
                    if state.get("otp_ready") and not submitted:
                        raise _Blocked()
                    ready = state.get("email_ready") and state.get("auth_ready") and not state.get("sending")
                    ready_since = (ready_since if ready_since is not None else self._clock()) if ready else None
                    if ready_since is not None and self._clock() - ready_since >= 0.5 and not submitted:
                        # Mark before CDP: a lost response never leads to a second click.
                        submitted = True
                        self._phase = "SENDING"
                        if not self._submit('input[autocomplete="username webauthn"]', email, "email-submit"):
                            return self._terminal("UNKNOWN")
                    self._sleep(0.25)
                return self._terminal("UNKNOWN")
            except _Blocked:
                return self._terminal("BLOCKED")
            except Exception:
                return self._terminal("UNKNOWN")

    def _cookies(self):
        self._check_stop()
        result = self._browser.send("Storage.getCookies",
            {"browserContextId": self._context}, timeout=3)
        cookies, expires = {}, {}
        for row in result.get("result", {}).get("cookies", []):
            if not isinstance(row, dict) or row.get("domain", "").lstrip(".").lower() not in _HOSTS:
                continue
            name, value = row.get("name"), row.get("value")
            if not isinstance(name, str) or not re.fullmatch(r"hatch_[A-Za-z0-9_]+", name):
                continue
            if not isinstance(value, str) or not value:
                continue
            expiry = row.get("expires", -1)
            if isinstance(expiry, (int, float)) and math.isfinite(expiry) and expiry > 0:
                if expiry <= time.time():
                    continue
                expires[name] = int(expiry)
            if name in cookies and cookies[name] != value:
                raise _Blocked()
            cookies[name] = value
        return cookies, expires

    def verify(self, code):
        with self._lock:
            if self._stop_requested.is_set():
                return self._terminal("UNKNOWN")
            if self._phase != "WAITING_FOR_CODE" or self._page is None:
                return self._terminal("BLOCKED")
            if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) or code in self._codes:
                return {"status": "INVALID_CODE", "message": "请提供本次登录的新六位验证码。"}
            try:
                state = self._snapshot()
                if not state.get("otp_ready") or state.get("sending"):
                    return self._terminal("UNKNOWN")
                self._match_challenge(state)
                self._codes.add(code)
                self._phase = "VERIFYING"
                if not self._submit('input[autocomplete="one-time-code"]', code, "code-submit"):
                    return self._terminal("UNKNOWN")
                deadline = self._clock() + 45
                while self._clock() < deadline:
                    self._check_stop()
                    try:
                        state = self._snapshot()
                        if state.get("invalid_code") and state.get("otp_ready") and not state.get("sending"):
                            self._phase = "WAITING_FOR_CODE"
                            return {"status": "INVALID_CODE", "message": "验证码无效或已过期，请检查本次验证码。"}
                        if not state.get("otp_ready") and not state.get("sending"):
                            cookies, expires = self._cookies()
                            if cookies.get("hatch_sess"):
                                self._check_stop()
                                html = self._page.js("/* muse-email:identity */ document.documentElement.outerHTML", timeout=3)
                                try:
                                    principal = authenticated_viewer_id(html)
                                except IdentityUnverified as exc:
                                    if exc.reason != "missing":
                                        raise _Blocked() from None
                                    if not self._identity_reloaded:
                                        # A SPA transition may retain unauthenticated Flight props.
                                        # One official GET refresh obtains server-side identity.
                                        self._identity_reloaded = True
                                        self._check_stop()
                                        self._page.send("Page.navigate", {"url": _SITE}, timeout=3)
                                else:
                                    self._check_stop()
                                    self._phase = "AUTHENTICATED"
                                    return {"status": "AUTHENTICATED", "cookies": cookies,
                                            "expires": expires, "principal_id": principal,
                                            "challenge_email_matched": self._challenge_matched}
                    except TimeoutError:
                        # Only read-only post-input observations are retryable.
                        # The deadline and the one-shot GET flag remain unchanged.
                        self._check_stop()
                    self._sleep(min(0.25, max(0, deadline - self._clock())))
                return self._terminal("UNKNOWN")
            except _Blocked:
                return self._terminal("BLOCKED")
            except Exception:
                return self._terminal("UNKNOWN")

    def close_session(self):
        with self._lock:
            page, context = self._page, self._context
            self._page = self._context = None
            self._phase = "IDLE"
            self._codes.clear()
            self._identity_reloaded = False
            self._email = None
            self._challenge_matched = False
            if page:
                try:
                    page.close()
                except Exception:
                    pass
            if context and self._browser:
                try:
                    self._browser.send("Target.disposeBrowserContext",
                        {"browserContextId": context}, timeout=2)
                except Exception:
                    pass

    def close(self):
        with self._lock:
            self.close_session()
            browser, proc, profile = self._browser, self._proc, self._profile
            profile_parent, profile_created = self._profile_parent, self._profile_created
            self._browser = self._proc = self._profile = self._port = None
            self._profile_parent = self._profile_created = None
            if browser:
                try:
                    browser.send("Browser.close", timeout=2)
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
            if proc:
                try:
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.terminate()
                        proc.wait(timeout=3)
                    except Exception:
                        try:
                            proc.kill()
                            proc.wait(timeout=3)
                        except Exception:
                            pass
            # Verify the absolute deletion target against ownership saved at
            # mkdtemp time; reject redirects, links/junctions, and live processes.
            if profile is not None and (proc is None or proc.poll() is not None):
                try:
                    target = profile.resolve(strict=True)
                    if (profile.is_absolute() and profile_parent is not None
                            and profile_created is not None
                            and target == profile_created
                            and target.parent == profile_parent
                            and target.is_relative_to(profile_parent)
                            and target.name.startswith("muse-email-login-")
                            and target.name != "muse-email-login-"
                            and not profile.is_symlink()
                            and not (hasattr(profile, "is_junction") and profile.is_junction())):
                        shutil.rmtree(target, ignore_errors=True)
                except (OSError, ValueError):
                    pass
