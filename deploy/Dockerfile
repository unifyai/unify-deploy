ARG BASE_IMAGE=us-central1-docker.pkg.dev/gcp-project-runtime/unity/unity-staging:latest
FROM ${BASE_IMAGE}

USER root
COPY . /app/unity-deploy-src
RUN cd /app/unity-deploy-src && uv pip install --system --no-cache .
USER unity
