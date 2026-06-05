"""Registry of preview-aware Cloud Run services and Unity image hash blobs.

Each ``Service`` describes one Cloud Run service that participates in
the preview-environment workflow: its canonical hostname, the GCP
project and region it lives in, and the sibling repository whose
``feature/*`` push triggers a tagged build.

The ``UNITY_IMAGE_HASH_BLOB`` describes the GCS object that pins the
Unity container image for a preview-environment Comms App revision.

Hostnames are stored as ``host`` (no scheme, no path) so the preview
URL builder can prefix them with ``https://<slug>---`` to derive the
tagged Cloud Run URL. Some services have a separate client-facing URL
because their Cloud Run service is reached through a load balancer.
"""

from __future__ import annotations

from dataclasses import dataclass

PROJECT_RESPONSIVE_CITY = "gcp-project-runtime"
PROJECT_SAAS = "gcp-project-saas"
REGION_US_CENTRAL = "us-central1"
REGION_EU_WEST = "europe-west1"

PREVIEW_TAG_SEPARATOR = "---"


@dataclass(frozen=True)
class Service:
    """One Cloud Run service participating in the preview workflow.

    ``host`` is the canonical hostname returned by
    ``gcloud run services describe ... --format='value(status.url)'``,
    stripped of its scheme.  ``repo`` is the sibling repository whose
    push to ``feature/*`` deploys a tagged revision of this service.
    """

    name: str
    project: str
    region: str
    host: str
    repo: str
    description: str
    client_host_template: str | None = None
    client_path: str = ""

    def tagged_url(self, slug: str) -> str:
        """Return the tag-prefixed URL for a preview revision of this service."""
        return f"https://{slug}{PREVIEW_TAG_SEPARATOR}{self.host}"

    def canonical_url(self) -> str:
        """Return the un-tagged service URL (regular staging traffic)."""
        return f"https://{self.host}"

    def client_url(self, slug: str) -> str:
        """Return the URL clients should use to reach this preview service."""
        if self.client_host_template is None:
            return self.tagged_url(slug)
        return (
            f"https://{self.client_host_template.format(slug=slug)}{self.client_path}"
        )


SERVICES: tuple[Service, ...] = (
    Service(
        name="saas-web-app-redesign-staging",
        project=PROJECT_SAAS,
        region=REGION_US_CENTRAL,
        host="service.a.run.app",
        repo="console",
        description="Console (frontend; the URL you visit in the browser)",
    ),
    Service(
        name="orchestra-staging",
        project=PROJECT_SAAS,
        region=REGION_EU_WEST,
        host="service.a.run.app",
        repo="orchestra",
        description="Orchestra API",
        client_host_template="{slug}internal.example.com",
        client_path="/v0",
    ),
    Service(
        name="unity-comms-app-staging",
        project=PROJECT_RESPONSIVE_CITY,
        region=REGION_US_CENTRAL,
        host="service.a.run.app",
        repo="communication",
        description="Comms App (assistant runtime control plane)",
    ),
    Service(
        name="unity-adapters-staging",
        project=PROJECT_RESPONSIVE_CITY,
        region=REGION_US_CENTRAL,
        host="service.a.run.app",
        repo="communication",
        description="Adapters (Twilio/Meet/scheduled-task webhooks)",
    ),
)


def service_by_name(name: str) -> Service | None:
    """Look up a registered service by its Cloud Run name."""
    for service in SERVICES:
        if service.name == name:
            return service
    return None


@dataclass(frozen=True)
class UnityImageHashBlob:
    """The GCS blob that holds the Unity image hash for a preview slug.

    A preview-environment Comms App revision spawns assistant Jobs from
    ``{registry}/unity-staging:<hash>`` where ``<hash>`` is read from
    this per-slug blob.  The unity / unity-deploy preview pipelines
    upload to this blob name when a feature branch is built.
    """

    bucket: str = "unity-image-hash"

    def blob_name(self, slug: str) -> str:
        return f"image_hash_staging_{slug}.txt"

    def gs_uri(self, slug: str) -> str:
        return f"gs://{self.bucket}/{self.blob_name(slug)}"


UNITY_IMAGE_HASH_BLOB = UnityImageHashBlob()
