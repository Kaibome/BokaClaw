# Sandboxed executor for shell tools and Agent Skill scripts.
#
# The executor only differs from the API image by a set of optional skill
# runtime packages. Extending the server image keeps the app code, pinned core
# dependencies, and the HTTP executor contract identical between the two.
ARG BASE_IMAGE=bokaclaw-server:test
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple

COPY server/executor-requirements.txt /tmp/executor-requirements.txt
RUN pip install --no-cache-dir --retries 10 --timeout 120 \
        --index-url "${PIP_INDEX_URL}" \
        -r /tmp/executor-requirements.txt \
    && rm -f /tmp/executor-requirements.txt

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8099

CMD ["uvicorn", "app.agent.executor_app:app", "--host", "0.0.0.0", "--port", "8099", "--no-access-log"]
