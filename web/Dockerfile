# ── Stage 1: build (dev variant: has shell + pip, runs as root) ───────
# Digest-pinned - see api/Dockerfile for the refresh command.
FROM dhi.io/python:3.14-debian13-dev@sha256:ef2d366cd461af05a717f2c245875d3cd24941d0727b0a34192ff097b5d6f807 AS builder

WORKDIR /build
COPY requirements.txt .
# See api/Dockerfile: the venv bootstraps pip, so strip it before the
# runtime stage copies the venv. Assertions follow the strip.
RUN python -m venv --copies /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
 && /opt/venv/bin/python -c "import flask, requests, gunicorn" \
 && /opt/venv/bin/pip uninstall -y pip \
 && rm -rf /opt/venv/lib/python*/site-packages/pip* \
           /opt/venv/lib/python*/site-packages/setuptools* \
           /opt/venv/lib/python*/site-packages/pkg_resources \
           /opt/venv/lib/python*/site-packages/wheel* \
           /opt/venv/bin/pip* \
 && /opt/venv/bin/python -c "import flask, requests, gunicorn, gunicorn.app.wsgiapp" \
 && ! /opt/venv/bin/python -m pip --version >/dev/null 2>&1

# ── Stage 2: distroless runtime (nonroot 65532, no shell, no pip) ─────
FROM dhi.io/python:3.14-debian13@sha256:90dda111ab0b6667b9321d800e426120a9d4a39c73390825479caca4520e5fad

LABEL org.opencontainers.image.title="file-web" \
      org.opencontainers.image.description="Client File Host website"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY --from=builder --chown=65532:65532 /opt/venv /opt/venv
COPY --chown=65532:65532 *.py ./

USER 65532
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')"]

CMD ["gunicorn", "-w", "2", "--threads", "4", "--timeout", "120", \
     "--worker-tmp-dir", "/tmp", "-b", "0.0.0.0:5000", "web:app"]
