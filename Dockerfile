# Build from this repository root: docker build -t muse2api-python-protocol .
FROM python:3.12-slim-bookworm AS protocol-source

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /prepare
COPY tools/prepare_protocol.py tools/prepare_protocol.py
COPY protocol-dependency/ protocol-dependency/
RUN python tools/prepare_protocol.py --clone --destination /prepared/muse-protocol

FROM python:3.12-slim-bookworm AS runtime

ARG INCLUDE_BROWSER=0
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    MUSE2API_HOME=/app \
    MUSE2API_HOST=0.0.0.0 \
    MUSE2API_PORT=18610 \
    MUSE_BACKEND=protocol \
    MUSE2API_CHROMIUM=/usr/bin/chromium

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && if [ "$INCLUDE_BROWSER" = "1" ]; then \
         apt-get install -y --no-install-recommends chromium fonts-wqy-zenhei fonts-freefont-ttf; \
       fi \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/app-requirements.txt
COPY --from=protocol-source /prepared/muse-protocol/ /opt/muse-protocol/
RUN pip install --no-cache-dir -r /tmp/app-requirements.txt /opt/muse-protocol
COPY . /app/
RUN mkdir -p /app/data/media
VOLUME ["/app/data"]
EXPOSE 18610
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18610", "--workers", "1"]
