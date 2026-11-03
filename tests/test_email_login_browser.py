"""Offline browser-driver contract tests; no Muse/Chrome/service/network."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from email_login_browser import EmailLoginBrowser, browser_available, _allowed_url, _SNAPSHOT, _SUBMIT, _CLICK
from muse_identity import IdentityUnverified, authenticated_viewer_id

EMAIL = "pilot@example.test"
CODE = "123456"
PRINCIPAL = "1000000000000001"


def identity(viewer=PRINCIPAL, user=PRINCIPAL, authenticated=True):
    models = [{"viewerId": viewer, "isAuthenticated": authenticated},
              {"prelude": {"user": {"kind": "user", "identifier": user}}}]
    flight = "".join(f"{n + 17:x}:" + json.dumps(m) + "\n" for n, m in enumerate(models))
    return "<script>self.__next_f.push(" + json.dumps([1, flight]) + ")</script>"


def email_state(**changes):
    return dict(url="https://muse.ai/", blocked=False, sending=False,
                email_ready=True, otp_ready=False, auth_ready=True, **changes)


def otp_state(target=EMAIL, **changes):
    return dict(url="https://muse.ai/", blocked=False, sending=False,
                email_ready=False, otp_ready=True, invalid_code=False,
                challenge_emails=[target], **changes)


def authenticated_state():
    return dict(url="https://muse.ai/thread/new", blocked=False, sending=False,
                email_ready=False, otp_ready=False)


class Clock:
    def __init__(self):
        self.now = 0
        self.hook = None

    def __call__(self):
        return self.now

    def sleep(self, amount):
        self.now += amount
        if self.hook:
            self.hook(self.now)


class Process:
    def __init__(self, args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.exited = False
        self.terminates = self.kills = 0
        profile = Path(next(x.split("=", 1)[1] for x in args if x.startswith("--user-data-dir=")))
        (profile / "DevToolsActivePort").write_text("45678\n/devtools/browser/owned-test\n", encoding="utf-8")

    def poll(self):
        return 0 if self.exited else None

    def wait(self, timeout):
        if not self.exited:
            raise subprocess.TimeoutExpired("fake-owned-Chrome", timeout)
        return 0

    def terminate(self):
        self.terminates += 1
        self.exited = True

    def kill(self):
        self.kills += 1
        self.exited = True


class Browser:
    def __init__(self, env):
        self.env = env
        self.calls, self.disposed, self.contexts = [], [], []
        self.closed = False

    def send(self, method, params=None, timeout=None):
        self.calls.append((method, params))
        if method == "Target.createBrowserContext":
            ident = "owned-context-" + str(len(self.contexts) + 1)
            self.contexts.append(ident)
            return {"result": {"browserContextId": ident}}
        if method == "Target.createTarget":
            return {"result": {"targetId": "owned-page"}}
        if method == "Target.disposeBrowserContext":
            self.disposed.append(params["browserContextId"])
        if method == "Storage.getCookies":
            self.env.cookie_contexts.append(params["browserContextId"])
            return {"result": {"cookies": self.env.cookies}}
        if method == "Browser.close":
            self.env.proc.exited = True
        return {"result": {}}

    def close(self):
        self.closed = True


class Page:
    def __init__(self, env):
        self.env = env
        self.closed = False

    def send(self, method, params=None, timeout=None):
        self.env.page_calls.append((method, params))
        if method == "Input.insertText":
            self.env.inserted.append(params["text"])
            # The observed official six-digit onChange invokes confirm-otp
            # before Input.insertText returns, without a button click.
            if self.env.prepared == "code" and len(params["text"]) == 6 and self.env.auto_complete:
                self.env.auto_submissions += 1
                self.env.state = dict(self.env.after_code)
        return {"result": {}}

    def js(self, expr, timeout=None):
        if "muse-email:snapshot" in expr:
            return dict(self.env.state)
        if "muse-email:identity" in expr:
            self.env.identity_reads += 1
            return self.env.html
        if "muse-email:email-submit" in expr:
            self.env.prepared = "email"
            return {"prepared": True}
        if "muse-email:code-submit" in expr:
            self.env.prepared = "code"
            return {"prepared": True}
        if "muse-email:click" in expr:
            kind = self.env.prepared
            if kind == "code" and self.env.auto_submissions:
                # Auto-completion already disabled/replaced the OTP form.
                return {"submitted": False}
            self.env.clicks.append(kind)
            if self.env.fail_after_click:
                raise TimeoutError("synthetic sensitive error must never escape")
            self.env.state = dict(self.env.after_email if kind == "email" else self.env.after_code)
            return {"submitted": self.env.submitted}
        raise AssertionError("Unexpected browser script")

    def close(self):
        self.closed = True


class Environment:
    def __init__(self, root):
        self.root, self.clock = root, Clock()
        self.state = email_state()
        self.after_email, self.after_code = otp_state(), authenticated_state()
        self.cookies = [{"domain": ".muse.ai", "name": "hatch_sess", "value": "fake-only-session", "expires": -1}]
        self.html = identity()
        self.clicks, self.inserted, self.pages, self.urls, self.page_calls, self.cookie_contexts = [], [], [], [], [], []
        self.identity_reads = 0
        self.auto_submissions = 0
        self.auto_complete = True
        self.fail_after_click = False
        self.submitted = True
        self.proc, self.browser = None, Browser(self)
        self.cfg = SimpleNamespace(chromium=sys.executable, profile_root=str(root),
                                   cdp_port=19210, profile_dir=str(root / "existing-shared"))

    def popen(self, args, **kwargs):
        self.proc = Process(args, **kwargs)
        return self.proc

    def cdp(self, url, timeout=None):
        self.urls.append(url)
        if "/devtools/browser/" in url:
            return self.browser
        page = Page(self)
        self.pages.append(page)
        return page

    def driver(self):
        return EmailLoginBrowser(self.cfg, cdp_factory=self.cdp, popen=self.popen,
                                 clock=self.clock, sleep=self.clock.sleep)


# Executes the actual embedded scripts against the observed FORM/SECTION shape.
# Node provides only a DOM stand-in: no browser, fetch, sockets, or dependencies.
_DOM_FIXTURE = r"""
const vm = require('vm');
const p = JSON.parse(require('fs').readFileSync(0,'utf8'));
let clicks = 0;
const visible = {getClientRects:()=>[{}]};
const button = {...visible, innerText:p.label || (p.otp?'下一步':'Continue'),
                disabled:!!p.disabled, getAttribute:()=>null, click:()=>{clicks++}};
const section = {innerText:p.otp?'输入安全码并登录\n如需登录，请输入我们发送到pilot@example.test的验证码。重发验证码\n记住此设备\n下一步\n试试其他方式':p.heading};
const form = {parentElement:section, querySelectorAll:()=>[button]};
const field = {...visible, value:'', disabled:false, maxLength:p.otp?6:254,
               closest:()=>form, getAttribute:()=>null,
               focus:()=>{}, select:()=>{field.selectionStart=0;field.selectionEnd=field.value.length}};
if(!p.otp) field.__reactProps$test = {};
const heading = {...visible, innerText:p.heading || '输入安全码并登录'};
const doc = {
  body:{innerText:section.innerText || ''},
  querySelector:()=>field,
  querySelectorAll:s=>{
    if(s==='input[autocomplete="username webauthn"]')return p.otp?[]:[field];
    if(s==='input[autocomplete="one-time-code"]')return p.otp?[field]:[];
    if(s==='h1,h2,[role="heading"]')return [heading];
    if(s==='button[type="submit"],input[type="submit"]')return [button];
    if(s==='input[type="checkbox"][required]')return p.required?[visible]:[];
    return [];
  }
};
const sandbox = {document:doc, location:new URL('https://muse.ai/'), URL,
                 performance:{getEntriesByType:()=>[{name:'https://muse.ai/api/auth/native/restart',responseEnd:1}]},
                 getComputedStyle:()=>({visibility:'visible'})};
const run = source => vm.runInNewContext(source,sandbox,{timeout:1000});
const snapshot = run(p.snapshot), prepared = run(p.prepare), before = run(p.click);
field.value = '123456'; button.disabled = false;
const after = run(p.click);
console.log(JSON.stringify({snapshot,prepared,before,after,clicks}));
"""


def dom_fixture(**params):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("Node unavailable: embedded DOM logic not executed")
    params.update(snapshot=_SNAPSHOT,
                  prepare=_SUBMIT.replace("SUBMIT", "code-submit").replace("SELECTOR", json.dumps('input[autocomplete="one-time-code"]')).replace("VALUE", json.dumps(CODE)),
                  click=_CLICK.replace("SELECTOR", json.dumps('input[autocomplete="one-time-code"]')).replace("VALUE", json.dumps(CODE)))
    result = subprocess.run([node, "-e", _DOM_FIXTURE], input=json.dumps(params),
                            text=True, capture_output=True, timeout=10, check=True)
    return json.loads(result.stdout)


class EmailLoginBrowserTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="email-driver-offline-")
        self.env = Environment(Path(self.scratch.name))
        self.driver = self.env.driver()

    def tearDown(self):
        self.driver.close()
        self.scratch.cleanup()

    def begin(self):
        self.assertEqual(self.driver.begin(EMAIL), {"status": "WAITING_FOR_CODE", "code_length": 6})

    def test_dom_empty_otp_disabled_next_is_ready_and_can_fill_before_click(self):
        result = dom_fixture(otp=True, disabled=True)
        self.assertTrue(result["snapshot"]["otp_ready"])
        self.assertFalse(result["snapshot"]["sending"])
        self.assertTrue(result["prepared"]["prepared"])
        self.assertFalse(result["before"]["submitted"])
        self.assertTrue(result["after"]["submitted"])
        self.assertEqual(result["clicks"], 1)
        self.env.after_email = result["snapshot"]
        self.begin()

    def test_dom_joint_login_heading_is_allowed_but_standalone_signup_is_blocked(self):
        for heading in ("Log in or create an account", "Sign in or create an account"):
            with self.subTest(heading=heading):
                result = dom_fixture(heading=heading)
                self.assertFalse(result["snapshot"]["blocked"])
        for attrs in ({"heading":"Create an account"}, {"heading":"Sign up"},
                      {"heading":"Log in or create an account", "required":True}):
            with self.subTest(attrs=attrs):
                self.assertTrue(dom_fixture(**attrs)["snapshot"]["blocked"])

    def test_dom_otp_loading_text_is_sending_without_inferring_it_from_disabled(self):
        result = dom_fixture(otp=True, disabled=True, label="正在验证")
        self.assertTrue(result["snapshot"]["sending"])
        self.assertFalse(result["snapshot"]["otp_ready"])

    def test_cleanup_rejects_a_different_resolved_profile(self):
        self.begin()
        foreign = self.env.root / "muse-email-login-unowned"
        foreign.mkdir()
        (foreign / "keep").write_text("preserve",encoding="utf-8")
        self.driver._profile = foreign
        with patch("email_login_browser.shutil.rmtree") as remove:
            self.driver.close()
        remove.assert_not_called()
        self.assertTrue((foreign / "keep").exists())

    def test_cleanup_rejects_symlink_or_junction_marker(self):
        self.begin()
        owned = self.driver._profile
        with patch.object(Path, "is_symlink", return_value=True):
            with patch("email_login_browser.shutil.rmtree") as remove:
                self.driver.close()
        remove.assert_not_called()
        self.assertTrue(owned.exists())

    def test_cleanup_preserves_profile_while_owned_process_is_still_alive(self):
        self.begin()
        owned = self.driver._profile
        self.env.proc.poll = lambda:None
        self.env.proc.terminate = lambda:None
        self.env.proc.kill = lambda:None
        self.driver.close()
        self.assertTrue(owned.exists())

    def test_stop_signal_does_not_wait_for_operation_lock_and_is_not_cleared(self):
        finished = threading.Event()
        def stop():
            self.driver.request_stop()
            finished.set()
        with self.driver._lock:
            worker = threading.Thread(target=stop)
            worker.start()
            self.assertTrue(finished.wait(1), "request_stop waited for the driver lock")
        worker.join(1)
        self.driver.close_session()
        self.driver.close()
        self.assertEqual(self.driver.begin(EMAIL)["status"], "UNKNOWN")
        self.assertIsNone(self.env.proc)
        self.assertEqual(self.env.clicks, [])

    def test_begin_wait_can_be_cancelled_before_any_submit(self):
        self.env.state["auth_ready"] = False
        self.env.clock.hook = lambda now:self.driver.request_stop() if now >= 1 else None
        self.assertEqual(self.driver.begin(EMAIL)["status"], "UNKNOWN")
        self.assertLessEqual(self.env.clock.now, 1.25)
        self.assertEqual(self.env.clicks, [])
        self.assertEqual(self.env.browser.disposed, ["owned-context-1"])

    def test_verify_result_wait_can_be_cancelled_without_resubmit(self):
        self.begin()
        self.env.cookies = []
        stop_at = self.env.clock.now + 1
        self.env.clock.hook = lambda now:self.driver.request_stop() if now >= stop_at else None
        self.assertEqual(self.driver.verify(CODE)["status"], "UNKNOWN")
        self.assertLessEqual(self.env.clock.now, stop_at + .25)
        self.assertEqual(self.env.clicks, ["email"])
        self.assertEqual(self.env.auto_submissions, 1)
        self.assertEqual(self.env.browser.disposed, ["owned-context-1"])

    def test_stop_after_native_input_prevents_the_actual_click(self):
        original_send = Page.send
        driver = self.driver
        def stopped_send(page, method, params=None, timeout=None):
            result = original_send(page, method, params, timeout)
            if method == "Input.insertText":
                driver.request_stop()
            return result
        with patch.object(Page, "send", stopped_send):
            self.assertEqual(self.driver.begin(EMAIL)["status"], "UNKNOWN")
        self.assertEqual(self.env.inserted, [EMAIL])
        self.assertEqual(self.env.clicks, [])

    def test_stop_during_startup_cleans_only_owned_process_and_profile(self):
        def no_devtools(args, **kwargs):
            proc = self.env.popen(args, **kwargs)
            profile = Path(next(x.split("=", 1)[1] for x in args if x.startswith("--user-data-dir=")))
            (profile / "DevToolsActivePort").unlink()
            return proc
        self.driver = EmailLoginBrowser(self.env.cfg, cdp_factory=self.env.cdp, popen=no_devtools,
                                       clock=self.env.clock, sleep=self.env.clock.sleep)
        self.env.clock.hook = lambda now:self.driver.request_stop() if now >= 1 else None
        self.assertEqual(self.driver.begin(EMAIL)["status"], "UNKNOWN")
        self.assertLessEqual(self.env.clock.now, 1.2)
        self.assertTrue(self.env.proc.exited)
        self.assertEqual(self.env.clicks, [])
        self.assertEqual(list(self.env.root.glob("muse-email-login-*")), [])

    def test_auto_completion_loading_observes_result_without_button_click(self):
        self.begin()
        self.env.after_code = {"url":"https://muse.ai/", "sending":True,
                               "otp_ready":False,"email_ready":False}
        complete_at = self.env.clock.now + .5
        def complete(now):
            if self.env.auto_submissions and now >= complete_at:
                self.env.state = authenticated_state()
        self.env.clock.hook = complete
        self.assertEqual(self.driver.verify(CODE)["status"],"AUTHENTICATED")
        self.assertEqual(self.env.auto_submissions,1)
        self.assertEqual(self.env.clicks,["email"])
        self.assertEqual(self.env.inserted,[EMAIL,CODE])

    def test_auto_completion_navigated_form_accepts_cookie_and_identity(self):
        self.begin()
        self.env.after_code = authenticated_state()
        self.assertEqual(self.driver.verify(CODE)["status"],"AUTHENTICATED")
        self.assertEqual(self.env.auto_submissions,1)
        self.assertEqual(self.env.clicks,["email"])
        self.assertEqual(self.env.inserted,[EMAIL,CODE])

    def test_auto_completion_absent_is_bounded_unknown_without_fallback_click(self):
        self.begin()
        self.env.auto_complete = False
        started = self.env.clock.now
        self.assertEqual(self.driver.verify(CODE)["status"],"UNKNOWN")
        self.assertGreaterEqual(self.env.clock.now-started,45)
        self.assertLessEqual(self.env.clock.now-started,45.25)
        self.assertEqual(self.env.auto_submissions,0)
        self.assertEqual(self.env.clicks,["email"])
        self.assertEqual(self.env.inserted,[EMAIL,CODE])

    def test_constructor_and_availability_do_not_launch_or_attach(self):
        self.assertTrue(browser_available(self.env.cfg))
        self.assertTrue(self.driver.available())
        self.assertIsNone(self.env.proc)
        self.assertEqual(self.env.urls, [])
        self.assertFalse(browser_available(SimpleNamespace(chromium="surely-not-a-chrome-executable")))

    def test_owned_random_port_new_profile_context_and_success(self):
        shared = self.env.root / "existing-shared"
        shared.mkdir()
        (shared / "keep").write_text("preserve", encoding="utf-8")
        self.begin()
        profile = self.driver._profile
        result = self.driver.verify(CODE)
        self.assertEqual(result["status"], "AUTHENTICATED")
        self.assertEqual(result["principal_id"], PRINCIPAL)
        self.assertTrue(result["challenge_email_matched"])
        self.assertEqual(self.env.clicks, ["email"])
        self.assertEqual(self.env.auto_submissions, 1)
        self.assertEqual(self.env.inserted, [EMAIL, CODE])
        self.assertIn("--remote-debugging-port=0", self.env.proc.args)
        self.assertIn("--remote-debugging-address=127.0.0.1", self.env.proc.args)
        self.assertTrue(all("45678" in url for url in self.env.urls))
        self.assertEqual(self.env.cookie_contexts, ["owned-context-1"])
        self.driver.close()
        self.assertEqual(self.env.browser.disposed, ["owned-context-1"])
        self.assertTrue(self.env.pages[0].closed)
        self.assertTrue(self.env.proc.exited)
        self.assertFalse(profile.exists())
        self.assertEqual((shared / "keep").read_text(), "preserve")

    def test_hydration_gate_prevents_premature_click(self):
        self.env.state["auth_ready"] = False
        def hydrated(now):
            if now < 2:
                self.assertEqual(self.env.clicks, [])
            else:
                self.env.state["auth_ready"] = True
        self.env.clock.hook = hydrated
        self.begin()
        self.assertEqual(self.env.clicks, ["email"])
        self.assertGreaterEqual(self.env.clock.now, 2.5)

    def test_no_hydration_is_bounded_without_send(self):
        self.env.state["auth_ready"] = False
        self.assertEqual(self.driver.begin(EMAIL)["status"], "UNKNOWN")
        self.assertEqual(self.env.clicks, [])
        self.assertLessEqual(self.env.clock.now, 31)
        self.assertEqual(self.env.browser.disposed, ["owned-context-1"])

    def test_sending_state_and_code_words_are_not_otp_page(self):
        self.env.after_email = {"url": "https://muse.ai/", "sending": True,
                                "otp_ready": False, "email_ready": False}
        def ready(now):
            if now < 3:
                self.assertNotEqual(self.driver._phase, "WAITING_FOR_CODE")
            elif self.env.clicks:
                self.env.state = otp_state()
        self.env.clock.hook = ready
        self.begin()
        self.assertGreaterEqual(self.env.clock.now, 3)
        self.assertEqual(self.env.clicks, ["email"])

    def test_submit_response_loss_never_double_clicks_or_retries(self):
        self.env.fail_after_click = True
        result = self.driver.begin(EMAIL)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.env.clicks, ["email"])
        self.assertNotIn("sensitive", str(result))
        self.assertIsNone(self.driver._page)

    def test_disallowed_origin_is_blocked_before_any_sensitive_input(self):
        for url in ("http://muse.ai/", "https://evil.test/", "https://muse.ai.evil.test/",
                    "https://user@muse.ai/", "https://muse.ai:444/"):
            with self.subTest(origin=url):
                self.env.state = email_state()
                self.env.state["url"] = url
                self.assertEqual(self.driver.begin(EMAIL)["status"], "BLOCKED")
        self.assertEqual(self.env.inserted, [])
        self.assertEqual(self.env.clicks, [])
        self.assertTrue(_allowed_url("https://www.muse.ai:443/"))

    def test_redirect_after_single_email_send_is_blocked_without_code(self):
        self.env.after_email = {"url": "https://evil.test/", "otp_ready": True}
        self.assertEqual(self.driver.begin(EMAIL)["status"], "BLOCKED")
        self.assertEqual(self.env.clicks, ["email"])
        self.assertEqual(self.env.inserted, [EMAIL])

    def test_challenge_target_mismatch_or_ambiguous_is_rejected(self):
        for targets in (["other@example.test"], [], [EMAIL, "other@example.test"]):
            with self.subTest(target_count=len(targets)):
                self.env.state = email_state()
                self.env.after_email = otp_state()
                self.env.after_email["challenge_emails"] = targets
                self.assertEqual(self.driver.begin(EMAIL)["status"], "BLOCKED")
        self.assertNotIn("code", self.env.clicks)

    def test_challenge_casefold_and_recheck_before_code(self):
        self.env.after_email = otp_state(EMAIL.upper())
        self.begin()
        self.env.state["challenge_emails"] = ["other@example.test"]
        self.assertEqual(self.driver.verify(CODE)["status"], "BLOCKED")
        self.assertEqual(self.env.clicks, ["email"])

    def test_invalid_code_is_explicit_and_same_code_never_resubmits(self):
        self.env.after_code = otp_state()
        self.env.after_code["invalid_code"] = True
        self.begin()
        self.assertEqual(self.driver.verify("12")["status"], "INVALID_CODE")
        self.assertEqual(self.driver.verify(CODE)["status"], "INVALID_CODE")
        self.assertEqual(self.driver.verify(CODE)["status"], "INVALID_CODE")
        self.assertEqual(self.env.clicks, ["email"])
        self.assertEqual(self.env.auto_submissions, 1)
        self.env.after_code = authenticated_state()
        self.assertEqual(self.driver.verify("654321")["status"], "AUTHENTICATED")
        self.assertEqual(self.env.clicks, ["email"])
        self.assertEqual(self.env.auto_submissions, 2)

    def test_captcha_or_forced_extra_step_fails_closed(self):
        self.env.state["blocked"] = True
        self.assertEqual(self.driver.begin(EMAIL)["status"], "BLOCKED")
        self.assertEqual(self.env.clicks, [])

    def test_cookie_domain_and_name_filters(self):
        self.env.cookies.extend([
            {"domain": "muse.ai.evil.test", "name": "hatch_sess", "value": "foreign"},
            {"domain": "notmuse.ai", "name": "hatch_other", "value": "foreign"},
            {"domain": ".muse.ai", "name": "analytics", "value": "exclude"},
            {"domain": "www.muse.ai", "name": "hatch_gw", "value": "fake-route"},
        ])
        self.begin()
        result = self.driver.verify(CODE)
        self.assertEqual(result["cookies"], {"hatch_sess": "fake-only-session", "hatch_gw": "fake-route"})

    def test_conflicting_cookie_values_are_blocked(self):
        self.env.cookies.append({"domain": "www.muse.ai", "name": "hatch_sess", "value": "other-fake"})
        self.begin()
        self.assertEqual(self.driver.verify(CODE)["status"], "BLOCKED")

    def test_cookie_without_authenticated_identity_never_succeeds(self):
        self.env.html = "<html>not authenticated</html>"
        self.begin()
        result = self.driver.verify(CODE)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.env.clicks, ["email"])
        self.assertEqual(self.env.auto_submissions, 1)
        self.assertLessEqual(self.env.clock.now, 47)
        self.assertEqual(sum(m == "Page.navigate" for m, _ in self.env.page_calls), 2)

    def test_principal_conflict_is_blocked_without_refreshing_it_away(self):
        self.env.html = identity(user="1000000000000002")
        self.begin()
        result = self.driver.verify(CODE)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertNotIn("principal_id", result)
        self.assertEqual(sum(m == "Page.navigate" for m, _ in self.env.page_calls), 1)

    def test_missing_cookie_never_accepts_even_valid_identity(self):
        self.env.cookies = []
        self.begin()
        self.assertEqual(self.driver.verify(CODE)["status"], "UNKNOWN")
        self.assertEqual(self.env.identity_reads, 0)

    def test_each_begin_disposes_only_its_previous_context(self):
        self.begin()
        self.env.state = email_state()
        self.begin()
        self.assertEqual(self.env.browser.contexts, ["owned-context-1", "owned-context-2"])
        self.assertEqual(self.env.browser.disposed, ["owned-context-1"])
        self.assertEqual(self.env.urls.count("ws://127.0.0.1:45678/devtools/browser/owned-test"), 1)
        self.driver.close_session()
        self.assertEqual(self.env.browser.disposed, ["owned-context-1", "owned-context-2"])

    def test_partial_startup_failure_cleans_owned_process_and_profile(self):
        def fail_page(url, timeout=None):
            if "/devtools/page/" in url:
                raise RuntimeError("synthetic startup failure")
            return self.env.cdp(url, timeout)
        self.driver = EmailLoginBrowser(self.env.cfg, cdp_factory=fail_page, popen=self.env.popen,
                                       clock=self.env.clock, sleep=self.env.clock.sleep)
        self.assertEqual(self.driver.begin(EMAIL)["status"], "UNKNOWN")
        self.assertTrue(self.env.proc.exited)
        self.assertEqual(self.env.browser.disposed, ["owned-context-1"])
        self.assertEqual(list(self.env.root.glob("muse-email-login-*")), [])

    def test_pure_identity_requires_boolean_authentication_and_matching_principals(self):
        self.assertEqual(authenticated_viewer_id(identity()), PRINCIPAL)
        for html in (identity(authenticated=False), identity(viewer="0", user="0"),
                     identity(user="1000000000000002"), "<html></html>"):
            with self.assertRaises(IdentityUnverified):
                authenticated_viewer_id(html)


if __name__ == "__main__":
    unittest.main()
