FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Capa de dependencias, cacheable entre builds de codigo.
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY migrations/ ./migrations/
COPY src/ ./src/

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["python", "-m", "src"]
