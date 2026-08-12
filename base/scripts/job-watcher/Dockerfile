FROM python:3.12-slim

ARG BRANCH=main

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY deploy/scripts/job-watcher/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# unisdk is public, so this clone is unauthenticated. A credential in a clone
# URL reaches the build log, and as a build argument it is recorded in the
# metadata of every layer that follows it.
RUN DEP_BRANCH=$([ "$BRANCH" = "main" ] && echo "main" || echo "staging") && \
    git clone --depth 1 --branch $DEP_BRANCH https://github.com/unifyai/unisdk.git /tmp/unify && \
    pip install --no-cache-dir /tmp/unify && \
    rm -rf /tmp/unify

COPY deploy/scripts/job-watcher/assistant_jobs_api.py .
COPY deploy/scripts/job-watcher/watcher.py .

CMD ["kopf", "run", "watcher.py", "--liveness=http://0.0.0.0:8080/healthz"]
