#!/usr/bin/env python3
"""Fully decode local acceptance media without contacting any remote service.

Images are verified, reopened, and every frame is loaded with Pillow's truncated
image support disabled. Videos need ffprobe metadata and an error-fatal ffmpeg
decode of every video/audio stream to null. This tool does not infer that a file
came from Muse. It never downloads files or emits decoder stderr/file contents.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import warnings


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg", ".ts"}


class DecodeFailure(RuntimeError):
    def __init__(self, stage, message, *, exit_code=None, diagnostic=None):
        super().__init__(message)
        self.stage, self.exit_code = stage, exit_code
        self.diagnostic_sha256 = hashlib.sha256(diagnostic.encode()).hexdigest() if diagnostic else None


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def executable(value, fallback):
    selected = value or shutil.which(fallback)
    if selected and Path(selected).is_file():
        return str(Path(selected).resolve())
    raise FileNotFoundError(f"{fallback} is required; provide its local executable path")


def run(command, timeout, stage):
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise DecodeFailure(stage, f"{stage} exceeded the configured timeout") from exc
    if result.returncode:
        raise DecodeFailure(stage, f"{stage} rejected the input", exit_code=result.returncode,
                            diagnostic=result.stderr)
    return result.stdout


def decode_image(path):
    from PIL import Image, ImageFile

    ImageFile.LOAD_TRUNCATED_IMAGES = False
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image_format, width, height = image.format, *image.size
                frames = int(getattr(image, "n_frames", 1))
                if width < 1 or height < 1 or not 1 <= frames <= 10000:
                    raise DecodeFailure("image_decode", "invalid image dimensions or excessive frame count")
                modes = set()
                for index in range(frames):
                    image.seek(index)
                    image.load()
                    modes.add(image.mode)
                return {"format": image_format, "width": width, "height": height,
                        "frames_decoded": frames, "modes": sorted(modes),
                        "verify_passed": True, "full_decode_passed": True}
        except DecodeFailure:
            raise
        except Exception as exc:
            # Decoder messages can contain embedded metadata or private paths.
            raise DecodeFailure("image_decode", "Pillow could not verify and fully decode the image",
                                diagnostic=type(exc).__name__ + ": " + str(exc)) from None


def decode_video(path, ffprobe, ffmpeg, timeout):
    raw = run([ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
               "-show_streams", "-show_format", "-of", "json", "-i", str(path)], timeout, "ffprobe")
    try:
        metadata = json.loads(raw)
    except ValueError:
        raise DecodeFailure("ffprobe", "ffprobe did not return valid JSON") from None
    streams = [item for item in metadata.get("streams", []) if item.get("codec_type") == "video"]
    if not streams or any(int(s.get("width") or 0) < 1 or int(s.get("height") or 0) < 1 for s in streams):
        raise DecodeFailure("ffprobe", "no valid dimensioned video stream was found")
    durations = [metadata.get("format", {}).get("duration"), *(s.get("duration") for s in streams)]
    duration = next((float(v) for v in durations if v is not None and v != "N/A"), None)
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise DecodeFailure("ffprobe", "no positive finite video duration was found")
    progress = run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror",
                    "-err_detect", "explode", "-protocol_whitelist", "file,pipe", "-i", str(path),
                    "-map", "0:v", "-map", "0:a?", "-progress", "pipe:1", "-nostats", "-f", "null", "-"],
                   timeout, "ffmpeg_full_decode")
    fields = {}
    for line in progress.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value.strip()
    frames = int(fields.get("frame") or 0)
    if fields.get("progress") != "end" or frames < 1:
        raise DecodeFailure("ffmpeg_full_decode", "ffmpeg did not finish a non-empty full video decode")
    return {"container_format": metadata.get("format", {}).get("format_name"),
            "duration_seconds": duration,
            "video_streams": [{key: s.get(key) for key in
                              ("index", "codec_name", "width", "height", "pix_fmt", "avg_frame_rate", "duration")}
                             for s in streams],
            "frames_decoded": frames, "full_decode_passed": True,
            "decoded_audio_streams": sum(s.get("codec_type") == "audio" for s in metadata.get("streams", []))}


def check_file(path, args):
    item = {"file_name": path.name, "status": "FAIL"}
    started = time.monotonic()
    try:
        if path.is_symlink() or not path.is_file():
            raise DecodeFailure("input", "input must be a regular local file")
        path = path.resolve()
        before = path.stat()
        item.update(bytes=before.st_size, sha256=digest(path))
        if before.st_size < 1:
            raise DecodeFailure("input", "input file is empty")
        kind = args.kind
        if kind == "auto":
            kind = "video" if path.suffix.lower() in VIDEO_EXTENSIONS else "image"
        item["kind"] = kind
        if kind == "image":
            item.update(decode_image(path))
        else:
            ffprobe, ffmpeg = executable(args.ffprobe, "ffprobe"), executable(args.ffmpeg, "ffmpeg")
            item.update(decode_video(path, ffprobe, ffmpeg, args.timeout))
            item["decoder_versions"] = {
                "ffprobe": run([ffprobe, "-version"], 10, "ffprobe_version").splitlines()[0],
                "ffmpeg": run([ffmpeg, "-version"], 10, "ffmpeg_version").splitlines()[0],
            }
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise DecodeFailure("input", "file changed while decoding")
        item["status"] = "PASS"
    except (ImportError, FileNotFoundError) as exc:
        item.update(status="BLOCKED", error_type=type(exc).__name__, error="required local decoder is unavailable")
    except DecodeFailure as exc:
        item.update(error=str(exc), stage=exc.stage, exit_code=exc.exit_code)
        if exc.diagnostic_sha256:
            item["diagnostic_sha256"] = exc.diagnostic_sha256
    except (OSError, ValueError, TypeError) as exc:
        item.update(error_type=type(exc).__name__, error="local media validation failed")
    item["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return item


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="local media files or directories")
    parser.add_argument("--kind", choices=("auto", "image", "video"), default="auto")
    parser.add_argument("--ffprobe", help="local ffprobe executable; otherwise search PATH")
    parser.add_argument("--ffmpeg", help="local ffmpeg executable; otherwise search PATH")
    parser.add_argument("--timeout", type=float, default=300, help="seconds per video subprocess")
    parser.add_argument("--recursive", action="store_true", help="scan supplied directories recursively")
    parser.add_argument("--report", type=Path, help="optional metadata-only JSON report")
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 3600:
        parser.error("--timeout must be greater than 0 and at most 3600")
    files, ignored = [], 0
    extensions = (IMAGE_EXTENSIONS if args.kind == "image" else VIDEO_EXTENSIONS if args.kind == "video"
                  else IMAGE_EXTENSIONS | VIDEO_EXTENSIONS)
    for selected in args.inputs:
        if selected.is_dir():
            for path in sorted(selected.rglob("*") if args.recursive else selected.iterdir()):
                if path.is_file() and path.suffix.lower() in extensions:
                    files.append(path)
                elif path.is_file():
                    ignored += 1
        else:
            files.append(selected)
    results = [check_file(path, args) for path in dict.fromkeys(files)]
    status = ("BLOCKED" if not results else "FAIL" if any(r["status"] == "FAIL" for r in results)
              else "BLOCKED" if any(r["status"] == "BLOCKED" for r in results) else "PASS")
    report = {"status": status, "scope": "full local media decoding only; input provenance not asserted",
              "files_checked": len(results), "directory_files_ignored": ignored, "results": results}
    try:
        report["pillow_version"] = importlib.metadata.version("Pillow")
    except importlib.metadata.PackageNotFoundError:
        pass
    if not results:
        report["error"] = "no matching media files were found"
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_name(args.report.name + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(args.report)
    print(rendered, end="")
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[status]


if __name__ == "__main__":
    raise SystemExit(main())
