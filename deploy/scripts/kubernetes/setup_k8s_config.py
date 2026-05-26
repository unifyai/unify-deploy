#!/usr/bin/env python3
"""
Script to set up Kubernetes ConfigMaps and Secrets for Unity cluster.

Creates cluster-wide configuration that all Unity pods will use.
Only assistant-specific variables (ASSISTANT_ID, USER_FIRST_NAME, etc.) are set per pod.

Usage:
    python setup_k8s_config.py --create
"""

import argparse
import base64
import sys

from kubernetes import client
from kubernetes.client.rest import ApiException

from _k8s_lib import ensure_kube_config


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

        # Check if ConfigMap exists
        try:
            api_client.read_namespaced_config_map(
                name="unity-global-config",
                namespace=namespace,
            )
            print("✅ Global ConfigMap already exists")
            return True
        except ApiException as e:
            if e.status == 404:
                # Create the ConfigMap
                api_client.create_namespaced_config_map(
                    namespace=namespace,
                    body=configmap_manifest,
                )
                print("✅ Created global ConfigMap")
                return True
            else:
                raise e

    except Exception as e:
        print(f"❌ Error creating global ConfigMap: {e}")
        return False


def create_global_secrets(api_client, namespace="default"):
    """Create global Secrets with API keys from GCP Secret Manager"""
    try:
        from google.cloud import secretmanager

        # Initialize Secret Manager client
        client = secretmanager.SecretManagerServiceClient()
        project_id = "gcp-project-runtime"

        secrets_data = {}
        required_secrets = [
            "LIVEKIT_SIP_URI",
            "LIVEKIT_URL",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
            "DEEPGRAM_API_KEY",
            "CARTESIA_API_KEY",
            "ELEVEN_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "ANTICAPTCHA_KEY",
            "UNITY_WEB_TAVILY_API_KEY",
            "ORCHESTRA_ADMIN_KEY",
            "VERTEXAI_CREDENTIALS",
            "SHARED_UNIFY_KEY",
        ]

        print("🔐 Fetching secrets from GCP Secret Manager...")
        for secret_name in required_secrets:
            try:
                # Construct the secret name
                secret_path = (
                    f"projects/{project_id}/secrets/{secret_name}/versions/latest"
                )

                # Access the secret version
                response = client.access_secret_version(request={"name": secret_path})
                secret_value = response.payload.data.decode("UTF-8")

                secrets_data[secret_name] = base64.b64encode(
                    secret_value.encode(),
                ).decode()
                print(f"   ✅ {secret_name}")

            except Exception as e:
                print(f"   ❌ {secret_name}: {e}")
                return False

        secret_manifest = {
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

        try:
            api_client.read_namespaced_secret(name="unity-secrets", namespace=namespace)
            print("✅ Application secrets already exist")
        except ApiException as e:
            if e.status == 404:
                api_client.create_namespaced_secret(
                    namespace=namespace,
                    body=secret_manifest,
                )
                print("✅ Created application secrets")
            else:
                raise e

        # Now create service account key secret
        print("🔐 Creating service account key secret...")
        try:
            # Fetch service account key from Secret Manager
            secret_path = f"projects/{project_id}/secrets/gcp-sa-key/versions/latest"
            response = client.access_secret_version(request={"name": secret_path})
            key_data = response.payload.data

            sa_key_manifest = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "comm-sa-key",
                    "namespace": namespace,
                    "labels": {"app": "unity"},
                },
                "type": "Opaque",
                "data": {"key.json": base64.b64encode(key_data).decode()},
            }

            try:
                api_client.read_namespaced_secret(
                    name="comm-sa-key",
                    namespace=namespace,
                )
                print("✅ Service account key secret already exists")
            except ApiException as e:
                if e.status == 404:
                    api_client.create_namespaced_secret(
                        namespace=namespace,
                        body=sa_key_manifest,
                    )
                    print("✅ Created service account key secret")
                else:
                    raise e

            return True

        except Exception as e:
            print(f"❌ Error creating service account key secret: {e}")
            return False

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
        help="Update existing resources",
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

    if args.create or args.update:
        print("🚀 Setting up Unity Kubernetes resources...")

        # Create namespace
        # if not create_namespace(api_client, args.namespace):
        #     sys.exit(1)

        # Create global ConfigMap
        if not create_global_configmap(api_client, args.namespace):
            sys.exit(1)

        # Create global Secrets
        if not create_global_secrets(api_client, args.namespace):
            sys.exit(1)

        # Create ServiceAccount
        if not create_service_account(api_client, args.namespace):
            sys.exit(1)

        # Service account key secret is now created in create_global_secrets

        # No image pull secret needed - cluster has permissions

        print("✅ All Unity Kubernetes resources created successfully!")
        print("\n💡 Next steps:")
        print("   1. Verify resources: python setup_k8s_config.py --list")
        print(
            "   2. Refresh the idle Job pool via the canonical production path:\n"
            "      python scripts/dev/idle_job_refresh.py --env staging",
        )
        print("   3. Check pool state: gcloud run services logs read ...")
        return

    # Default: show help
    parser.print_help()


if __name__ == "__main__":
    main()
