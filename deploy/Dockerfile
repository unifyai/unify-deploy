ARG BASE_IMAGE=us-central1-docker.pkg.dev/gcp-project-runtime/unity/unity-base-staging:latest
FROM ${BASE_IMAGE}

USER root
COPY . /app/unity-deploy-src
RUN cd /app/unity-deploy-src && uv pip install --system --no-cache .

# Install the brain repo so the brain_operator deployment's function
# wrappers can `import brain.*` from inside the assistant runtime. The
# brain repo is cloned at the given ref (commit SHA in production
# triggers, branch name in staging) and installed editable so its data
# tree (docs, manifests, deck templates) is available to brain helpers
# at runtime.  See docs/operations/scheduled-jobs.md in the brain repo
# for the deployment contract.
#
# brain is private, so the build-arg GITHUB_TOKEN (sourced from the
# Cloud Build DEVBOT_GITHUB_TOKEN secret) is wired into git via
# ``url.<token>@github.com.insteadOf``. The matching ``--unset`` after
# the install scrubs the token out of /root/.gitconfig so it does not
# persist in the final image layer.
ARG BRAIN_REPO=https://github.com/unifyai/brain.git
ARG BRAIN_REF=main
ARG BRAIN_OPERATOR_ASSISTANT_ID=
ARG BRAIN_OPERATOR_TASKS_ENABLED=false
ARG GITHUB_TOKEN
RUN git config --global url."https://${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/" && \
    git clone --depth 1 --branch "${BRAIN_REF}" "${BRAIN_REPO}" /app/brain && \
    uv pip install --system --no-cache -e /app/brain && \
    git config --global --unset url."https://${GITHUB_TOKEN}@github.com/".insteadOf

ENV BRAIN_OPERATOR_ASSISTANT_ID=${BRAIN_OPERATOR_ASSISTANT_ID}
ENV BRAIN_OPERATOR_TASKS_ENABLED=${BRAIN_OPERATOR_TASKS_ENABLED}

# --- brain.social (Instagram/TikTok auto-publish) -----------------------
# The social pipeline's media generation drives marketing/images +
# marketing/videos, which need ffmpeg + a few extra Python deps. Those jobs
# ship DISABLED (see brain/social/scheduled_jobs.py) and on-pod generation
# also needs a writable scratch dir + media hosting, so the install is left
# commented until the pipeline is turned on. Uncomment to enable:
#
# USER root
# RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && \
#     rm -rf /var/lib/apt/lists/*
# RUN uv pip install --system --no-cache replicate elevenlabs Pillow

USER unity
