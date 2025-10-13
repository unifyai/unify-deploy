from datetime import datetime, timedelta
from fastapi import APIRouter, Form, HTTPException
from google.cloud import pubsub_v1, storage
from google.oauth2.service_account import Credentials
import json
import os
from .helpers import (
    setup_kubernetes_client,
    create_unity_job,
    delete_job,
    get_job_logs,
    suspend_job,
    create_external_service_for_job,
    get_service_external_ip,
    delete_service,
)
from communication.helpers import STAGING

router = APIRouter()

# Project ID from the existing codebase
PROJECT_ID = "gcp-project-runtime"
# Default region for Cloud Run jobs
DEFAULT_REGION = "us-central1"


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

        # Create the topic if it doesn't already exist
        try:
            publisher.create_topic(request={"name": topic_path})
        except Exception as e:
            if "already exists" not in str(e).lower():
                raise

        # Create or update the subscription with no expiration
        expiration_policy = pubsub_v1.types.ExpirationPolicy(ttl=None)

        try:
            subscriber.create_subscription(
                request={
                    "name": subscription_path,
                    "topic": topic_path,
                    "expiration_policy": expiration_policy,
                }
            )
        except Exception as e:
            if "already exists" in str(e).lower():
                # Ensure the subscription never expires
                subscription = pubsub_v1.types.Subscription(
                    name=subscription_path,
                    expiration_policy=expiration_policy,
                )
                update_mask = {"paths": ["expiration_policy.ttl"]}
                subscriber.update_subscription(
                    request={
                        "subscription": subscription,
                        "update_mask": update_mask,
                    }
                )
            else:
                raise

        return {
            "success": True,
            "message": "Topic and subscription ensured with no expiration",
            "topic_name": topic_path,
            "subscription_name": subscription_path,
            "project_id": PROJECT_ID,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to create topic and subscription: {str(e)}"
        )


# delete pubsub topic
@router.delete("/pubsub/topic")
async def delete_pubsub_topic(topic_name: str = Form(...)):
    """
    Delete a Google Cloud Pub/Sub topic with the assistant_id as the topic name.
    Subscriptions are explicitly deleted first to avoid orphaned resources.
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

        # Delete all subscriptions attached to the topic (if any)
        try:
            for subscription_name in publisher.list_topic_subscriptions(
                request={"topic": topic_path}
            ):
                try:
                    subscriber.delete_subscription(
                        request={"subscription": subscription_name}
                    )
                except Exception as sub_err:
                    # If the subscription was already deleted, continue
                    if "not found" not in str(sub_err).lower():
                        raise
        except Exception as list_err:
            # If the topic is not found, there are no subscriptions to delete
            if "not found" not in str(list_err).lower():
                raise

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


# create external service for a job
@router.post("/job/expose")
async def expose_job_service(
    job_name: str = Form(...),
    namespace: str = Form("default"),
    port: int = Form(3000),
    service_name: str = Form(""),
):
    """
    Create a LoadBalancer Service that exposes the Job's Pod externally on the given port.
    Returns the service name and current external IP status.
    """
    try:
        batch_api, core_api = setup_kubernetes_client()
        if not batch_api or not core_api:
            raise HTTPException(
                status_code=500, detail="Failed to connect to Kubernetes cluster"
            )

        name = service_name or f"unity-svc-{job_name}"
        svc = create_external_service_for_job(
            core_api=core_api,
            job_name=job_name,
            namespace=namespace,
            port=port,
            service_name=name,
        )
        if not svc:
            raise HTTPException(status_code=500, detail="Failed to create Service")

        ip_info = get_service_external_ip(core_api, name, namespace)
        return {
            "success": True,
            "service_name": name,
            "namespace": namespace,
            "port": port,
            "external": ip_info,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to expose service: {str(e)}"
        )


# get external ip for a service
@router.get("/job/service/ip")
async def get_job_service_ip(service_name: str, namespace: str = "default"):
    try:
        batch_api, core_api = setup_kubernetes_client()
        if not batch_api or not core_api:
            raise HTTPException(
                status_code=500, detail="Failed to connect to Kubernetes cluster"
            )

        ip_info = get_service_external_ip(core_api, service_name, namespace)
        return {
            "success": True,
            "service_name": service_name,
            "namespace": namespace,
            "external": ip_info,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get service IP: {str(e)}"
        )


# delete external service
@router.delete("/job/service")
async def delete_job_service(
    service_name: str = Form(...), namespace: str = Form("default")
):
    try:
        batch_api, core_api = setup_kubernetes_client()
        if not batch_api or not core_api:
            raise HTTPException(
                status_code=500, detail="Failed to connect to Kubernetes cluster"
            )

        ok = delete_service(core_api, service_name, namespace)
        if ok:
            return {
                "success": True,
                "message": f"Service deleted: {service_name}",
                "service_name": service_name,
            }
        raise HTTPException(
            status_code=500, detail=f"Failed to delete service: {service_name}"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to delete service: {str(e)}"
        )


# create kubernetes job
@router.post("/job/create")
async def create_kubernetes_job(
    namespace: str = Form("default"),
    image: str = Form(
        "us-central1-docker.pkg.dev/gcp-project-runtime/unity/unity:latest"
    ),
):
    """
    Create a Kubernetes Job for a Unity assistant.

    Args:
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
        timestamp_str = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        job_name = (
            f"unity-{timestamp_str}"
            if not STAGING
            else f"unity-{timestamp_str}-staging"
        )

        # Create the job
        job = create_unity_job(
            batch_api=batch_api,
            job_name=job_name,
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
                detail=f"Failed to create job for assistant: {job_name}",
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
    job_name: str = Form(...), namespace: str = Form("default")
):
    """
    Delete a Kubernetes Job for a Unity assistant.

    Args:
        job_name: Name of the job (required)
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
        success = delete_job(batch_api, job_name, namespace)

        if success:
            return {
                "success": True,
                "message": f"Job deleted successfully: {job_name}",
                "job_name": job_name,
                "namespace": namespace,
            }
        else:
            raise HTTPException(
                status_code=500, detail=f"Failed to delete job: {job_name}"
            )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete job: {str(e)}")


# start job via pubsub
@router.post("/job/start")
async def start_job(
    api_key: str = Form(...),
    medium: str = Form(...),
    assistant_id: str = Form(...),
    user_id: str = Form(...),
    user_name: str = Form(...),
    user_email: str = Form(...),
    assistant_name: str = Form(...),
    assistant_age: str = Form(...),
    assistant_region: str = Form(...),
    assistant_about: str = Form(...),
    user_number: str = Form(""),
    assistant_number: str = Form(""),
    assistant_email: str = Form(""),
    user_whatsapp_number: str = Form(""),
    voice_provider: str = Form(""),
    voice_id: str = Form(""),
):
    """
    Start a Unity assistant job by publishing job parameters to Pub/Sub topic.

    Args:
        api_key: API key for authentication (required)
        medium: The type of medium (required)
        assistant_id: Unique assistant identifier (required)
        user_id: Unique user identifier (required)
        user_name: User's name (required)
        user_email: User's email (required)
        assistant_name: Assistant's name (required)
        assistant_age: Assistant's age (required)
        assistant_region: Assistant's region (required)
        assistant_about: Assistant's about (required)
        user_number: User's phone number (optional, defaults to empty string)
        assistant_number: Assistant's phone number (optional, defaults to empty string)
        assistant_email: Assistant's email (optional, defaults to empty string)
        user_whatsapp_number: User's whatsapp number (optional, defaults to empty string)
        voice_provider: TTS provider (optional, defaults to empty string)
        voice_id: Voice ID (optional, defaults to empty string)
    """
    try:
        # Get credentials from environment variable
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        creds = Credentials.from_service_account_info(creds_json)

        # Initialize the publisher client
        publisher = pubsub_v1.PublisherClient(credentials=creds)

        # Create the topic path
        topic_path = publisher.topic_path(
            PROJECT_ID,
            "unity-startup" if not STAGING else "unity-startup-staging",
        )

        # Prepare the job data
        job_data = {
            "thread": "startup",
            "event": {
                "api_key": api_key,
                "medium": medium,
                "assistant_id": assistant_id,
                "user_id": user_id,
                "user_name": user_name,
                "user_email": user_email,
                "assistant_name": assistant_name,
                "assistant_age": assistant_age,
                "assistant_region": assistant_region,
                "assistant_about": assistant_about,
                "user_number": user_number,
                "assistant_number": assistant_number,
                "assistant_email": assistant_email,
                "user_whatsapp_number": user_whatsapp_number,
                "voice_provider": voice_provider,
                "voice_id": voice_id,
            },
        }

        # Convert to JSON string
        message_data = json.dumps(job_data).encode("utf-8")

        # Publish the message
        future = publisher.publish(topic_path, data=message_data)
        message_id = future.result()

        return {
            "success": True,
            "message": "Job start request published to Pub/Sub successfully",
            "message_id": message_id,
            "topic_path": topic_path,
            "assistant_id": assistant_id,
            "is_staging": bool(STAGING),
            "project_id": PROJECT_ID,
        }

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to publish job start request: {str(e)}"
        )


# stop kubernetes job
@router.post("/job/stop")
async def stop_job(job_name: str = Form(...), namespace: str = Form("default")):
    """
    Stop a Kubernetes Job for a Unity assistant.
    """
    try:
        # Initialize Kubernetes client
        batch_api, core_api = setup_kubernetes_client()
        if not batch_api or not core_api:
            raise HTTPException(
                status_code=500, detail="Failed to connect to Kubernetes cluster"
            )
        # Suspend the job
        success = suspend_job(batch_api, job_name, namespace)
        if success:
            return {
                "success": True,
                "message": f"Job suspended successfully: {job_name}",
            }
        else:
            raise HTTPException(
                status_code=500, detail=f"Failed to suspend job: {job_name}"
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to suspend job: {str(e)}")


# list kubernetes jobs
@router.get("/jobs")
async def list_kubernetes_jobs(namespace: str = "default", hours: int = 3):
    """
    List all Unity Kubernetes jobs in the namespace.

    Args:
        namespace: Kubernetes namespace (optional, defaults to "default")
        hours: Number of hours to filter jobs (optional, defaults to 3)
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
        job_items = list(
            filter(
                lambda job: (
                    datetime.now()
                    - datetime.strptime(
                        job.metadata.name.replace("unity-", "").replace("-staging", ""),
                        "%Y-%m-%d-%H-%M-%S",
                    )
                )
                < timedelta(hours=hours),
                jobs.items,
            )
        )
        print(f"Job items: {map(lambda job: job.metadata.name, job_items)}")

        job_list = []
        for job in job_items:
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


# get job logs
@router.get("/job/logs")
async def get_job_logs_endpoint(
    job_name: str, namespace: str = "default", tail_lines: int = 10
):
    """
    Get logs from a Kubernetes Job.

    Args:
        job_name: Name of the job (required)
        namespace: Kubernetes namespace (optional, defaults to "default")
        tail_lines: Number of lines to tail (optional, defaults to 10)
    """
    try:
        # Initialize Kubernetes client
        batch_api, core_api = setup_kubernetes_client()
        if not batch_api or not core_api:
            raise HTTPException(
                status_code=500, detail="Failed to connect to Kubernetes cluster"
            )

        # Get logs
        result = get_job_logs(
            core_api=core_api,
            job_name=job_name,
            namespace=namespace,
            tail_lines=tail_lines,
        )

        if result["success"]:
            return result
        else:
            raise HTTPException(status_code=404, detail=result["message"])

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get job logs: {str(e)}")


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
