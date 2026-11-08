FROM python:3.12

WORKDIR /app

# Copy requirements first for better caching
COPY server/requirements.txt .
RUN pip install -r requirements.txt

# Agent runtime used by POST /agent. Installed non-editable under /opt because
# the compose command wipes /app/packages at startup.
COPY agent-loop /opt/agent-loop
RUN pip install --no-cache-dir /opt/agent-loop

# Return to app directory and copy server code
WORKDIR /app
COPY server .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
