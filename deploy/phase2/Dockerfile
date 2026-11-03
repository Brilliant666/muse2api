# Build the source image first from this worktree's root Dockerfile:
#   docker build -t muse2api-phase2:20261103-source -f Dockerfile .
# Then build this acceptance-only extension, without changing .dockerignore:
#   docker build -t muse2api-phase2:20261103-protocol -f deploy/phase2/Dockerfile .
# Neither image contains test accounts; give tests their own external data volume.
ARG PHASE2_SOURCE_IMAGE=muse2api-phase2:20261103-source
FROM ${PHASE2_SOURCE_IMAGE}

LABEL org.muse2api.acceptance.stage="phase2-linux"
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir Pillow==13.0.0

# Build-time checks verify Linux codec tools without starting a final container.
RUN python -c 'import json, platform, shutil, subprocess; from importlib.metadata import version; from PIL import Image; assert shutil.which("ffmpeg") and shutil.which("ffprobe"); print("PHASE2_ENVIRONMENT=" + json.dumps({"python": platform.python_version(), "system": platform.system(), "machine": platform.machine(), "packages": {name: version(name) for name in ("Pillow", "muse2api-http", "curl_cffi", "cryptography", "fastapi", "uvicorn")}, "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0], "ffprobe": subprocess.check_output(["ffprobe", "-version"], text=True).splitlines()[0]}))'
