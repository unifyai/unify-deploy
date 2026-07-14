# Optional local checkout of https://github.com/unifyai/brain
#
# Runtime / reconcile never read this path. The ``unify_company`` client
# bundle is published by brain CI on push to ``staging`` / ``main`` into
# ``gs://bucket/{staging|production}/unify_company/``.
#
# Clone here only when you need the local symlink
# ``unify_deploy/assistant_deployments/clients/unify_company`` for embedded
# (non-bundled) development / unit tests:
#
#   bash deploy/scripts/ensure_brain_checkout.sh
#   # or: BRAIN_REF=main bash deploy/scripts/ensure_brain_checkout.sh
#
# unify-deploy ``main`` checks out brain ``main``; other branches use
# brain ``staging``. Do not commit this tree — it is gitignored on purpose.
