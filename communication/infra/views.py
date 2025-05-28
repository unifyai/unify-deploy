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
        job_name = f"unity-{assistant_id}"

        # Create the parent path
        parent = f"projects/{PROJECT_ID}/locations/{DEFAULT_REGION}"

        # Define the job configuration
        job = run_v2.Job(
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
        job_name = f"unity-{assistant_id}"
        job_path = f"projects/{PROJECT_ID}/locations/{DEFAULT_REGION}/jobs/{job_name}"

        if action == "start":
            # Execute the job
            operation = jobs_client.run_job(name=job_path)

            # Return immediately after starting the execution (don't wait for completion)
            # The job will run indefinitely until stopped
            return {
                "success": True,
                "message": f"Cloud Run job execution started successfully",
                "action": "start",
                "job_name": job_path,
                "operation_name": operation.name,
                "assistant_id": assistant_id,
                "project_id": PROJECT_ID,
                "region": DEFAULT_REGION,
            }

        elif action == "stop":
            # Get the executions client
            executions_client = run_v2.ExecutionsClient(credentials=creds)

            # List executions for this job
            executions = executions_client.list_executions(parent=job_path)

            cancelled_executions = []
            failed_cancellations = []
            for execution in executions:
                try:
                    # Try to cancel the execution - API will handle if it's already completed/failed/cancelled
                    cancel_operation = executions_client.cancel_execution(
                        name=execution.name
                    )
                    cancel_operation.result()  # Wait for cancellation to complete
                    cancelled_executions.append(execution.name)
                except Exception as cancel_error:
                    # If cancellation fails (e.g., already completed), just log and continue
                    failed_cancellations.append(
                        {"execution": execution.name, "error": str(cancel_error)}
                    )

            return {
                "success": True,
                "message": f"Cloud Run job stop operation completed",
                "action": "stop",
                "job_name": job_path,
                "cancelled_executions": cancelled_executions,
                "failed_cancellations": failed_cancellations,
                "assistant_id": assistant_id,
                "project_id": PROJECT_ID,
                "region": DEFAULT_REGION,
            }

    except Exception as e:
        # Handle various error cases
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Cloud Run job 'unity-{assistant_id}' not found. "
                    "Please create the job first."
                ),
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
        job_name = f"unity-{assistant_id}"
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
                "message": f"Cloud Run job 'unity-{assistant_id}' not found",
                "assistant_id": assistant_id,
                "project_id": PROJECT_ID,
                "region": DEFAULT_REGION,
            }

        # Get recent executions
        executions = executions_client.list_executions(parent=job_path)

        # Get the latest execution (most recent)
        latest_execution = None
        latest_execution_status = "no_executions"

        try:
            # Get the first execution from the list (they're ordered by creation time, newest first)
            executions_list = list(executions)
            if executions_list:
                latest_execution = executions_list[0]

                # Debug: Log what we're getting from the API
                print(f"DEBUG: Latest execution object: {latest_execution}")
                if hasattr(latest_execution, "conditions"):
                    print(f"DEBUG: Execution conditions: {latest_execution.conditions}")
                    for condition in latest_execution.conditions:
                        print(
                            f"DEBUG: Condition type: {condition.type}, status: {condition.status}"
                        )

                # Determine status of latest execution
                if (
                    hasattr(latest_execution, "conditions")
                    and latest_execution.conditions
                ):
                    for condition in latest_execution.conditions:
                        if condition.type == "Completed" and condition.status == "True":
                            latest_execution_status = "completed"
                            break
                        elif condition.type == "Failed" and condition.status == "True":
                            latest_execution_status = "failed"
                            break
                        elif (
                            condition.type == "Cancelled" and condition.status == "True"
                        ):
                            latest_execution_status = "cancelled"
                            break
                        elif (
                            condition.type == "Cancelling"
                            and condition.status == "True"
                        ):
                            latest_execution_status = "cancelling"
                            break
                    # If no terminal condition found, assume running
                    if latest_execution_status == "no_executions":
                        latest_execution_status = "running"
                else:
                    # No conditions available, assume running
                    latest_execution_status = "running"
        except Exception as status_error:
            print(f"Could not determine status for latest execution: {status_error}")
            latest_execution_status = "unknown"

        # Prepare latest execution info
        latest_execution_info = None
        if latest_execution:
            latest_execution_info = {
                "name": latest_execution.name,
                "status": latest_execution_status,
                "create_time": (
                    latest_execution.create_time.isoformat()
                    if hasattr(latest_execution, "create_time")
                    and latest_execution.create_time
                    else None
                ),
                "start_time": (
                    latest_execution.start_time.isoformat()
                    if hasattr(latest_execution, "start_time")
                    and latest_execution.start_time
                    else None
                ),
                "completion_time": (
                    latest_execution.completion_time.isoformat()
                    if hasattr(latest_execution, "completion_time")
                    and latest_execution.completion_time
                    else None
                ),
            }

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
            "latest_execution": latest_execution_info,
            "execution_status": latest_execution_status,
            "total_executions": (
                len(executions_list) if "executions_list" in locals() else 0
            ),
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
        job_name = f"unity-{assistant_id}"
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
