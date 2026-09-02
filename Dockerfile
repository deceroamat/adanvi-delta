FROM python:3.12-slim
# httptools (via uvicorn[standard]) no tiene wheel precompilado para
# linux/riscv64 en PyPI, asi que uv lo compila desde el sdist. Necesitamos
# un compilador C disponible para ese paso.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates \
    && curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
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
