from fastapi import APIRouter, Form, Request, HTTPException
from google.cloud import pubsub_v1, run_v2
from google.oauth2.service_account import Credentials
from google.protobuf.duration_pb2 import Duration
import json
import os


router = APIRouter()

# Project ID from the existing codebase
PROJECT_ID = "gcp-project-runtime"
# Default region for Cloud Run jobs
DEFAULT_REGION = "us-central1"


# create pubsub topic
@router.post("/pubsub/topic")
async def create_pubsub_topic(assistant_id: str = Form(...)):
    """
    Create a Google Cloud Pub/Sub topic with the assistant_id as the topic name.
    """
    try:
        # Get credentials from environment variable
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        creds = Credentials.from_service_account_info(creds_json)

        # Initialize the publisher client
        publisher = pubsub_v1.PublisherClient(credentials=creds)

        # Create the topic path using the project ID and assistant ID
        topic_path = publisher.topic_path(PROJECT_ID, assistant_id)

        # Create the topic
        topic = publisher.create_topic(request={"name": topic_path})

        return {
            "success": True,
            "message": f"Topic created successfully",
            "topic_name": topic.name,
            "assistant_id": assistant_id,
            "project_id": PROJECT_ID,
        }

    except Exception as e:
        # Handle case where topic already exists or other errors
        if "already exists" in str(e).lower():
            return {
                "success": True,
                "message": f"Topic already exists",
                "topic_name": f"projects/{PROJECT_ID}/topics/{assistant_id}",
                "assistant_id": assistant_id,
                "project_id": PROJECT_ID,
            }
        else:
            raise HTTPException(
                status_code=500, detail=f"Failed to create topic: {str(e)}"
            )


# delete pubsub topic
@router.delete("/pubsub/topic")
async def delete_pubsub_topic(assistant_id: str = Form(...)):
    """
    Delete a Google Cloud Pub/Sub topic with the assistant_id as the topic name.
    """
    try:
        # Get credentials from environment variable
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        creds = Credentials.from_service_account_info(creds_json)

        # Initialize the publisher client
        publisher = pubsub_v1.PublisherClient(credentials=creds)

        # Create the topic path using the project ID and assistant ID
        topic_path = publisher.topic_path(PROJECT_ID, assistant_id)

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


# create cloud run job
@router.post("/job/create")
async def create_cloudrun_job(assistant_id: str = Form(...)):
    """
    Create a Google Cloud Run job named unity_<assistant_id>.
    """
    try:
        # Get credentials from environment variable
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        creds = Credentials.from_service_account_info(creds_json)

        # Initialize the Cloud Run Jobs client
        jobs_client = run_v2.JobsClient(credentials=creds)

        # Create the job name with unity_ prefix
        job_name = f"unity_{assistant_id}"

        # Create the parent path
        parent = f"projects/{PROJECT_ID}/locations/{DEFAULT_REGION}"

        # Define the job configuration
        job = run_v2.Job(
            name=f"unity_{assistant_id}",
            labels={"cloud.googleapis.com/location": DEFAULT_REGION},
            template=run_v2.ExecutionTemplate(
                template=run_v2.TaskTemplate(
                    containers=[
                        run_v2.Container(
                            name="unity-1",
                            image=(
                                "us-central1-docker.pkg.dev/gcp-project-runtime"
                                "/unity/unity:latest"
                            ),
                            env=[
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
                                    name="TWILIO_ACCOUNT_SID",
                                    value_source=run_v2.EnvVarSource(
                                        secret_key_ref=run_v2.SecretKeySelector(
                                            secret="TWILIO_ACCOUNT_SID",
                                            version="latest",
                                        )
                                    ),
                                ),
                                run_v2.EnvVar(
                                    name="TWILIO_AUTH_TOKEN",
                                    value_source=run_v2.EnvVarSource(
                                        secret_key_ref=run_v2.SecretKeySelector(
                                            secret="TWILIO_AUTH_TOKEN", version="latest"
                                        )
                                    ),
                                ),
                                run_v2.EnvVar(
                                    name="TWILIO_API_SID",
                                    value_source=run_v2.EnvVarSource(
                                        secret_key_ref=run_v2.SecretKeySelector(
                                            secret="TWILIO_API_SID", version="latest"
                                        )
                                    ),
                                ),
                                run_v2.EnvVar(
                                    name="TWILIO_API_SECRET",
                                    value_source=run_v2.EnvVarSource(
                                        secret_key_ref=run_v2.SecretKeySelector(
                                            secret="TWILIO_API_SECRET", version="latest"
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
                                    name="UNIFY_KEY",
                                    value_source=run_v2.EnvVarSource(
                                        secret_key_ref=run_v2.SecretKeySelector(
                                            secret="ORCHESTRA_API_KEY", version="latest"
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
                            ],
                            resources=run_v2.ResourceRequirements(
                                limits={"cpu": "1", "memory": "2Gi"}
                            ),
                        )
                    ],
                    max_retries=0,
                    timeout=Duration(seconds=43200),
                    service_account=(
                        "service-account@example.iam.gserviceaccount.com"
                    ),
                )
            ),
        )

        # Create the job
        operation = jobs_client.create_job(parent=parent, job=job, job_id=job_name)

        # Wait for the operation to complete
        result = operation.result()

        return {
            "success": True,
            "message": f"Cloud Run job created successfully",
            "job_name": result.name,
            "assistant_id": assistant_id,
            "project_id": PROJECT_ID,
            "region": DEFAULT_REGION,
            "full_job_name": job_name,
        }

    except Exception as e:
        # Handle case where job already exists or other errors
        if "already exists" in str(e).lower():
            return {
                "success": True,
                "message": f"Cloud Run job already exists",
                "job_name": f"projects/{PROJECT_ID}/locations/{DEFAULT_REGION}/jobs/unity_{assistant_id}",
                "assistant_id": assistant_id,
                "project_id": PROJECT_ID,
                "region": DEFAULT_REGION,
                "full_job_name": f"unity_{assistant_id}",
            }
        else:
            raise HTTPException(
                status_code=500, detail=f"Failed to create Cloud Run job: {str(e)}"
            )


# control cloud run job execution (start/stop)
@router.post("/job/control")
async def control_cloudrun_job(
    assistant_id: str = Form(...), action: str = Form(...)  # "start" or "stop"
):
    """
    Control a Google Cloud Run job execution (start or stop).

    Args:
        assistant_id: The assistant ID (job will be unity_<assistant_id>)
        action: Either "start" to execute the job or "stop" to cancel running executions
    """
    try:
        # Validate action parameter
        if action not in ["start", "stop"]:
            raise HTTPException(
                status_code=400, detail="Action must be either 'start' or 'stop'"
            )

        # Get credentials from environment variable
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        creds = Credentials.from_service_account_info(creds_json)

        # Initialize the Cloud Run Jobs client
        jobs_client = run_v2.JobsClient(credentials=creds)

        # Create the job name with unity_ prefix
        job_name = f"unity_{assistant_id}"
        job_path = f"projects/{PROJECT_ID}/locations/{DEFAULT_REGION}/jobs/{job_name}"

        if action == "start":
            # Execute the job
            operation = jobs_client.run_job(name=job_path)

            # Wait for the operation to complete (this creates the execution)
            result = operation.result()

            return {
                "success": True,
                "message": f"Cloud Run job execution started successfully",
                "action": "start",
                "job_name": job_path,
                "execution_name": result.name,
                "assistant_id": assistant_id,
                "project_id": PROJECT_ID,
                "region": DEFAULT_REGION,
            }

        elif action == "stop":
            # First, we need to get the running executions
            executions_client = run_v2.ExecutionsClient(credentials=creds)

            # List executions for this job
            executions = executions_client.list_executions(parent=job_path)

            cancelled_executions = []
            for execution in executions:
                # Only cancel running executions
                if execution.status.conditions:
                    # Check if execution is still running (not completed, failed, or cancelled)
                    is_running = True
                    for condition in execution.status.conditions:
                        if condition.type == "Completed" and condition.status == "True":
                            is_running = False
                            break
                        elif condition.type == "Failed" and condition.status == "True":
                            is_running = False
                            break

                    if is_running:
                        try:
                            # Cancel the execution
                            cancel_operation = executions_client.cancel_execution(
                                name=execution.name
                            )
                            cancel_operation.result()  # Wait for cancellation to complete
                            cancelled_executions.append(execution.name)
                        except Exception as cancel_error:
                            # Continue with other executions even if one fails to cancel
                            print(
                                f"Failed to cancel execution {execution.name}: {cancel_error}"
                            )

            if cancelled_executions:
                return {
                    "success": True,
                    "message": f"Cloud Run job executions stopped successfully",
                    "action": "stop",
                    "job_name": job_path,
                    "cancelled_executions": cancelled_executions,
                    "assistant_id": assistant_id,
                    "project_id": PROJECT_ID,
                    "region": DEFAULT_REGION,
                }
            else:
                return {
                    "success": True,
                    "message": f"No running executions found to stop",
                    "action": "stop",
                    "job_name": job_path,
                    "cancelled_executions": [],
                    "assistant_id": assistant_id,
                    "project_id": PROJECT_ID,
                    "region": DEFAULT_REGION,
                }

    except Exception as e:
        # Handle various error cases
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=404,
                detail=f"Cloud Run job 'unity_{assistant_id}' not found. Please create the job first.",
            )
        else:
            raise HTTPException(
                status_code=500, detail=f"Failed to {action} Cloud Run job: {str(e)}"
            )


# get cloud run job status
@router.get("/job/status")
async def get_cloudrun_job_status(assistant_id: str):
    """
    Get the status of a Google Cloud Run job and its recent executions.

    Args:
        assistant_id: The assistant ID (job will be unity_<assistant_id>)
    """
    try:
        # Get credentials from environment variable
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        creds = Credentials.from_service_account_info(creds_json)

        # Initialize the Cloud Run clients
        jobs_client = run_v2.JobsClient(credentials=creds)
        executions_client = run_v2.ExecutionsClient(credentials=creds)

        # Create the job name with unity_ prefix
        job_name = f"unity_{assistant_id}"
        job_path = f"projects/{PROJECT_ID}/locations/{DEFAULT_REGION}/jobs/{job_name}"

        # Get job details
        try:
            job = jobs_client.get_job(name=job_path)
            job_exists = True
        except Exception:
            job_exists = False
            job = None

        if not job_exists:
            return {
                "success": True,
                "job_exists": False,
                "message": f"Cloud Run job 'unity_{assistant_id}' not found",
                "assistant_id": assistant_id,
                "project_id": PROJECT_ID,
                "region": DEFAULT_REGION,
            }

        # Get recent executions
        executions = executions_client.list_executions(parent=job_path)

        execution_statuses = []
        running_count = 0
        completed_count = 0
        failed_count = 0

        for execution in executions:
            status = "unknown"
            if execution.status.conditions:
                for condition in execution.status.conditions:
                    if condition.type == "Completed" and condition.status == "True":
                        status = "completed"
                        completed_count += 1
                        break
                    elif condition.type == "Failed" and condition.status == "True":
                        status = "failed"
                        failed_count += 1
                        break
                    elif condition.type == "Running" and condition.status == "True":
                        status = "running"
                        running_count += 1
                        break

            execution_statuses.append(
                {
                    "name": execution.name,
                    "status": status,
                    "create_time": (
                        execution.create_time.isoformat()
                        if execution.create_time
                        else None
                    ),
                    "start_time": (
                        execution.start_time.isoformat()
                        if execution.start_time
                        else None
                    ),
                    "completion_time": (
                        execution.completion_time.isoformat()
                        if execution.completion_time
                        else None
                    ),
                }
            )

        return {
            "success": True,
            "job_exists": True,
            "job_name": job_path,
            "assistant_id": assistant_id,
            "project_id": PROJECT_ID,
            "region": DEFAULT_REGION,
            "job_status": {
                "name": job.name,
                "create_time": job.create_time.isoformat() if job.create_time else None,
                "update_time": job.update_time.isoformat() if job.update_time else None,
            },
            "execution_summary": {
                "total_executions": len(execution_statuses),
                "running": running_count,
                "completed": completed_count,
                "failed": failed_count,
            },
            "recent_executions": execution_statuses[:10],  # Show last 10 executions
        }

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get Cloud Run job status: {str(e)}"
        )


# delete cloud run job
@router.delete("/job/delete")
async def delete_cloudrun_job(assistant_id: str = Form(...)):
    """
    Delete a Google Cloud Run job with the assistant_id as the job name.
    """
    try:
        # Get credentials from environment variable
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        creds = Credentials.from_service_account_info(creds_json)

        # Initialize the Cloud Run Jobs client
        jobs_client = run_v2.JobsClient(credentials=creds)

        # Create the job name with unity_ prefix
        job_name = f"unity_{assistant_id}"
        job_path = f"projects/{PROJECT_ID}/locations/{DEFAULT_REGION}/jobs/{job_name}"

        # Delete the job
        jobs_client.delete_job(name=job_path)

        return {
            "success": True,
            "message": f"Cloud Run job deleted successfully",
            "job_name": job_path,
            "assistant_id": assistant_id,
            "project_id": PROJECT_ID,
            "region": DEFAULT_REGION,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to delete Cloud Run job: {str(e)}"
        )
