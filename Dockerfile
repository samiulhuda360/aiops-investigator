FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY aiops ./aiops
COPY runbooks ./runbooks
COPY cache ./cache
RUN pip install --no-cache-dir .
ENV AIOPS_VAR=/data AIOPS_WORLD=/data/active AIOPS_STATE_DIR=/data/state AIOPS_TRACE_FILE=/data/traces.jsonl
