"""Paths for tests in the original repository layout.

The protocol source is prepared locally by tools/prepare_protocol.py and is
never copied into the committed test tree. An explicit source directory helps
validate the same tests against an already prepared checkout.
"""
import hashlib
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT
_configured = Path(os.environ.get("MUSE_PROTOCOL_SOURCE") or ".protocol").expanduser()
PROTOCOL_DIR = (_configured if _configured.is_absolute() else ROOT / _configured).resolve()


def validate_protocol_source(directory):
    directory = Path(directory).resolve()
    needed = ("__init__.py", "chat.py", "gateway.py", "media.py", "protocol.json")
    if not all((directory / "muse2api" / name).is_file() for name in needed):
        raise ImportError(
            "BLOCKED: patched protocol source is missing at " + str(directory)
            + "; run python tools/prepare_protocol.py --clone, or set "
            "MUSE_PROTOCOL_SOURCE to an already prepared protocol directory"
        )
    if (directory / "PREPARATION_INCOMPLETE").exists():
        raise ImportError("BLOCKED: protocol preparation is incomplete")
    manifest_path = ROOT / "protocol-dependency" / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    marker_path = directory / "PREPARED_PROTOCOL.json"
    if directory == (ROOT / ".protocol").resolve() and not marker_path.is_file():
        raise ImportError("BLOCKED: .protocol lacks its preparation marker; use tools/prepare_protocol.py")
    if marker_path.is_file():
        marker = json.loads(marker_path.read_text())
        if (marker.get("source") != manifest["source"] or
                marker.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()):
            raise ImportError("BLOCKED: prepared protocol marker does not match this repository manifest")
    hashes = {}
    for relative, expected in manifest["output_files"].items():
        target = (directory / relative).resolve()
        if not target.is_relative_to(directory) or not target.is_file():
            raise ImportError("BLOCKED: invalid or missing prepared file " + relative)
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            raise ImportError("BLOCKED: prepared protocol file differs from reviewed patch: " + relative)
        hashes[relative] = actual
    return {"prepared_marker": marker_path.is_file(), "patched_file_sha256": hashes}


def require_protocol_source():
    validate_protocol_source(PROTOCOL_DIR)
    return PROTOCOL_DIR
