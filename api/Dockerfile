# ── Stage 1: build (dev variant: has shell + pip, runs as root) ───────
# Digest-pinned for immutability. To refresh after DHI publishes a patch:
#   docker pull dhi.io/python:3.14-debian13-dev
#   docker inspect --format='{{index .RepoDigests 0}}' dhi.io/python:3.14-debian13-dev
FROM dhi.io/python:3.14-debian13-dev@sha256:ef2d366cd461af05a717f2c245875d3cd24941d0727b0a34192ff097b5d6f807 AS builder

WORKDIR /build
COPY requirements.txt .

# --copies makes the venv independent of where python lives in the
# runtime image (DHI uses /usr/bin, python:slim used /usr/local).
# python -m venv bootstraps pip INTO the venv, and the whole venv is
# copied to the runtime stage - so pip must be stripped here or it ships.
# The final two lines are build-time assertions: imports still work after
# the strip, and pip is genuinely unreachable.
RUN python -m venv --copies /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
 && /opt/venv/bin/python -c "import fastapi, uvicorn, psycopg2, bcrypt, jwt, multipart" \
 && /opt/venv/bin/pip uninstall -y pip \
 && rm -rf /opt/venv/lib/python*/site-packages/pip* \
           /opt/venv/lib/python*/site-packages/setuptools* \
           /opt/venv/lib/python*/site-packages/pkg_resources \
           /opt/venv/lib/python*/site-packages/wheel* \
           /opt/venv/bin/pip* \
 && /opt/venv/bin/python -c "import fastapi, uvicorn, psycopg2, bcrypt, jwt, multipart" \
 && ! /opt/venv/bin/python -m pip --version >/dev/null 2>&1 \
 && mkdir -p /data

# ── Stage 2: distroless runtime (nonroot 65532, no shell, no pip) ─────
FROM dhi.io/python:3.14-debian13@sha256:90dda111ab0b6667b9321d800e426120a9d4a39c73390825479caca4520e5fad

LABEL org.opencontainers.image.title="file-api" \
      org.opencontainers.image.description="Client File Host API"

# No RUN steps are possible here - there is no shell. Nothing to patch
# (DHI ships patched), no user to create (already nonroot 65532), no pip
# to remove (the runtime variant has none).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY --from=builder --chown=65532:65532 /opt/venv /opt/venv
COPY --from=builder --chown=65532:65532 /data /data
COPY --chown=65532:65532 *.py ./

USER 65532
EXPOSE 8000

# Exec form is required - no shell to parse the string form.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
