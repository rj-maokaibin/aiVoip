ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/voip-live-acceptance-matplotlib

USER root
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends fontconfig fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p "$MPLCONFIGDIR" \
    && chmod 1777 "$MPLCONFIGDIR"

COPY backend/requirements.txt /opt/voip-live-acceptance/requirements.txt
RUN python -m pip install --no-cache-dir -r /opt/voip-live-acceptance/requirements.txt

WORKDIR /workspace
