FROM python:3.12-slim

WORKDIR /app

ARG PIP_INDEX_URL=https://pypi.org/simple

COPY server/eval-requirements.txt .

RUN pip install --no-cache-dir --retries 10 --timeout 120 --index-url "${PIP_INDEX_URL}" -r eval-requirements.txt

COPY server/ .

RUN useradd --create-home --uid 1000 eval && chown -R eval:eval /app
USER eval

# No Confident AI upload, no CLI telemetry.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    DEEPEVAL_TELEMETRY_OPT_OUT=1 \
    DEEPEVAL_CACHE_FOLDER=/tmp/deepeval

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "app.eval.healthcheck"]

CMD ["python", "-m", "app.eval.worker"]
