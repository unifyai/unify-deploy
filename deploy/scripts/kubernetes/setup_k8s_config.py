#!/usr/bin/env python3
"""
Script to set up Kubernetes ConfigMaps and Secrets for Unity cluster.

Creates cluster-wide configuration that all Unity pods will use.
Only assistant-specific variables (ASSISTANT_ID, USER_FIRST_NAME, etc.) are set per pod.

API keys are read from GCP Secret Manager. Use --update to reconcile existing
unity-secrets from versions/latest, or install External Secrets Operator
(deploy/k8s/secrets/) for continuous sync.

Usage:
    python setup_k8s_config.py --create --namespace staging
    python setup_k8s_config.py --update --namespace staging
"""

import argparse
import base64
import sys
from pathlib import Path

from kubernetes import client
from kubernetes.client.rest import ApiException

from _k8s_lib import ensure_kube_config

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from unity_cluster_secrets import (
    COMM_SA_KEY_SECRET_NAME,
    GCP_SA_KEY_SM_SECRET,
    GCP_SECRETS_PROJECT_ID,
    UNITY_SECRET_KEYS_FROM_GCP,
    is_eso_managed_secret,
)


def fetch_gcp_secret_payload(secret_manager_client, sm_secret_id: str) -> bytes:
    """Read the latest enabled version of a Secret Manager secret."""
    secret_path = (
        f"projects/{GCP_SECRETS_PROJECT_ID}/secrets/{sm_secret_id}/versions/latest"
    )
    response = secret_manager_client.access_secret_version(
        request={"name": secret_path},
    )
    return response.payload.data


def build_unity_secrets_data(secret_manager_client) -> dict[str, str]:
    """Return base64-encoded secret data for unity-secrets from GCP."""
    secrets_data = {}
    for sm_name, k8s_key in UNITY_SECRET_KEYS_FROM_GCP:
        raw = fetch_gcp_secret_payload(secret_manager_client, sm_name)
        secrets_data[k8s_key] = base64.b64encode(raw).decode()
    return secrets_data


def upsert_namespaced_secret(
    api_client,
    namespace: str,
    manifest: dict,
    *,
    reconcile: bool,
) -> bool:
    """Create a Secret or replace it when reconcile=True."""
    name = manifest["metadata"]["name"]
    try:
        existing = api_client.read_namespaced_secret(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        api_client.create_namespaced_secret(namespace=namespace, body=manifest)
        print(f"✅ Created Secret '{name}'")
        return True

    if not reconcile:
        print(f"✅ Secret '{name}' already exists (use --update to reconcile from GCP)")
        return True

    if is_eso_managed_secret(existing.metadata):
        print(
            f"❌ Secret '{name}' is managed by External Secrets Operator; "
            "annotate the ExternalSecret to force-sync or wait for refreshInterval",
        )
        return False

    manifest["metadata"]["resource_version"] = existing.metadata.resource_version
    api_client.replace_namespaced_secret(name=name, namespace=namespace, body=manifest)
    print(f"✅ Reconciled Secret '{name}' from GCP Secret Manager")
    return True


def setup_kubernetes_client():
    """Initialize Kubernetes clients for the Batch and Core APIs.

    Returns the ``(BatchV1Api, CoreV1Api)`` pair the rest of this
    script expects, or ``(None, None)`` if cluster auth fails.
    """
    if not ensure_kube_config():
        return None, None
    return client.BatchV1Api(), client.CoreV1Api()


def create_namespace(api_client, namespace="default"):
    """Create the Unity namespace if it doesn't exist"""
    try:
        # Check if namespace exists
        try:
            api_client.read_namespace(name=namespace)
            print(f"✅ Namespace '{namespace}' already exists")
            return True
        except ApiException as e:
            if e.status == 404:
                # Namespace doesn't exist, create it
                namespace_manifest = {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {
                        "name": namespace,
                        "labels": {"name": namespace, "app": "unity"},
                    },
                }

                api_client.create_namespace(body=namespace_manifest)
                print(f"✅ Created namespace '{namespace}'")
                return True
            else:
                raise e

    except Exception as e:
        print(f"❌ Error creating namespace: {e}")
        return False


def create_global_configmap(api_client, namespace="default"):
    """Create global ConfigMap with cluster-wide constants"""
    try:
        configmap_manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "unity-config",
                "namespace": namespace,
                "labels": {"app": "unity"},
            },
            "data": {
                "PROJECT_ID": "gcp-project-runtime",
            },
        }

        configmap_name = configmap_manifest["metadata"]["name"]
        try:
            api_client.read_namespaced_config_map(
                name=configmap_name,
                namespace=namespace,
            )
            print(f"✅ ConfigMap '{configmap_name}' already exists")
            return True
        except ApiException as e:
            if e.status == 404:
                api_client.create_namespaced_config_map(
                    namespace=namespace,
                    body=configmap_manifest,
                )
                print(f"✅ Created ConfigMap '{configmap_name}'")
                return True
            raise

    except Exception as e:
        print(f"❌ Error creating global ConfigMap: {e}")
        return False


def create_global_secrets(api_client, namespace="default", *, reconcile: bool = False):
    """Create or reconcile unity-secrets and comm-sa-key from GCP Secret Manager."""
    try:
        from google.cloud import secretmanager

        sm_client = secretmanager.SecretManagerServiceClient()

        print("🔐 Fetching secrets from GCP Secret Manager...")
        try:
            secrets_data = build_unity_secrets_data(sm_client)
        except Exception as exc:
            print(f"   ❌ Failed to read required secrets: {exc}")
            return False

        for _sm_name, k8s_key in UNITY_SECRET_KEYS_FROM_GCP:
            print(f"   ✅ {k8s_key}")

        unity_manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "unity-secrets",
                "namespace": namespace,
                "labels": {"app": "unity"},
            },
            "type": "Opaque",
            "data": secrets_data,
        }

        if not upsert_namespaced_secret(
            api_client,
            namespace,
            unity_manifest,
            reconcile=reconcile,
        ):
            return False

        print("🔐 Reconciling comm-sa-key from GCP Secret Manager...")
        key_data = fetch_gcp_secret_payload(sm_client, GCP_SA_KEY_SM_SECRET)
        sa_key_manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": COMM_SA_KEY_SECRET_NAME,
                "namespace": namespace,
                "labels": {"app": "unity"},
            },
            "type": "Opaque",
            "data": {"key.json": base64.b64encode(key_data).decode()},
        }

        return upsert_namespaced_secret(
            api_client,
            namespace,
            sa_key_manifest,
            reconcile=reconcile,
        )

    except Exception as e:
        print(f"❌ Error creating secrets: {e}")
        return False


def create_service_account(api_client, namespace="default"):
    """Create the comm-sa service account if it doesn't exist"""
    try:
        service_account_manifest = {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": "comm-sa",
                "namespace": namespace,
                "labels": {"app": "unity"},
            },
        }

        # Check if ServiceAccount exists
        try:
            api_client.read_namespaced_service_account(
                name="comm-sa",
                namespace=namespace,
            )
            print("✅ ServiceAccount 'comm-sa' already exists")
            return True
        except ApiException as e:
            if e.status == 404:
                # Create the ServiceAccount
                api_client.create_namespaced_service_account(
                    namespace=namespace,
                    body=service_account_manifest,
                )
                print("✅ Created ServiceAccount 'comm-sa'")
                return True
            else:
                raise e

    except Exception as e:
        print(f"❌ Error creating ServiceAccount: {e}")
        return False


# Removed create_image_pull_secret function - not needed since cluster has permissions


def list_resources(api_client, namespace="default"):
    """List all Unity resources in the namespace"""
    try:
        print(f"📋 Unity Resources in namespace '{namespace}':")
        print()

        # List ConfigMaps
        print("🔧 ConfigMaps:")
        configmaps = api_client.list_namespaced_config_map(
            namespace=namespace,
            label_selector="app=unity",
        )
        for cm in configmaps.items:
            print(f"   - {cm.metadata.name}")
        if not configmaps.items:
            print("   (none)")
        print()

        # List Secrets
        print("🔐 Secrets:")
        secrets = api_client.list_namespaced_secret(
            namespace=namespace,
            label_selector="app=unity",
        )
        for secret in secrets.items:
            print(f"   - {secret.metadata.name}")
        if not secrets.items:
            print("   (none)")
        print()

        # List ServiceAccounts
        print("👤 ServiceAccounts:")
        service_accounts = api_client.list_namespaced_service_account(
            namespace=namespace,
            label_selector="app=unity",
        )
        for sa in service_accounts.items:
            print(f"   - {sa.metadata.name}")
        if not service_accounts.items:
            print("   (none)")
        print()

    except Exception as e:
        print(f"❌ Error listing resources: {e}")


def delete_resources(api_client, namespace="default"):
    """Delete all Unity resources in the namespace"""
    try:
        print(f"🗑️  Deleting Unity resources in namespace '{namespace}'...")

        # Delete ConfigMaps
        configmaps = api_client.list_namespaced_config_map(
            namespace=namespace,
            label_selector="app=unity",
        )
        for cm in configmaps.items:
            api_client.delete_namespaced_config_map(
                name=cm.metadata.name,
                namespace=namespace,
            )
            print(f"   ✅ Deleted ConfigMap: {cm.metadata.name}")

        # Delete Secrets
        secrets = api_client.list_namespaced_secret(
            namespace=namespace,
            label_selector="app=unity",
        )
        for secret in secrets.items:
            api_client.delete_namespaced_secret(
                name=secret.metadata.name,
                namespace=namespace,
            )
            print(f"   ✅ Deleted Secret: {secret.metadata.name}")

        # Delete ServiceAccounts
        service_accounts = api_client.list_namespaced_service_account(
            namespace=namespace,
            label_selector="app=unity",
        )
        for sa in service_accounts.items:
            api_client.delete_namespaced_service_account(
                name=sa.metadata.name,
                namespace=namespace,
            )
            print(f"   ✅ Deleted ServiceAccount: {sa.metadata.name}")

        print("✅ All Unity resources deleted")

    except Exception as e:
        print(f"❌ Error deleting resources: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Set up Kubernetes ConfigMaps and Secrets for Unity cluster",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python setup_k8s_config.py --create              # Create all resources
  python setup_k8s_config.py --list                # List existing resources
  python setup_k8s_config.py --delete              # Delete all resources
  python setup_k8s_config.py --update              # Update existing resources
        """,
    )

    parser.add_argument(
        "--create",
        action="store_true",
        help="Create all Kubernetes resources",
    )

    parser.add_argument("--list", action="store_true", help="List all Unity resources")

    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete all Unity resources",
    )

    parser.add_argument(
        "--update",
        action="store_true",
        help="Reconcile unity-secrets and comm-sa-key from GCP Secret Manager only",
    )

    parser.add_argument(
        "--namespace",
        default="default",
        help="Kubernetes namespace (default: default)",
    )

    args = parser.parse_args()

    print(f"🔧 Unity Kubernetes Configuration Setup")
    print(f"   Namespace: {args.namespace}")
    print()

    # Initialize Kubernetes client
    clients = setup_kubernetes_client()

    if not clients:
        print("❌ Failed to connect to Kubernetes cluster")
        sys.exit(1)

    batch_api, api_client = clients
    print("✅ Connected to Kubernetes cluster")

    # Handle different operations
    if args.list:
        list_resources(api_client, args.namespace)
        return

    if args.delete:
        confirm = input(
            "⚠️  Are you sure you want to delete all Unity resources? (y/N): ",
        )
        if confirm.lower() == "y":
            delete_resources(api_client, args.namespace)
        else:
            print("❌ Operation cancelled")
        return

    reconcile_secrets = args.update

    if args.create or reconcile_secrets:
        if reconcile_secrets and not args.create:
            print("🔐 Reconciling Unity secrets from GCP Secret Manager...")
        else:
            print("🚀 Setting up Unity Kubernetes resources...")

        if args.create:
            if not create_global_configmap(api_client, args.namespace):
                sys.exit(1)
            if not create_service_account(api_client, args.namespace):
                sys.exit(1)

        if not create_global_secrets(
            api_client,
            args.namespace,
            reconcile=reconcile_secrets,
        ):
            sys.exit(1)

        print("✅ Unity Kubernetes secrets reconciled successfully!")
        if args.create:
            print("✅ Unity Kubernetes bootstrap resources ensured!")
        print("\n💡 Next steps:")
        print("   1. Verify resources: python setup_k8s_config.py --list")
        print(
            "   2. Secret rotation: deploy/guides/UNITY_CLUSTER_SECRETS.md",
        )
        if reconcile_secrets:
            print(
                f"   3. Restart Unity jobs in '{args.namespace}' so pods pick up new env",
            )
        else:
            print(
                "   3. Refresh idle pool: python scripts/dev/idle_job_refresh.py "
                f"--env {args.namespace}",
            )
        return

    # Default: show help
    parser.print_help()


if __name__ == "__main__":
    main()
