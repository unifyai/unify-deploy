"""Pure helpers for Unity GKE secret sync (no Kubernetes client imports)."""

GCP_SECRETS_PROJECT_ID = "gcp-project-runtime"

# (Secret Manager secret id, key written into the Kubernetes Secret)
UNITY_SECRET_KEYS_FROM_GCP = (
    ("LIVEKIT_SIP_URI", "LIVEKIT_SIP_URI"),
    ("LIVEKIT_URL", "LIVEKIT_URL"),
    ("LIVEKIT_API_KEY", "LIVEKIT_API_KEY"),
    ("LIVEKIT_API_SECRET", "LIVEKIT_API_SECRET"),
    ("DEEPGRAM_API_KEY", "DEEPGRAM_API_KEY"),
    ("CARTESIA_API_KEY", "CARTESIA_API_KEY"),
    ("ELEVEN_API_KEY", "ELEVEN_API_KEY"),
    ("OPENAI_API_KEY", "OPENAI_API_KEY"),
    ("OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
    ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    ("ANTICAPTCHA_KEY", "ANTICAPTCHA_KEY"),
    ("TAVILY_API_KEY", "TAVILY_API_KEY"),
    ("ORCHESTRA_ADMIN_KEY", "ORCHESTRA_ADMIN_KEY"),
    ("VERTEXAI_CREDENTIALS", "VERTEXAI_CREDENTIALS"),
)

ESO_MANAGED_ANNOTATION = "reconcile.external-secrets.io/data-hash"
COMM_SA_KEY_SECRET_NAME = "comm-sa-key"
GCP_SA_KEY_SM_SECRET = "gcp-sa-key"


def is_eso_managed_secret(metadata) -> bool:
    """Return True when External Secrets Operator owns this Secret."""
    if metadata is None:
        return False
    if isinstance(metadata, dict):
        annotations = metadata.get("annotations") or {}
    else:
        annotations = metadata.annotations or {}
    return ESO_MANAGED_ANNOTATION in annotations
