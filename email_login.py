"""Serialized, persistent email-login queue; browser and account storage are injected."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

_TERMINAL = {"AUTHENTICATED", "FAILED", "BLOCKED", "UNKNOWN", "CANCELLED", "EXPIRED", "INTERRUPTED"}
_FIELDS = ("id", "email", "status", "created_at", "updated_at", "deadline_at", "begin_count", "attempts", "code_length", "error_code")
_EMAIL = re.compile(r"[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+\Z")


class LoginManager:
    def __init__(self, data_dir, driver_factory, save_account, max_jobs=10, otp_timeout=600):
        if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or not 1 <= max_jobs <= 10:
            raise ValueError("invalid_max_jobs")
        if isinstance(otp_timeout, bool) or not isinstance(otp_timeout, (int, float)) or not 0 < otp_timeout <= 3600:
            raise ValueError("invalid_otp_timeout")
        if not callable(driver_factory) or not callable(save_account):
            raise ValueError("invalid_login_callback")
        self._path = Path(data_dir) / "email-login-jobs.json"
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._factory, self._save = driver_factory, save_account
        self.max_jobs, self.otp_timeout = max_jobs, otp_timeout
        self._cv = threading.Condition(threading.RLock())
        self._jobs, self._codes = [], {}
        self._active, self._driver, self._available = None, None, None
        self._closed, self._driver_failed, self._error_code = False, False, None
        if self._path.exists():
            try:
                rows = json.loads(self._path.read_text(encoding="utf-8"))["jobs"]
                for row in rows:
                    job = {key: row.get(key) for key in _FIELDS}
                    if not isinstance(job["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", job["id"]):
                        raise ValueError()
                    job["email"] = self._email(job["email"])
                    if job["status"] not in _TERMINAL:
                        job.update(status="INTERRUPTED", error_code="PROCESS_RESTARTED", updated_at=time.time())
                    self._jobs.append(job)
            except Exception:
                raise ValueError("login_state_invalid") from None
            self._persist()
        self._thread = threading.Thread(target=self._run, name="email-login-queue", daemon=True)
        self._thread.start()

    @staticmethod
    def _email(value):
        if not isinstance(value, str):
            raise ValueError("invalid_email")
        value = value.strip().lower()
        if len(value) > 254 or re.search(r"[:/\\<>]", value) or not _EMAIL.fullmatch(value) or any(ord(c) < 32 for c in value):
            raise ValueError("invalid_email")
        return value

    def _persist(self):
        temporary = self._path.with_suffix(".tmp")
        payload = {"version": 1, "jobs": [{k: job[k] for k in _FIELDS} for job in self._jobs]}
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self._path)

    def _change(self, job, status, code=None, **fields):
        job.update(status=status, error_code=code, updated_at=time.time(), **fields)
        self._persist()
        self._cv.notify_all()

    def snapshot(self):
        with self._cv:
            return {"jobs": copy.deepcopy(self._jobs), "available": self._available,
                    "active_job_id": self._active, "max_jobs": self.max_jobs, "closed": self._closed, "error_code": self._error_code}

    def create(self, emails):
        if not isinstance(emails, list) or not emails:
            raise ValueError("invalid_emails")
        normalized = [self._email(email) for email in emails]
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate_email")
        with self._cv:
            if self._closed:
                raise ValueError("login_manager_closed")
            pending = [job for job in self._jobs if job["status"] not in _TERMINAL]
            if len(normalized) + len(pending) > self.max_jobs:
                raise ValueError("login_queue_full")
            if set(normalized) & {job["email"] for job in pending}:
                raise ValueError("duplicate_email")
            now = time.time()
            rows = [dict(id=uuid.uuid4().hex, email=email, status="QUEUED", created_at=now,
                         updated_at=now, deadline_at=None, begin_count=0, attempts=0,
                         code_length=None, error_code=None) for email in normalized]
            self._jobs.extend(rows)
            try:
                self._persist()
            except Exception:
                del self._jobs[-len(rows):]
                raise RuntimeError("login_state_write_failed") from None
            result = self.snapshot()
            self._cv.notify_all()
            return result

    def _job(self, job_id):
        job = next((item for item in self._jobs if item["id"] == job_id), None)
        if job is None:
            raise ValueError("login_job_not_found")
        return job

    def submit_code(self, job_id, code):
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{4,12}", code):
            raise ValueError("invalid_code_format")
        with self._cv:
            job = self._job(job_id)
            if self._closed or self._active != job_id or job["status"] != "WAITING_FOR_CODE":
                raise ValueError("login_job_not_waiting")
            if time.time() >= job["deadline_at"]:
                self._change(job, "EXPIRED", "CODE_EXPIRED")
                raise ValueError("login_code_expired")
            if job["code_length"] and len(code) != job["code_length"]:
                raise ValueError("invalid_code_format")
            previous = copy.deepcopy(job)
            self._codes[job_id] = code
            try:
                self._change(job, "VERIFYING", attempts=job["attempts"] + 1)
            except Exception:
                job.update(previous)
                self._codes.pop(job_id, None)
                raise RuntimeError("login_state_write_failed") from None
            return self.snapshot()

    def cancel(self, job_id):
        with self._cv:
            job = self._job(job_id)
            if job["status"] == "SAVING":
                raise ValueError("login_account_save_in_progress")
            if job["status"] not in _TERMINAL:
                previous, code = copy.deepcopy(job), self._codes.pop(job_id, None)
                try:
                    self._change(job, "CANCELLED", "USER_CANCELLED")
                except Exception:
                    job.update(previous)
                    if code is not None:
                        self._codes[job_id] = code
                    self._cv.notify_all()
                    raise RuntimeError("login_state_write_failed") from None
            return self.snapshot()

    def _begin(self, job):
        if self._driver is None and not self._driver_failed:
            try:
                self._driver = self._factory()
                if self._driver is None:
                    raise RuntimeError()
                self._available = True
            except Exception:
                self._available, self._driver_failed = False, True
        with self._cv:
            if self._closed or job["status"] != "STARTING":
                return
        try:
            result = {"status": "BROWSER_UNAVAILABLE"} if self._driver_failed else self._driver.begin(job["email"])
            if not isinstance(result, dict) or not isinstance(result.get("status"), str):
                result = {"status": "UNKNOWN"}
        except Exception:
            result = {"status": "UNKNOWN"}
        if result["status"] == "BROWSER_UNAVAILABLE":
            self._available, self._driver_failed = False, True
        with self._cv:
            if job["status"] != "STARTING" or self._closed:
                return
            status = result.get("status")
            if status == "WAITING_FOR_CODE":
                length = result.get("code_length")
                length = length if type(length) is int and 4 <= length <= 12 else None
                self._change(job, status, deadline_at=time.time() + self.otp_timeout, code_length=length)
            else:
                terminal = status if status in {"BLOCKED", "UNKNOWN", "EXPIRED"} else "FAILED"
                safe_code = status if status in {"BLOCKED", "UNKNOWN", "EXPIRED", "BROWSER_UNAVAILABLE", "ACCOUNT_NOT_FOUND", "INVALID_EMAIL"} else "LOGIN_BEGIN_FAILED"
                self._change(job, terminal, safe_code)

    def _verify(self, job, code):
        try:
            result = self._driver.verify(code)
        except Exception:
            result = {"status": "UNKNOWN"}
        finally:
            code = None
        with self._cv:
            if job["status"] != "VERIFYING" or self._closed:
                return
            status = result.get("status") if isinstance(result, dict) and isinstance(result.get("status"), str) else None
            if status == "INVALID_CODE" and job["attempts"] < 3:
                self._change(job, "WAITING_FOR_CODE", "INVALID_CODE")
                return
            if status != "AUTHENTICATED":
                terminal = status if status in {"BLOCKED", "UNKNOWN"} else "FAILED"
                self._change(job, terminal, "CODE_ATTEMPTS_EXHAUSTED" if status == "INVALID_CODE" else (terminal if status else "LOGIN_VERIFY_FAILED"))
                return
            if not isinstance(result.get("cookies"), dict) or not isinstance(result.get("expires"), dict) or not isinstance(result.get("principal_id"), str) or not result["principal_id"]:
                self._change(job, "UNKNOWN", "INVALID_AUTH_RESULT")
                return
            self._change(job, "SAVING")
        try:
            self._save(job["email"], result)
        except Exception:
            status, error = "FAILED", "SAVE_ACCOUNT_FAILED"
        else:
            status, error = "AUTHENTICATED", None
        finally:
            result = None
        with self._cv:
            self._change(job, status, error)

    def _process(self, job):
        self._begin(job)
        while True:
            with self._cv:
                if self._closed or job["status"] in _TERMINAL:
                    self._codes.pop(job["id"], None)
                    return
                if job["status"] == "WAITING_FOR_CODE":
                    remaining = job["deadline_at"] - time.time()
                    if remaining <= 0:
                        self._change(job, "EXPIRED", "CODE_EXPIRED")
                        continue
                    self._cv.wait(timeout=remaining)
                    continue
                code = self._codes.pop(job["id"], None)
                if job["status"] != "VERIFYING" or code is None:
                    self._change(job, "UNKNOWN", "LOGIN_STATE_INVALID")
                    continue
            self._verify(job, code)
            code = None

    def _close_session(self):
        if self._driver is not None:
            try:
                self._driver.close_session()
            except Exception:
                self._available, self._driver_failed = False, True

    def _run(self):
        try:
            while True:
                with self._cv:
                    if self._closed:
                        break
                    job = next((row for row in self._jobs if row["status"] == "QUEUED"), None)
                    if job is None:
                        self._cv.wait()
                        continue
                    self._active = job["id"]
                    self._change(job, "STARTING", begin_count=1)
                try:
                    self._process(job)
                finally:
                    self._close_session()
                    with self._cv:
                        self._active = None
        except Exception:
            with self._cv:
                self._closed, self._available = True, False
                self._codes.clear()
                for job in self._jobs:
                    if job["status"] not in _TERMINAL:
                        job.update(status="INTERRUPTED", error_code="LOGIN_WORKER_STOPPED", updated_at=time.time())
                try:
                    self._persist()
                except Exception:
                    pass
        finally:
            if self._driver is not None:
                try:
                    self._driver.close()
                except Exception:
                    pass

    def close(self):
        with self._cv:
            self._closed = True
            self._codes.clear()
            for job in self._jobs:
                if job["status"] not in _TERMINAL and job["status"] != "SAVING":
                    job.update(status="INTERRUPTED", error_code="MANAGER_CLOSED", updated_at=time.time())
            try:
                self._persist()
            except Exception:
                self._error_code = "LOGIN_STATE_WRITE_FAILED"
            finally:
                self._cv.notify_all()
        try:
            request_stop = getattr(self._driver, "request_stop", None)
            if callable(request_stop):
                request_stop()  # Optional thread-safe Event setter; no browser I/O.
        except Exception:
            pass
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=20)
