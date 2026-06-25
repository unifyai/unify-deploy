# Self-hosted GitHub Actions runner image for the Integration smoke merge gate.
#
# Base provides the auto-registering runner loop (ACCESS_TOKEN -> ephemeral
# registration). We add the toolchain the integration suite needs: Python 3.12,
# uv, portaudio headers (audio deps build during uv sync), the gcloud CLI, and
# the GKE auth plugin. GCP/Kubernetes auth itself is wired at run time by the
# workflow (gcloud auth activate-service-account) and in-cluster config.
FROM myoung34/github-runner:ubuntu-noble

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.12 \
        python3.12-venv \
        python3.12-dev \
        portaudio19-dev \
        build-essential \
        curl \
        ca-certificates \
        gnupg \
        apt-transport-https \
    && rm -rf /var/lib/apt/lists/*

# Google Cloud CLI + GKE auth plugin (used for gcloud auth print-access-token
# and any kubectl fallback; in-cluster config covers the Kubernetes API).
RUN curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
        | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        google-cloud-cli \
        google-cloud-cli-gke-gcloud-auth-plugin \
        kubectl \
    && rm -rf /var/lib/apt/lists/*

# uv for dependency resolution (matches the repo's checked-in tooling flow).
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

ENV USE_GKE_GCLOUD_AUTH_PLUGIN=True
