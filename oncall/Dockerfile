FROM python:3.11-slim-bookworm

ARG GIT_SHA=""
LABEL org.opencontainers.image.revision=$GIT_SHA

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash --uid 10001 appuser

RUN python -m venv /opt/venv && pip install --no-cache-dir --upgrade pip

COPY Pipfile Pipfile.lock ./
RUN pip install --no-cache-dir pipenv && pipenv requirements > requirements.txt \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
USER appuser

HEALTHCHECK --interval=30s --timeout=2s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" > /dev/null || exit 1

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --timeout-keep-alive 75"]
