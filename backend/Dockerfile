FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/voip-ai-matplotlib
WORKDIR /app

# Domestic mirrors are preferred for dependency rebuilds. Debian security stays on
# the official security endpoint; any domestic mirror failure falls back to the
# image's original Debian sources instead of silently weakening package checks.
ARG DEBIAN_PRIMARY_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
RUN set -eux; \
    sources=/etc/apt/sources.list.d/debian.sources; \
    cp "$sources" /tmp/debian.sources.official; \
    . /etc/os-release; \
    if python -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1].rstrip("/") + "/dists/" + sys.argv[2] + "/InRelease", timeout=4).read(1)' "$DEBIAN_PRIMARY_MIRROR" "$VERSION_CODENAME"; then \
      mirror="${DEBIAN_PRIMARY_MIRROR%/}"; \
      sed -i "s#http://deb.debian.org/debian#${mirror}#g; s#https://deb.debian.org/debian#${mirror}#g" "$sources"; \
      echo "APT_PRIMARY_MIRROR=DOMESTIC url=$mirror"; \
    else \
      echo "APT_PRIMARY_MIRROR=UNREACHABLE fallback=OFFICIAL"; \
    fi; \
    if ! (apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=10 -o Acquire::https::Timeout=10 update \
      && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=10 -o Acquire::https::Timeout=10 install -y --no-install-recommends \
        ca-certificates curl tshark ffmpeg fontconfig fonts-noto-cjk tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim); then \
      echo "APT_PRIMARY_INSTALL=FAIL fallback=OFFICIAL"; \
      cp /tmp/debian.sources.official "$sources"; \
      rm -rf /var/lib/apt/lists/*; \
      apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 update; \
      DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 install -y --no-install-recommends \
        ca-certificates curl tshark ffmpeg fontconfig fonts-noto-cjk tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim; \
      echo "APT_FALLBACK_INSTALL=PASS source=OFFICIAL"; \
    fi; \
    rm -rf /var/lib/apt/lists/* /tmp/debian.sources.official; \
    mkdir -p "$MPLCONFIGDIR"; \
    chmod 1777 "$MPLCONFIGDIR"

COPY requirements.txt .
ARG PIP_PRIMARY_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_FALLBACK_INDEX_URL=https://pypi.org/simple
RUN --mount=type=cache,target=/root/.cache/pip \
    set -eux; \
    if pip install --retries 1 --timeout 10 --index-url "$PIP_PRIMARY_INDEX_URL" -r requirements.txt; then \
      echo "PIP_PRIMARY_INDEX=PASS"; \
    else \
      echo "PIP_PRIMARY_INDEX=FAIL fallback=OFFICIAL"; \
      pip install --retries 3 --timeout 30 --index-url "$PIP_FALLBACK_INDEX_URL" -r requirements.txt; \
      echo "PIP_FALLBACK_INDEX=PASS source=OFFICIAL"; \
    fi

COPY . .

# Rollout switches are intentionally after apt/pip. SHADOW -> CANARY -> DEFAULT
# must not invalidate the expensive dependency layers.
ENV PRELIMINARY_EVIDENCE_V2_COMPOSE=true PRELIMINARY_EVIDENCE_V2_PROJECT=false PRELIMINARY_EVIDENCE_V2_STRICT_VALIDATOR=true
ARG BUILD_REVISION=unknown
LABEL org.opencontainers.image.revision=$BUILD_REVISION
ENV PYTHONPATH=/app
