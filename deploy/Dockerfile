FROM python:3.12-slim

ARG UNIFY_KEY
ARG GITHUB_TOKEN
ARG BRANCH=main

WORKDIR /app
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends git curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN git config --global url."https://${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"

ARG DEP_REPOS_SHA=unknown
RUN echo "dep_repos_sha=${DEP_REPOS_SHA}" && \
    DEP_BRANCH=$([ "$BRANCH" = "main" ] && echo "main" || echo "staging") && \
    git clone --depth 1 --branch $DEP_BRANCH https://github.com/unifyai/unity.git /unity-src && \
    git clone --depth 1 --branch $DEP_BRANCH https://github.com/unifyai/unify.git /unify && \
    git clone --depth 1 --branch $DEP_BRANCH https://github.com/unifyai/unillm.git /unillm

RUN chmod +x /unity-src/scripts/install-system-deps.sh && \
    /unity-src/scripts/install-system-deps.sh

ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get install -y nodejs
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/ && \
    mv /root/.local/bin/uvx /usr/local/bin/

RUN cp -r /unity-src/. /app/

RUN uv pip install --system --no-cache torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/cpu

RUN uv pip install --system --no-cache .

RUN git clone --depth 1 --branch unity-modifications \
    https://github.com/unifyai/magnitude.git /opt/magnitude
WORKDIR /opt/magnitude
RUN rm -f package-lock.json && npm install --ignore-scripts
RUN cd packages/magnitude-extract && npx tsup
RUN cd packages/magnitude-core && npx baml-cli generate && npx pkgroll
WORKDIR /app
RUN ln -sfn /opt/magnitude magnitude

RUN git config --global --unset url."https://${GITHUB_TOKEN}@github.com/".insteadOf

COPY . /app/unity-deploy-src
RUN cd /app/unity-deploy-src && uv pip install --system --no-cache .

RUN chmod +x /app/deploy/entrypoint.sh || true

WORKDIR /app/agent-service
RUN npm ci && npx tsc --skipLibCheck
WORKDIR /app

ENV PYTHONPATH=/app
ENV UNIFY_KEY=${UNIFY_KEY}
RUN install -m 0755 /app/scripts/sandbox-dpkg /usr/local/bin/sandbox-dpkg

ENV OMP_NUM_THREADS=1
ENV MKL_NUM_THREADS=1
RUN HF_HOME=/opt/hf-cache python unity/conversation_manager/medium_scripts/call.py download-files
RUN playwright install

ENV PYTHONUNBUFFERED=1
ENV TOKENIZERS_PARALLELISM=false
ENV UNITY_TERMINAL_LOG=true
ENV UNIFY_TERMINAL_LOG=false
ENV UNILLM_TERMINAL_LOG=false
ENV UNITY_LOG_DIR=/var/log/unity
ENV UNIFY_LOG_DIR=/var/log/unify
ENV UNILLM_LOG_DIR=/var/log/unillm
ENV MAGNITUDE_LOG_DIR=/var/log/magnitude
ENV MAGNITUDE_DEBUG=true

RUN useradd -m -u 1000 unity && \
    mkdir -p /var/log/unity /var/log/unify /var/log/unillm /var/log/magnitude /home/unity/.cache && \
    chown -R unity:unity /app /var/log/unity /var/log/unify /var/log/unillm /var/log/magnitude && \
    cp -r /root/.cache/ms-playwright /home/unity/.cache/ms-playwright && \
    chown -R unity:unity /home/unity/.cache

USER unity
EXPOSE 8000 3000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/bin/bash", "/app/deploy/entrypoint.sh"]
