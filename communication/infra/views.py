from fastapi import APIRouter, Form, HTTPException
from google.cloud import pubsub_v1, storage
from google.oauth2.service_account import Credentials
import json
import os
from .helpers import (
    setup_kubernetes_client,
    check_job_exists,
    create_unity_job,
    delete_job,
)

router = APIRouter()

# Project ID from the existing codebase
PROJECT_ID = "gcp-project-runtime"
# Default region for Cloud Run jobs
DEFAULT_REGION = "us-central1"
STAGING = os.getenv("STAGING")


# create pubsub topic
@router.post("/pubsub/topic")
async def create_pubsub_topic(topic_name: str = Form(...)):
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
async def delete_pubsub_topic(topic_name: str = Form(...)):
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
        topic_path = publisher.topic_path(PROJECT_ID, topic_name)

        # Delete the topic
        publisher.delete_topic(request={"topic": topic_path})

        return {
            "success": True,
            "message": f"Topic deleted successfully",
            "topic_name": topic_path,
            "project_id": PROJECT_ID,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete topic: {str(e)}")


# create kubernetes job
@router.post("/job/create")
async def create_kubernetes_job(
    api_key: str = Form(...),
    assistant_id: str = Form(...),
    user_name: str = Form(...),
    assistant_name: str = Form(...),
    user_number: str = Form(...),
    assistant_number: str = Form(""),
    user_phone_number: str = Form(""),
    namespace: str = Form("default"),
    image: str = Form(
        "us-central1-docker.pkg.dev/gcp-project-runtime/unity/unity:latest"
    ),
):
    """
    Create a Kubernetes Job for a Unity assistant.

    Args:
        api_key: API key for authentication (required)
        assistant_id: Unique assistant identifier (required)
        user_name: User's name (required)
        assistant_name: Assistant's name (required)
        user_number: User's phone number (required)
        assistant_number: Assistant's phone number (optional, defaults to empty string)
        user_phone_number: User's phone for calls (optional, defaults to user_number)
        namespace: Kubernetes namespace (optional, defaults to "default")
        image: Docker image to use (optional, defaults to latest unity image)
    """
    try:
        # Initialize Kubernetes client
        batch_api, core_api = setup_kubernetes_client()
        if not batch_api or not core_api:
            raise HTTPException(
                status_code=500,
                detail="Failed to connect to Kubernetes cluster. Make sure gcloud CLI is installed and configured.",
            )

        # Create the job name with unity- prefix
        job_name = (
            f"unity-{assistant_id}" if not STAGING else f"unity-{assistant_id}-staging"
        )

        # Check if job already exists and is running
        exists, status = check_job_exists(batch_api, job_name, namespace)
        if exists and status == "running":
            return {
                "success": True,
                "message": f"Assistant {assistant_id} is already running",
                "assistant_id": assistant_id,
                "namespace": namespace,
                "status": "already_running",
            }

        # Use user_phone_number if provided, otherwise use user_number
        phone_number = user_phone_number if user_phone_number else user_number

        # Create the job
        job = create_unity_job(
            batch_api=batch_api,
            api_key=api_key,
            assistant_id=assistant_id,
            job_name=job_name,
            user_name=user_name,
            assistant_name=assistant_name,
            user_number=user_number,
            assistant_number=assistant_number,
            user_phone_number=phone_number,
            namespace=namespace,
            image=image,
            is_staging=bool(STAGING),
        )

        if job:
            return {
                "success": True,
                "message": "Kubernetes job created successfully",
                "job_name": job.metadata.name,
                "job_uid": job.metadata.uid,
                "assistant_id": assistant_id,
                "namespace": namespace,
                "image": image,
                "creation_timestamp": (
                    job.metadata.creation_timestamp.isoformat()
                    if job.metadata.creation_timestamp
                    else None
                ),
            }
        else:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create job for assistant {assistant_id}",
            )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to create Kubernetes job: {str(e)}"
        )


# delete kubernetes job
@router.delete("/job/delete")
async def delete_kubernetes_job(
    assistant_id: str = Form(...), namespace: str = Form("default")
):
    """
    Delete a Kubernetes Job for a Unity assistant.

    Args:
        assistant_id: Unique assistant identifier (required)
        namespace: Kubernetes namespace (optional, defaults to "default")
    """
    try:
        # Initialize Kubernetes client
        batch_api, core_api = setup_kubernetes_client()
        if not batch_api or not core_api:
            raise HTTPException(
                status_code=500, detail="Failed to connect to Kubernetes cluster"
            )

        # Delete the job
        success = delete_job(batch_api, assistant_id, namespace)

        if success:
            return {
                "success": True,
                "message": f"Job deleted successfully: unity-{assistant_id}",
                "assistant_id": assistant_id,
                "namespace": namespace,
            }
        else:
            raise HTTPException(
                status_code=500, detail=f"Failed to delete job: unity-{assistant_id}"
            )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete job: {str(e)}")


# list kubernetes jobs
@router.get("/jobs")
async def list_kubernetes_jobs(namespace: str = "default"):
    """
    List all Unity Kubernetes jobs in the namespace.

    Args:
        namespace: Kubernetes namespace (optional, defaults to "default")
    """
    try:
        # Initialize Kubernetes client
        batch_api, core_api = setup_kubernetes_client()
        if not batch_api or not core_api:
            raise HTTPException(
                status_code=500, detail="Failed to connect to Kubernetes cluster"
            )

        # List jobs
        jobs = batch_api.list_namespaced_job(
            namespace=namespace, label_selector="app=unity"
        )

        job_list = []
        for job in jobs.items:
            assistant_id = job.metadata.labels.get("assistant-id", "unknown")
            status = "Unknown"

            if job.status.active:
                status = "Running"
            elif job.status.succeeded:
                status = "Completed"
            elif job.status.failed:
                status = "Failed"

            job_info = {
                "job_name": job.metadata.name,
                "assistant_id": assistant_id,
                "status": status,
                "creation_timestamp": (
                    job.metadata.creation_timestamp.isoformat()
                    if job.metadata.creation_timestamp
                    else None
                ),
                "active": job.status.active or 0,
                "succeeded": job.status.succeeded or 0,
                "failed": job.status.failed or 0,
            }
            job_list.append(job_info)

        return {
            "success": True,
            "jobs": job_list,
            "namespace": namespace,
            "total_jobs": len(job_list),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list jobs: {str(e)}")


# get latest unity image commit hash
@router.get("/image")
async def get_latest_unity_image_commit():
    """
    Get the commit hash of the latest Unity Docker image from a text file in Google Cloud Storage.

    Returns:
        JSON response with image details with commit hash
    """
    try:
        # Get credentials from environment variable
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        creds = Credentials.from_service_account_info(creds_json)

        # Initialize the Cloud Storage client
        storage_client = storage.Client(credentials=creds)

        # Define the bucket and file path
        bucket_name = "unity-image-hash"
        blob_name = "image_hash.txt" if not STAGING else "image_hash_staging.txt"

        try:
            # Get the bucket
            bucket = storage_client.bucket(bucket_name)

            # Get the blob (file)
            blob = bucket.blob(blob_name)

            # Check if the file exists
            if not blob.exists():
                raise HTTPException(
                    status_code=404,
                    detail=f"File {blob_name} not found in bucket {bucket_name}",
                )

            # Download and read the content
            content = blob.download_as_text()
            commit_hash = content.strip()

            # Get blob metadata
            blob.reload()

            return {"commit_hash": commit_hash}

        except HTTPException:
            raise
        except Exception as gcs_error:
            print(f"❌ Error accessing GCS: {gcs_error}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to read image hash from GCS: {str(gcs_error)}",
            )

    except HTTPException:
        raise
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(
            status_code=500, detail=f"Failed to get latest Unity image commit: {str(e)}"
        )
