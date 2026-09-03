FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/voip-ai-matplotlib
ENV PRELIMINARY_EVIDENCE_V2_COMPOSE=true PRELIMINARY_EVIDENCE_V2_PROJECT=false PRELIMINARY_EVIDENCE_V2_STRICT_VALIDATOR=true
WORKDIR /app
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl tshark ffmpeg fontconfig fonts-noto-cjk tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim && rm -rf /var/lib/apt/lists/* \
    && mkdir -p "$MPLCONFIGDIR" && chmod 1777 "$MPLCONFIGDIR"
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt
COPY . .
ARG BUILD_REVISION=unknown
LABEL org.opencontainers.image.revision=$BUILD_REVISION
ENV PYTHONPATH=/app
