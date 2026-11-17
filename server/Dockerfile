FROM python:3.12-slim

WORKDIR /app

ARG PIP_INDEX_URL=https://pypi.org/simple

COPY server/requirements.txt .

RUN pip install --no-cache-dir --retries 10 --timeout 120 --index-url "${PIP_INDEX_URL}" -r requirements.txt

# In-process agent runtime used by POST /agent.
COPY agent-loop /opt/agent-loop
RUN pip install --no-cache-dir /opt/agent-loop

# Pre-download the BPE encodings used for context-budget accounting so the
# running container does not need outbound network on its first agent run.
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache
RUN python -c "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')"

COPY server/ .

EXPOSE 8000

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
