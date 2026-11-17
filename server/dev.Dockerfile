FROM python:3.12

WORKDIR /app

# Copy requirements first for better caching
COPY server/requirements.txt .
RUN pip install -r requirements.txt

# Agent runtime used by POST /agent. Installed non-editable under /opt because
# the compose command wipes /app/packages at startup.
COPY agent-loop /opt/agent-loop
RUN pip install --no-cache-dir /opt/agent-loop

# Pre-download the BPE encodings used for context-budget accounting so the
# running container does not need outbound network on its first agent run.
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache
RUN python -c "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')"

# Return to app directory and copy server code
WORKDIR /app
COPY server .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
