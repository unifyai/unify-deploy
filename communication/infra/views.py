from fastapi import APIRouter, Form, HTTPException
from google.cloud import pubsub_v1, run_v2
from google.iam.v1 import iam_policy_pb2, policy_pb2
from google.oauth2.service_account import Credentials
from google.protobuf.duration_pb2 import Duration
import json
import os
import asyncio


router = APIRouter()

# Project ID from the existing codebase
PROJECT_ID = "gcp-project-runtime"
# Default region for Cloud Run jobs
DEFAULT_REGION = "us-central1"


async def set_allow_unauthenticated_policy(resource_path: str, creds: Credentials):
    """
    Set IAM policy to allow unauthenticated access to a Cloud Run service.

    Args:
        resource_path: Full resource path (e.g., projects/.../services/... or projects/.../jobs/...)
        creds: Google Cloud credentials
    """
    try:
        services_client = run_v2.ServicesClient(credentials=creds)

        # Get current IAM policy
        try:
            request = iam_policy_pb2.GetIamPolicyRequest(resource=resource_path)
            policy = services_client.get_iam_policy(request=request)
            print("✅ Successfully retrieved current IAM policy")
        except Exception as e:
            print(f"❌ Failed to get IAM policy: {e}")
            print(
                "This likely means the service doesn't exist. Check the service list above."
            )
            return

        # Check if allUsers already has Cloud Run Invoker role
        invoker_binding = None
        for binding in policy.bindings:
            if binding.role == "roles/run.invoker":
                invoker_binding = binding
                break

        # Add allUsers to Cloud Run Invoker role
        if not invoker_binding:
            invoker_binding = policy_pb2.Binding(
                role="roles/run.invoker", members=["allUsers"]
            )
            policy.bindings.append(invoker_binding)
            print("➕ Added new binding for roles/run.invoker")
        elif "allUsers" not in invoker_binding.members:
            invoker_binding.members.append("allUsers")
            print("➕ Added allUsers to existing roles/run.invoker binding")
        else:
            print("✅ allUsers already has roles/run.invoker access")

        # Set the updated policy using the correct request approach
        try:
            request = iam_policy_pb2.SetIamPolicyRequest(
                resource=resource_path, policy=policy
            )
            services_client.set_iam_policy(request=request)
            print(
                "✅ Successfully updated IAM policy - service now allows unauthenticated access"
            )
        except Exception as e:
            print(f"❌ Failed to set IAM policy: {e}")
    except Exception as e:
        print(
            f"Warning: Could not set allow unauthenticated policy for service: {str(e)}"
        )
        # Don't fail the entire operation if IAM policy setting fails


# create pubsub topic
@router.post("/pubsub/topic")
async def create_pubsub_topic(assistant_id: str = Form(...)):
    """
    Create a Google Cloud Pub/Sub topic and subscription with the assistant_id as
    the name.
    """
    try:
        # Get credentials from environment variable
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        creds = Credentials.from_service_account_info(creds_json)

        # Initialize the publisher and subscriber clients
        publisher = pubsub_v1.PublisherClient(credentials=creds)
        subscriber = pubsub_v1.SubscriberClient(credentials=creds)

        # Create the topic path using the project ID and assistant ID
        topic_name = f"unity-{assistant_id}"
        topic_path = publisher.topic_path(PROJECT_ID, topic_name)
        subscription_path = subscriber.subscription_path(
            PROJECT_ID, f"{topic_name}-sub"
        )

        # Try to create the topic
        try:
            publisher.create_topic(request={"name": topic_path})
            subscriber.create_subscription(
                request={
                    "name": subscription_path,
                    "topic": topic_path,
                }
            )
            return {
                "success": True,
                "message": "Topic and subscription created successfully",
                "topic_name": topic_path,
                "subscription_name": subscription_path,
                "assistant_id": assistant_id,
                "project_id": PROJECT_ID,
            }
        except Exception as e:
            # Handle case where topic already exists
            if "already exists" in str(e).lower():
                return {
                    "success": True,
                    "message": "Topic and subscription already exist",
                    "topic_name": topic_path,
                    "subscription_name": subscription_path,
                    "assistant_id": assistant_id,
                    "project_id": PROJECT_ID,
                }
            else:
                raise e
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to create topic and subscription: {str(e)}"
        )


# delete pubsub topic
@router.delete("/pubsub/topic")
async def delete_pubsub_topic(assistant_id: str = Form(...)):
    """
    Delete a Google Cloud Pub/Sub topic with the assistant_id as the topic name.
    Note: Deleting a topic automatically deletes all subscriptions attached to it.
    """
    try:
        # Get credentials from environment variable
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        creds = Credentials.from_service_account_info(creds_json)

        # Initialize the publisher client
        publisher = pubsub_v1.PublisherClient(credentials=creds)

        # Create the topic path using the project ID and assistant ID
        topic_path = publisher.topic_path(PROJECT_ID, f"unity-{assistant_id}")

        # Delete the topic
        publisher.delete_topic(request={"topic": topic_path})

        return {
            "success": True,
            "message": f"Topic deleted successfully",
            "topic_name": topic_path,
            "assistant_id": assistant_id,
            "project_id": PROJECT_ID,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete topic: {str(e)}")


# create cloud run service
@router.post("/service/create")
async def create_cloudrun_service(
    api_key: str = Form(...),
    assistant_id: str = Form(...),
    user_name: str = Form(...),
    assistant_number: str = Form(...),
    user_number: str = Form(...),
    port: int = Form(8000),  # Default HTTP port
):
    """
    Create a Google Cloud Run service named unity-<assistant_id>.

    Args:
        api_key: The API key for the assistant
        assistant_id: The assistant ID (service will be unity-<assistant_id>)
        user_name: The user's name
        assistant_number: The assistant's phone number
        user_number: The user's phone number
        port: The port the service listens on (default: 8000)
    """
    try:
        # Get credentials from environment variable
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        creds = Credentials.from_service_account_info(creds_json)

        # Initialize the Cloud Run Services client
        services_client = run_v2.ServicesClient(credentials=creds)

        # Create the service name with unity- prefix
        service_name = f"unity-{assistant_id}"

        # Create the parent path
        parent = f"projects/{PROJECT_ID}/locations/{DEFAULT_REGION}"

        # Define the service configuration
        service = run_v2.Service(
            template=run_v2.RevisionTemplate(
                containers=[
                    run_v2.Container(
                        name="unity-1",
                        image=(
                            "us-central1-docker.pkg.dev/gcp-project-runtime"
                            "/unity/unity:latest"
                        ),
                        ports=[
                            run_v2.ContainerPort(
                                name="http1",
                                container_port=port,
                            )
                        ],
                        env=[
                            run_v2.EnvVar(
                                name="UNIFY_KEY",
                                value=api_key,
                            ),
                            run_v2.EnvVar(
                                name="UNIFY_BASE_URL",
                                value="https://api.unify.ai/v0",
                            ),
                            run_v2.EnvVar(
                                name="ASSISTANT_ID",
                                value=assistant_id,
                            ),
                            run_v2.EnvVar(
                                name="USER_NAME",
                                value=user_name,
                            ),
                            run_v2.EnvVar(
                                name="ASSISTANT_NUMBER",
                                value=assistant_number,
                            ),
                            run_v2.EnvVar(
                                name="USER_NUMBER",
                                value=user_number,
                            ),
                            run_v2.EnvVar(
                                name="USER_PHONE_NUMBER",
                                value=user_number,
                            ),
                            run_v2.EnvVar(
                                name="UNITY_COMMS_URL",
                                value_source=run_v2.EnvVarSource(
                                    secret_key_ref=run_v2.SecretKeySelector(
                                        secret="UNIFY_COMMS_URL",
                                        version="latest",
                                    )
                                ),
                            ),
                            run_v2.EnvVar(
                                name="LIVEKIT_SIP_URI",
                                value_source=run_v2.EnvVarSource(
                                    secret_key_ref=run_v2.SecretKeySelector(
                                        secret="LIVEKIT_SIP_URI", version="latest"
                                    )
                                ),
                            ),
                            run_v2.EnvVar(
                                name="LIVEKIT_URL",
                                value_source=run_v2.EnvVarSource(
                                    secret_key_ref=run_v2.SecretKeySelector(
                                        secret="LIVEKIT_URL", version="latest"
                                    )
                                ),
                            ),
                            run_v2.EnvVar(
                                name="LIVEKIT_API_KEY",
                                value_source=run_v2.EnvVarSource(
                                    secret_key_ref=run_v2.SecretKeySelector(
                                        secret="LIVEKIT_API_KEY", version="latest"
                                    )
                                ),
                            ),
                            run_v2.EnvVar(
                                name="LIVEKIT_API_SECRET",
                                value_source=run_v2.EnvVarSource(
                                    secret_key_ref=run_v2.SecretKeySelector(
                                        secret="LIVEKIT_API_SECRET",
                                        version="latest",
                                    )
                                ),
                            ),
                            run_v2.EnvVar(
                                name="DEEPGRAM_API_KEY",
                                value_source=run_v2.EnvVarSource(
                                    secret_key_ref=run_v2.SecretKeySelector(
                                        secret="DEEPGRAM_API_KEY", version="latest"
                                    )
                                ),
                            ),
                            run_v2.EnvVar(
                                name="CARTESIA_API_KEY",
                                value_source=run_v2.EnvVarSource(
                                    secret_key_ref=run_v2.SecretKeySelector(
                                        secret="CARTESIA_API_KEY", version="latest"
                                    )
                                ),
                            ),
                            run_v2.EnvVar(
                                name="ELEVEN_API_KEY",
                                value_source=run_v2.EnvVarSource(
                                    secret_key_ref=run_v2.SecretKeySelector(
                                        secret="ELEVEN_API_KEY", version="latest"
                                    )
                                ),
                            ),
                            run_v2.EnvVar(
                                name="OPENAI_API_KEY",
                                value_source=run_v2.EnvVarSource(
                                    secret_key_ref=run_v2.SecretKeySelector(
                                        secret="OPENAI_API_KEY", version="latest"
                                    )
                                ),
                            ),
                            run_v2.EnvVar(
                                name="ORCHESTRA_ADMIN_KEY",
                                value_source=run_v2.EnvVarSource(
                                    secret_key_ref=run_v2.SecretKeySelector(
                                        secret="ORCHESTRA_ADMIN_KEY", version="latest"
                                    )
                                ),
                            ),
                        ],
                        resources=run_v2.ResourceRequirements(
                            limits={"cpu": "2", "memory": "8Gi"}
                        ),
                    )
                ],
                timeout=Duration(seconds=3600),  # 1 hour timeout for services
                service_account=(
                    "service-account@example.iam.gserviceaccount.com"
                ),
                scaling=run_v2.RevisionScaling(
                    min_instance_count=0,
                    max_instance_count=1,
                ),
            ),
            traffic=[
                run_v2.TrafficTarget(
                    type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST,
                    percent=100,
                )
            ],
        )

        # Create the service
        operation = services_client.create_service(
            parent=parent, service=service, service_id=service_name
        )

        # Wait for the operation to complete
        result = operation.result()

        # Set allow unauthenticated policy
        await set_allow_unauthenticated_policy(
            f"{parent}/services/{service_name}", creds
        )

        return {
            "success": True,
            "message": f"Cloud Run service created successfully",
            "service_name": result.name,
            "service_url": result.uri,
            "assistant_id": assistant_id,
            "project_id": PROJECT_ID,
            "region": DEFAULT_REGION,
            "full_service_name": service_name,
            "port": port,
        }

    except Exception as e:
        # Handle case where service already exists or other errors
        if "already exists" in str(e).lower():
            return {
                "success": True,
                "message": f"Cloud Run service already exists",
                "service_name": f"projects/{PROJECT_ID}/locations/{DEFAULT_REGION}/services/unity-{assistant_id}",
                "assistant_id": assistant_id,
                "project_id": PROJECT_ID,
                "region": DEFAULT_REGION,
                "full_service_name": service_name,
            }
        else:
            raise HTTPException(
                status_code=500, detail=f"Failed to create Cloud Run service: {str(e)}"
            )


# delete cloud run service
@router.delete("/service/delete")
async def delete_cloudrun_service(assistant_id: str = Form(...)):
    """
    Delete a Google Cloud Run service with the assistant_id as the service name.
    """
    try:
        # Get credentials from environment variable
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        creds = Credentials.from_service_account_info(creds_json)

        # Initialize the Cloud Run Services client
        services_client = run_v2.ServicesClient(credentials=creds)

        # Create the service name with unity- prefix
        service_name = f"unity-{assistant_id}"
        service_path = (
            f"projects/{PROJECT_ID}/locations/{DEFAULT_REGION}/services/{service_name}"
        )

        # Delete the service
        operation = services_client.delete_service(name=service_path)

        # Wait for the operation to complete
        operation.result()

        return {
            "success": True,
            "message": f"Cloud Run service deleted successfully",
            "service_name": service_path,
            "assistant_id": assistant_id,
            "project_id": PROJECT_ID,
            "region": DEFAULT_REGION,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to delete Cloud Run service: {str(e)}"
        )
