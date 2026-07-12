# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 twitchfarm

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DJANGO_DEBUG=0

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY manage.py ./
COPY twitch_farm/ twitch_farm/
COPY controller/ controller/

RUN mkdir -p /app/data /app/runtime /app/staticfiles \
    && DJANGO_DEBUG=0 \
       DJANGO_SECRET_KEY=container-build-only-not-for-runtime-0123456789-abcdef \
       TWITCH_FARM_CREDENTIAL_KEYS=MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA= \
       .venv/bin/python manage.py collectstatic --noinput \
    && chown -R twitchfarm:twitchfarm /app

USER twitchfarm

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "twitch_farm.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-"]
