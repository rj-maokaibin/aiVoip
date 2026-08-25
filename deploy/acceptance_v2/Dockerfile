FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl git jq \
       python3 python3-venv python3-pip \
       nodejs npm \
       docker.io docker-compose-v2 \
       tshark \
       ffmpeg fontconfig fonts-noto-cjk \
    && tshark -v | head -n1 | grep -q '4.2.2' \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLCONFIGDIR=/tmp/matplotlib

LABEL org.opencontainers.image.title="VOIP Acceptance Runtime V2" \
      io.ruijie.voip.acceptance.contract="voip-acceptance-infrastructure-v2" \
      io.ruijie.voip.acceptance.version="2.0.0"

CMD ["bash"]
