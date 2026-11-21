FROM python:3.12.13-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy \
    MPLCONFIGDIR=/tmp/matplotlib OMP_NUM_THREADS=2 OPENVINO_TELEMETRY=0
WORKDIR /opt/frontdoor
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.12.10
# This export excludes the project itself, so a version bump leaves this layer intact.
COPY requirements-runtime.txt ./
RUN --mount=type=cache,id=frontdoor-uv,target=/root/.cache/uv \
    uv venv .venv && uv pip sync --python .venv/bin/python --require-hashes requirements-runtime.txt
# Keep model retrieval independent of application code and other helper scripts.
COPY scripts/download_model.py ./scripts/download_model.py
RUN .venv/bin/python scripts/download_model.py /models
# run.sh imports app directly from this working directory; no editable install is needed.
COPY app ./app
COPY THIRD_PARTY.md ./
COPY run.sh /run.sh
RUN chmod +x /run.sh
EXPOSE 8080 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD .venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["/run.sh"]
# ARG affects subsequent RUN cache keys even when the command does not reference it.
ARG BUILD_VERSION=1.1.12
LABEL io.hass.name="Front Door AI" io.hass.description="Local doorway vision and reminders" \
      io.hass.version="${BUILD_VERSION}" io.hass.type="app" io.hass.arch="amd64"
