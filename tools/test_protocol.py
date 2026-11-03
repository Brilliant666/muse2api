#!/usr/bin/env python3
"""Run the offline protocol suites without using a real account or data folder.

Prepare the patched dependency first with tools/prepare_protocol.py. Override
its default .protocol location with --protocol-source or MUSE_PROTOCOL_SOURCE.
This command never installs dependencies, starts Chromium or contacts Muse.
It writes no repository files unless --report is explicitly supplied.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests" / "protocol"
sys.dont_write_bytecode = True
sys.path.insert(0, str(TESTS))
from _paths import validate_protocol_source

REQUIRED_IMPORTS = (
    "fastapi", "starlette", "anyio", "sniffio", "pydantic", "python_multipart",
    "requests", "urllib3", "websocket", "websockets", "cryptography",
)
EXPECTED_PYTHON = {"test_protocol_regressions": 28, "test_adapter_integration": 8,
                   "test_app_contract": 23, "test_composed_protocol": 2}
EXPECTED_JS = 6
CHILD = r'''
import importlib, json, sys, unittest
from pathlib import Path
names = json.loads(sys.argv[2])
loader = unittest.defaultTestLoader
suites = {name: loader.loadTestsFromName(name) for name in names}
counts = {name: suite.countTestCases() for name, suite in suites.items()}
result = unittest.TextTestRunner(verbosity=2 if sys.argv[3] == "1" else 1).run(
    unittest.TestSuite(suites.values()))
protocol = importlib.import_module("test_protocol_regressions")
import muse2api
source = Path(protocol.PROTOCOL_DIR).resolve()
loaded_source = Path(muse2api.__file__).resolve()
source_matches = loaded_source.is_relative_to(source)
summary = {
    "counts": counts, "expected_counts": names, "tests_run": result.testsRun,
    "failures": len(result.failures), "errors": len(result.errors),
    "skipped": len(result.skipped), "skip_reasons": [reason for _, reason in result.skipped],
    "curl_import_stub": protocol.CURL_STUB,
    "live_native_transport_tested": False, "live_muse_tested": False,
    "protocol_source": str(source), "loaded_protocol_source": str(loaded_source),
    "source_matches": source_matches,
    "failed_tests": [str(test) for test, _ in result.failures + result.errors],
}
summary["status"] = "PASS" if (result.wasSuccessful() and not result.skipped
    and counts == names and result.testsRun == sum(names.values()) and source_matches) else "FAIL"
Path(sys.argv[1]).write_text(json.dumps(summary, indent=2) + "\n")
raise SystemExit(0 if summary["status"] == "PASS" else 1)
'''


def missing_imports():
    missing = []
    for name in REQUIRED_IMPORTS:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    return missing


def execute(arguments, *, cwd, environment, verbose):
    result = subprocess.run(arguments, cwd=cwd, env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=120, check=False)
    if verbose or result.returncode:
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
    return result


def run(args):
    selected = Path(args.protocol_source or os.environ.get("MUSE_PROTOCOL_SOURCE") or ".protocol").expanduser()
    source = (selected if selected.is_absolute() else ROOT / selected).resolve()
    report = {"scope": "offline protocol validation only", "status": "BLOCKED",
              "live_muse_tested": False, "live_native_transport_tested": False}
    problems = []
    if sys.version_info < (3, 11):
        problems.append("Python 3.11 or newer is required")
    try:
        report["protocol_integrity"] = validate_protocol_source(source)
    except (ImportError, OSError, ValueError, KeyError) as exc:
        problems.append(str(exc))
    missing = missing_imports()
    if missing:
        problems.append("Missing real Python import dependencies: " + ", ".join(missing))
    node = shutil.which("node")
    if node is None:
        problems.append("Node.js is required for the six admin behavior cases")
    if problems:
        report["blockers"] = problems
        return 2, report

    with tempfile.TemporaryDirectory(prefix="muse-offline-validation-") as temporary:
        workspace = Path(temporary)
        environment = dict(os.environ)
        for name in list(environment):
            if name.startswith(("MUSE_", "MUSE2API_")) or name in {
                "MODEL_API_KEY", "META_API_KEY", "META_MODEL_API_KEY", "IMAGE_GATEWAY_TOKEN"
            }:
                del environment[name]
        environment.update({
            "MUSE_PROTOCOL_SOURCE": str(source), "MUSE_BACKEND": "protocol",
            "MUSE2API_HOME": str(workspace / "application"),
            "MUSE2API_PROFILE_ROOT": str(workspace / "profiles"),
            "MUSE2API_KEY": "offline-validation-synthetic-key",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        search = [str(TESTS / "network_guard"), str(TESTS), str(source), str(ROOT)]
        if os.environ.get("PYTHONPATH"):
            search.append(os.environ["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(search)
        python_report = workspace / "python-results.json"
        python = execute([sys.executable, "-c", CHILD, str(python_report),
                          json.dumps(EXPECTED_PYTHON), "1" if args.verbose else "0"],
                         cwd=workspace, environment=environment, verbose=args.verbose)
        if python_report.is_file():
            report["python"] = json.loads(python_report.read_text())
        else:
            report["python"] = {"status": "FAIL", "error": "Python suites did not finish with a report"}
        javascript = execute([node, str(TESTS / "test_admin_protocol.mjs")],
                             cwd=workspace, environment=environment, verbose=args.verbose)
        js_cases = sum(line.startswith("PASS ") and "admin protocol behaviors;" not in line
                       for line in javascript.stdout.splitlines())
        js_ok = javascript.returncode == 0 and js_cases == EXPECTED_JS
        report["javascript"] = {"status": "PASS" if js_ok else "FAIL",
                                 "passed": js_cases, "expected": EXPECTED_JS,
                                 "scope": "actual admin functions with DOM/network harness"}
        successful = python.returncode == 0 and report["python"].get("status") == "PASS" and js_ok
        report["status"] = "PASS" if successful else "FAIL"
        report["note"] = ("All successes apply to offline behavior. Native curl's import surface may be "
                          "substituted when unavailable; network peers are synthetic even when it is installed.")
        return (0 if successful else 1), report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol-source", help="already prepared protocol directory; defaults to .protocol")
    parser.add_argument("--report", type=Path, help="explicit optional JSON report destination")
    parser.add_argument("-v", "--verbose", action="store_true", help="include individual test output")
    args = parser.parse_args(argv)
    try:
        code, report = run(args)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        code, report = 2, {"status": "BLOCKED", "scope": "offline validation",
                           "error": str(exc), "live_muse_tested": False}
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered)
    print(rendered, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
