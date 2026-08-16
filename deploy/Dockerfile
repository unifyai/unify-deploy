ARG BASE_IMAGE=us-central1-docker.pkg.dev/gcp-project-runtime/unity/unity-base-staging:latest
FROM ${BASE_IMAGE}

USER root
COPY . /app/unity-deploy-src

ENV UNIFY_DEPLOY_INSTALL_PROFILE=runtime
ENV UNIFY_DEPLOY_CLIENT_MODE=bundled

RUN cd /app/unity-deploy-src && uv pip install --system --no-cache .

# The overlay welds two independently promoted repos: the base image's
# unify tree and this repo's unify_deploy package. If either moves without
# the other, the first thing to break is the offline-runner boot import
# chain — and a poisoned :latest takes down every task pod that pulls it.
# Import that chain here so an incoherent pairing fails the build before
# any tag exists, whichever repo promoted first.
RUN python3 -c "import unify_deploy.startup_config, unify_deploy.runtime_bootstrap, unify_deploy.client_bundle.fetch, unify.task_scheduler.offline_runner"

# Brain-operator code is delivered via the unify_company GCS client bundle
# (brain/unify_deploy submodule), not pip-installed into the shared image.
ARG BRAIN_OPERATOR_ASSISTANT_ID=
ENV BRAIN_OPERATOR_ASSISTANT_ID=${BRAIN_OPERATOR_ASSISTANT_ID}

RUN rm -rf /app/unity-deploy-src

USER unity
