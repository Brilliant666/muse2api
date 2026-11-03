#!/usr/bin/env python3
"""Prepare the pinned Muse protocol dependency without changing its source checkout.

Run with --clone to explicitly fetch the public repository, or --checkout PATH
for an existing checkout containing the fixed commit. The default destination is
.protocol/ at the application repository root. Existing destinations are refused.
This command applies verified patches; it does not install packages, start the
application, read credentials, or contact Muse.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile


SOURCE = {
    "repository": "https://github.com/its-benjamin/muse2api-http.git",
    "commit": "6309f64b44ce133a2428fadf966a395d503ce84f",
}
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEPENDENCY_DIR = REPOSITORY_ROOT / "protocol-dependency"
INCOMPLETE_MARKER = "PREPARATION_INCOMPLETE"
PREPARED_MARKER = "PREPARED_PROTOCOL.json"
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 25 * 1024 * 1024
FORBIDDEN_COMPONENTS = {".git", ".muse-state", "__pycache__", ".venv", "venv"}
RUNTIME_FILENAMES = {".env", "accounts.json", "tasks.json", "protocol_operations.json",
                     "cookies_muse.txt", "gateway.json"}


class PreparationError(RuntimeError):
    pass


def safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PreparationError("invalid relative path in manifest or Git archive")
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or str(path) != value or
        any(part in {"", ".", ".."} or ":" in part or part.endswith((".", " "))
            or any(ord(char) < 32 for char in part) for part in path.parts)):
        raise PreparationError("absolute or traversing path rejected")
    if any(re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
           for part in path.parts):
        raise PreparationError("reserved device filename rejected")
    if any(part.lower() in FORBIDDEN_COMPONENTS for part in path.parts):
        raise PreparationError("Git metadata, environment, or cache path rejected")
    if path.name.lower() in RUNTIME_FILENAMES:
        raise PreparationError("runtime data or credential filename rejected")
    return path


def under(root, relative):
    relative = safe_relative(relative)
    root = Path(root).resolve()
    target = root.joinpath(*relative.parts)
    try:
        target.resolve().relative_to(root)
    except ValueError:
        raise PreparationError("path resolves outside its allowed directory") from None
    return target


def expected_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PreparationError("manifest must contain lowercase SHA-256 file digests")
    return value


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def verify_files(root, mapping, label):
    if not isinstance(mapping, dict):
        raise PreparationError(label + " must be a path-to-SHA256 mapping")
    for relative, expected in mapping.items():
        path = under(root, relative)
        if path.is_symlink() or not path.is_file():
            raise PreparationError(label + " file is missing or is a symlink: " + relative)
        if digest(path) != expected_hash(expected):
            raise PreparationError(label + " hash mismatch: " + relative)


def git_environment():
    environment = os.environ.copy()
    # Archive the actual fixed object, independent of replacement refs or an
    # inherited worktree/index selector. Do not modify the caller's environment.
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR"):
        environment.pop(name, None)
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def run_git(arguments, *, cwd=None, isolated_apply=False):
    environment = git_environment()
    if isolated_apply:
        environment["GIT_CEILING_DIRECTORIES"] = str(Path(cwd).resolve().parent)
    result = subprocess.run(["git", *arguments], cwd=cwd, env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        # Do not print raw Git credential-helper or network diagnostics.
        action = arguments[0] if arguments else "command"
        raise PreparationError(f"git {action} failed (exit {result.returncode})")
    return result.stdout


def archive_fixed(checkout, commit, destination):
    checkout = Path(checkout).expanduser().resolve()
    if not checkout.is_dir():
        raise PreparationError("source checkout directory is missing")
    resolved = run_git(["rev-parse", "--verify", "--end-of-options", commit + "^{commit}"],
                       cwd=checkout).decode("ascii").strip()
    if resolved != commit:
        raise PreparationError("source commit did not resolve to the fixed baseline")
    archive = run_git(["archive", "--format=tar", commit], cwd=checkout)
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise PreparationError("Git archive exceeds the preparation size limit")
    if destination.is_symlink():
        raise PreparationError("archive destination must not be a symlink")
    if destination.exists():
        if (not destination.is_dir() or
            {item.name for item in destination.iterdir()} != {INCOMPLETE_MARKER}):
            raise PreparationError("archive destination is not an empty reserved output")
    else:
        destination.mkdir(parents=True, exist_ok=False)
    total = 0
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        for member in stream:
            relative = safe_relative(member.name.rstrip("/"))
            normalized = str(relative)
            if normalized in seen:
                raise PreparationError("duplicate archive member rejected")
            seen.add(normalized)
            target = under(destination, normalized)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise PreparationError("links and special archive entries are not permitted")
            total += member.size
            if member.size > MAX_FILE_BYTES or total > MAX_ARCHIVE_BYTES:
                raise PreparationError("archive extraction exceeds size limits")
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = stream.extractfile(member)
            if extracted is None:
                raise PreparationError("unable to read a Git archive member")
            with extracted, target.open("xb") as output:
                shutil.copyfileobj(extracted, output, length=1024 * 1024)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)


def validate_patch_paths(path, declared_paths):
    text = Path(path).read_text(encoding="utf-8")
    changes = 0
    current = None
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
            if not match or match.group(1) != match.group(2):
                raise PreparationError("unsupported or ambiguous Git patch path")
            current = str(safe_relative(match.group(1)))
            if current not in declared_paths:
                raise PreparationError("patch changes an undeclared output path: " + current)
            changes += 1
            continue
        if line.startswith("@@"):
            in_hunk = True
        if in_hunk:
            continue
        if line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            raise PreparationError("this kit format does not permit patch rename/copy operations")
        if line == "GIT binary patch":
            raise PreparationError("binary additions must use the verified overlay")
        if line.startswith(("new file mode ", "old mode ", "new mode ")) and line.endswith("120000"):
            raise PreparationError("patch-created symlinks are not permitted")
        if line.startswith(("--- ", "+++ ")):
            expected = ("a/" if line.startswith("--- ") else "b/") + (current or "")
            if current is None or line[4:] not in (expected, "/dev/null"):
                raise PreparationError("patch file header differs from declared path")
    if not changes:
        raise PreparationError("patch has no supported Git file changes")


def reject_symlink_components(path):
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise PreparationError("symlink path components are not permitted")


def load_manifest(directory=DEPENDENCY_DIR):
    directory = Path(directory)
    reject_symlink_components(directory)
    manifest_path = under(directory, "manifest.json")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise PreparationError("manifest must be a regular file")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
        raise PreparationError("unsupported protocol manifest format")
    if manifest.get("source") != SOURCE:
        raise PreparationError("source differs from the reviewed protocol repository and commit")
    for key in ("baseline_files", "output_files"):
        mapping = manifest.get(key)
        if not isinstance(mapping, dict) or not mapping:
            raise PreparationError("manifest requires a nonempty " + key + " mapping")
        for relative, expected in mapping.items():
            safe_relative(relative)
            expected_hash(expected)
    if set(manifest["baseline_files"]) != set(manifest["output_files"]):
        raise PreparationError("this dependency patch only changes existing declared files")
    patches = manifest.get("patches")
    if not isinstance(patches, list) or not patches:
        raise PreparationError("manifest requires patch descriptors")
    for patch in patches:
        if not isinstance(patch, dict):
            raise PreparationError("invalid patch descriptor")
        safe_relative(patch.get("path"))
        expected_hash(patch.get("sha256"))
    normalize_lf = manifest.get("normalize_lf", [])
    if (not isinstance(normalize_lf, list) or
        any(not isinstance(relative, str) for relative in normalize_lf) or
        len(normalize_lf) != len(set(normalize_lf))):
        raise PreparationError("normalize_lf must be a list of distinct paths")
    for relative in normalize_lf:
        safe_relative(relative)
        if relative not in manifest["baseline_files"]:
            raise PreparationError("normalized files must be declared in baseline_files")
    return manifest


def prepare(args, *, dependency_dir=DEPENDENCY_DIR):
    dependency_dir = Path(dependency_dir)
    manifest = load_manifest(dependency_dir)
    output = Path(args.destination).expanduser().absolute()
    reject_symlink_components(output)
    if output.exists() or output.is_symlink():
        raise PreparationError("destination already exists; choose a new directory")
    if args.clone:
        if args.checkout:
            raise PreparationError("--clone cannot be combined with --checkout")
    elif not args.checkout:
        raise PreparationError("provide --checkout or explicitly choose --clone")
    elif output.resolve().is_relative_to(Path(args.checkout).expanduser().resolve()):
        raise PreparationError("destination must be outside the input source checkout")

    declared = set(manifest["output_files"])
    for descriptor in manifest["patches"]:
        patch_path = under(dependency_dir, descriptor["path"])
        reject_symlink_components(patch_path)
        verify_files(dependency_dir, {descriptor["path"]: descriptor["sha256"]}, "patch")
        validate_patch_paths(patch_path, declared)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=False)
    marker = output / INCOMPLETE_MARKER
    marker.write_text("Preparation has not finished. Do not install or run this directory.\n", encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="muse-protocol-source-") as scratch:
        checkout = args.checkout
        if args.clone:
            checkout = Path(scratch) / "source"
            run_git(["clone", "--no-checkout", SOURCE["repository"], str(checkout)])
        archive_fixed(checkout, SOURCE["commit"], output)
        # Verify the Git blob bytes before any CRLF normalization. Only the
        # named, reviewed files are normalized, never the input checkout.
        verify_files(output, manifest["baseline_files"], "raw baseline")
        for relative in manifest.get("normalize_lf", []):
            target = under(output, relative)
            target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n"))
        for descriptor in manifest["patches"]:
            patch_path = under(dependency_dir, descriptor["path"])
            run_git(["apply", "--check", str(patch_path)], cwd=output, isolated_apply=True)
            run_git(["apply", "--whitespace=nowarn", str(patch_path)], cwd=output, isolated_apply=True)
        verify_files(output, manifest["output_files"], "prepared output")
    (output / PREPARED_MARKER).write_text(json.dumps({
        "format_version": 1, "source": SOURCE,
        "manifest_sha256": digest(dependency_dir / "manifest.json"),
        "output_files": manifest["output_files"],
    }, indent=2) + "\n", encoding="utf-8")
    marker.unlink()
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--checkout", help="existing Git checkout containing the fixed protocol commit")
    sources.add_argument("--clone", action="store_true", help="explicitly fetch the public protocol repository")
    parser.add_argument("--destination", default=str(REPOSITORY_ROOT / ".protocol"),
                        help="new output directory (default: repository/.protocol); must not exist")
    args = parser.parse_args(argv)
    try:
        output = prepare(args)
    except (PreparationError, OSError, ValueError, tarfile.TarError) as exc:
        print("Preparation failed: " + str(exc), file=sys.stderr)
        print("The source checkout was not modified. Any newly created destination remains incomplete; use a new path after fixing the error.", file=sys.stderr)
        return 1
    print("Prepared and verified protocol source: " + str(output))
    print("No packages were installed and no service was started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
