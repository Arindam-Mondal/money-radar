# syntax=docker/dockerfile:1

# One image for both api and worker; the service's command picks the role.
# Multi-arch: python and uv images ship linux/amd64 and linux/arm64, and every
# dependency has wheels for both, so the same Dockerfile builds on x86 and ARM.

# Same base in both stages: a venv's python is a symlink into the base image.
ARG PYTHON_IMAGE=python:3.12-slim-trixie
ARG UV_VERSION=0.11.17

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------- Stage 1: build the virtualenv ----------
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependencies first, so this layer is cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# ---------- Stage 2: runtime ----------
FROM ${PYTHON_IMAGE} AS runtime

# Fixed numeric UID/GID: Kubernetes runAsNonRoot can only verify a numeric USER,
# and volume permissions stay predictable across hosts.
ARG APP_UID=10001
RUN groupadd --system --gid ${APP_UID} app \
    && useradd --system --uid ${APP_UID} --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app app ./app

# Bytecode is precompiled at build time, so the container also runs with a read-only root fs.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER ${APP_UID}:${APP_UID}
EXPOSE 8000

# Exec form: uvicorn is PID 1 and receives SIGTERM directly, so shutdown is graceful.
# Default role = api; the worker service overrides this in docker-compose.yml.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
