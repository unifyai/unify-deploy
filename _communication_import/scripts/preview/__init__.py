"""Preview-environment helper for feature-branch deployment testing.

Cloud Run revisions deployed by ``cloudbuild/*-preview.yaml`` triggers
on ``feature/*`` branches are tagged with a slugged branch name and
served from a tag-prefixed URL with no live-traffic share.  This
package wraps the ``gcloud run`` queries needed to inspect those
revisions, print the per-branch console URL, and clean up stale tagged
revisions when a feature lands.

Run as ``python -m scripts.preview <command>`` from the repo root.
"""
