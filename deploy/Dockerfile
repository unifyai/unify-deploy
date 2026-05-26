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
ARG BRAIN_REPO=https://github.com/unifyai/brain.git
ARG BRAIN_REF=main
RUN git clone --depth 1 --branch "${BRAIN_REF}" "${BRAIN_REPO}" /app/brain && \
    uv pip install --system --no-cache -e /app/brain

USER unity
