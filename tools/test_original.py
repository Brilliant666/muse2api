#!/usr/bin/env python3
"""Run original root-level regression scripts with isolated data and no Muse I/O.

Each script gets a fresh process and temporary home. The media-selection case
starts its own headless Chromium on an ephemeral CDP port; it does not use a
logged-in browser. The optional report records exits and only parseable counts.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def environment(workspace, chromium):
    result = {key: value for key, value in os.environ.items()
              if not key.startswith(("MUSE_", "MUSE2API_")) and key not in {
                  "PYTHONPATH", "MODEL_API_KEY", "META_API_KEY", "META_MODEL_API_KEY",
                  "IMAGE_GATEWAY_TOKEN"}}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        cdp_port = listener.getsockname()[1]
    result.update({
        "MUSE2API_HOME": str(workspace / "application"),
        "MUSE2API_PROFILE_ROOT": str(workspace / "profiles"),
        "MUSE2API_HOME_DIR": str(workspace),
        "MUSE2API_KEY": "original-regression-synthetic-key",
        "MUSE2API_CONCURRENCY": "1", "MUSE_BACKEND": "browser",
        "MUSE2API_CDP_PORT": str(cdp_port),
        "PYTHONPATH": os.pathsep.join([str(TESTS / "protocol" / "network_guard"), str(ROOT)]),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
    })
    if chromium:
        result["MUSE2API_CHROMIUM"] = chromium
    return result


def execute(command, workspace, env, timeout):
    started = time.monotonic()
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
               else {"start_new_session": True})
    process = subprocess.Popen(command, cwd=workspace, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               errors="replace", **options)
    expired = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        expired = True
        # Terminate only this owned test process tree, including its isolated
        # browser if a case hangs before its own finally block.
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=15)
    result = {"status": "FAIL" if expired or process.returncode else "PASS",
              "exit_code": process.returncode, "timed_out": expired,
              "seconds": round(time.monotonic() - started, 3),
              "stdout": stdout, "stderr": stderr}
    combined = stdout + "\n" + stderr
    parsed = re.search(r"^Ran (\d+) tests? in ", combined, re.MULTILINE)
    if parsed:
        result["test_count"] = int(parsed.group(1))
        result["test_count_source"] = "unittest summary"
    else:
        parsed = re.search(r"^PASS[: ]+(\d+) (checks|[^\n]* cases)\b", stdout, re.MULTILINE)
        if parsed:
            result["test_count"] = int(parsed.group(1))
            result["test_count_source"] = "script assertion summary"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", default=os.environ.get("MUSE2API_CHROMIUM"),
                        help="headless Chromium/Chrome/Edge executable for media-selection")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--timeout", type=float, default=150, help="per-script seconds")
    parser.add_argument("--only", nargs="+", help="exact script names for a targeted rerun")
    args = parser.parse_args(argv)
    scripts = sorted(TESTS.glob("test_*.py"))
    javascript = TESTS / "test_admin_api_error_messages.mjs"
    if args.only:
        requested = set(args.only)
        available = {path.name for path in scripts} | {javascript.name}
        if requested - available:
            parser.error("unknown script: " + ", ".join(sorted(requested - available)))
        scripts = [path for path in scripts if path.name in requested]
    entries = []
    for path in scripts:
        with tempfile.TemporaryDirectory(prefix="muse-original-regression-", ignore_cleanup_errors=True) as home:
            workspace = Path(home)
            command = [sys.executable, str(path)]
            if path.name == "test_vm_wait.py":
                command.extend([str(ROOT / "engine.py"), "--assert"])
            try:
                result = execute(command, workspace, environment(workspace, args.chromium), args.timeout)
            except OSError as exc:
                result = {"status": "BLOCKED", "error": str(exc)}
            result["script"] = path.relative_to(ROOT).as_posix()
            entries.append(result)
            print(result["status"] + " " + result["script"], flush=True)
    if not args.only or javascript.name in args.only:
        node = shutil.which("node")
        if node:
            with tempfile.TemporaryDirectory(prefix="muse-original-js-") as home:
                workspace = Path(home)
                result = execute([node, str(javascript)], workspace,
                                 environment(workspace, args.chromium), args.timeout)
            result["test_count"] = len(re.findall(r"^PASS ", result["stdout"], re.MULTILINE))
            result["test_count_source"] = "individual JavaScript PASS lines"
        else:
            result = {"status": "BLOCKED", "error": "Node.js is not installed"}
        result["script"] = javascript.relative_to(ROOT).as_posix()
        entries.append(result)
        print(result["status"] + " " + result["script"], flush=True)
    status = "FAIL" if any(item["status"] == "FAIL" for item in entries) else (
        "BLOCKED" if any(item["status"] == "BLOCKED" for item in entries) else "PASS")
    report = {"status": status, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "original regressions; synthetic peers and isolated headless browser; no Muse account",
              "python": sys.version, "chromium": args.chromium,
              "live_muse_tested": False, "generation_calls_to_muse": 0,
              "python_scripts": len(scripts), "results": entries}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "scripts": len(entries)}, ensure_ascii=False))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
