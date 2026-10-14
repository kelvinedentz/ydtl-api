FROM python:3.12-slim

# ffmpeg is required by yt-dlp for merging video/audio and converting to mp3/m4a
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Deno: the JS runtime yt-dlp uses to solve YouTube's signature/"n" challenges.
# It is found on PATH automatically (only deno is enabled by default).
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so this layer is cached until pyproject.toml or uv.lock change.
# pyproject.toml must list "yt-dlp[default]" so the challenge-solver scripts are installed.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src

RUN useradd --system --create-home app
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]