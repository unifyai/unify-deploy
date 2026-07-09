ARG BASE_IMAGE=us-central1-docker.pkg.dev/gcp-project-runtime/unity/unity-base-staging:latest
FROM ${BASE_IMAGE}

USER root
COPY . /app/unity-deploy-src

ENV UNITY_DEPLOY_INSTALL_PROFILE=runtime
ENV UNITY_DEPLOY_CLIENT_MODE=bundled

RUN cd /app/unity-deploy-src && uv pip install --system --no-cache .

# Brain-operator code is delivered via the unify_company GCS client bundle
# (brain/unify_deploy submodule), not pip-installed into the shared image.
ARG BRAIN_OPERATOR_ASSISTANT_ID=
ARG BRAIN_OPERATOR_TASKS_ENABLED=false
ENV BRAIN_OPERATOR_ASSISTANT_ID=${BRAIN_OPERATOR_ASSISTANT_ID}
ENV BRAIN_OPERATOR_TASKS_ENABLED=${BRAIN_OPERATOR_TASKS_ENABLED}

RUN rm -rf /app/unity-deploy-src

USER unity
