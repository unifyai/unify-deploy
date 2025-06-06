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


# create cloud run job
@router.post("/job/create")
async def create_cloudrun_job(
    assistant_id: str = Form(...),
    user_name: str = Form(...),
    assistant_number: str = Form(...),
    user_number: str = Form(...),
):
    """
    Create a Google Cloud Run job named unity_<assistant_id>.

    Args:
        assistant_id: The assistant ID (job will be unity_<assistant_id>)
        user_name: The user's name
        assistant_number: The assistant's phone number
        user_number: The user's phone number
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
            jobs_client.run_job(name=job_path)

            # Return immediately after starting the execution (don't wait for completion)
            # The job will run indefinitely until stopped
            return {
                "success": True,
                "message": f"Cloud Run job execution started successfully",
                "action": "start",
                "job_name": job_path,
                "assistant_id": assistant_id,
                "project_id": PROJECT_ID,
                "region": DEFAULT_REGION,
            }

        elif action == "stop":
            # Get the executions client
            executions_client = run_v2.ExecutionsClient(credentials=creds)

            # List executions for this job
            executions = executions_client.list_executions(parent=job_path)
            executions_list = list(executions)

            if not executions_list:
                return {
                    "success": True,
                    "message": "No executions found to cancel",
                    "action": "stop",
                    "job_name": job_path,
                    "cancelled_executions": [],
                    "failed_cancellations": [],
                    "assistant_id": assistant_id,
                    "project_id": PROJECT_ID,
                    "region": DEFAULT_REGION,
                }

            async def cancel_execution_async(execution):
                """Cancel a single execution asynchronously"""
                try:
                    # Run the synchronous cancel operation in a thread pool
                    loop = asyncio.get_event_loop()
                    cancel_operation = await loop.run_in_executor(
                        None,
                        lambda: executions_client.cancel_execution(name=execution.name),
                    )
                    # Wait for cancellation to complete
                    await loop.run_in_executor(None, cancel_operation.result)
                    return {"success": True, "execution": execution.name}
                except Exception as cancel_error:
                    return {
                        "success": False,
                        "execution": execution.name,
                        "error": str(cancel_error),
                    }

            # Cancel all executions concurrently
            cancellation_tasks = [
                cancel_execution_async(execution) for execution in executions_list
            ]
            cancellation_results = await asyncio.gather(
                *cancellation_tasks, return_exceptions=True
            )

            # Process results
            cancelled_executions = []
            failed_cancellations = []

            for result in cancellation_results:
                if isinstance(result, Exception):
                    failed_cancellations.append(
                        {"execution": "unknown", "error": str(result)}
                    )
                elif result["success"]:
                    cancelled_executions.append(result["execution"])
                else:
                    failed_cancellations.append(
                        {"execution": result["execution"], "error": result["error"]}
                    )

            return {
                "success": True,
                "message": f"Cloud Run job stop operation completed",
                "action": "stop",
                "job_name": job_path,
                "cancelled_executions": cancelled_executions,
                "failed_cancellations": failed_cancellations,
                "total_executions": len(executions_list),
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


# create cloud run service
@router.post("/service/create")
async def create_cloudrun_service(
    assistant_id: str = Form(...),
    user_name: str = Form(...),
    assistant_number: str = Form(...),
    user_number: str = Form(...),
    port: int = Form(8000),  # Default HTTP port
):
    """
    Create a Google Cloud Run service named unity-<assistant_id>.

    Args:
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
                            limits={"cpu": "1", "memory": "4Gi"}
                        ),
                    )
                ],
                timeout=Duration(seconds=3600),  # 1 hour timeout for services
                service_account=(
                    "service-account@example.iam.gserviceaccount.com"
                ),
                scaling=run_v2.RevisionScaling(
                    min_instance_count=0,
                    max_instance_count=10,
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


# update cloud run service traffic
@router.post("/service/control")
async def control_cloudrun_service(
    assistant_id: str = Form(...),
    action: str = Form(...),  # "update_traffic", "scale"
    traffic_percent: int = Form(100),  # For traffic updates
    min_instances: int = Form(0),  # For scaling
    max_instances: int = Form(10),  # For scaling
):
    """
    Control a Google Cloud Run service (update traffic allocation or scaling).

    Args:
        assistant_id: The assistant ID (service will be unity-<assistant_id>)
        action: Either "update_traffic" or "scale"
        traffic_percent: Percentage of traffic to latest revision (0-100)
        min_instances: Minimum number of instances
        max_instances: Maximum number of instances
    """
    try:
        # Validate action parameter
        if action not in ["update_traffic", "scale"]:
            raise HTTPException(
                status_code=400,
                detail="Action must be either 'update_traffic' or 'scale'",
            )

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

        # Get current service configuration
        current_service = services_client.get_service(name=service_path)

        if action == "update_traffic":
            # Validate traffic percent
            if not 0 <= traffic_percent <= 100:
                raise HTTPException(
                    status_code=400, detail="Traffic percent must be between 0 and 100"
                )

            # Update traffic allocation
            current_service.traffic = [
                run_v2.TrafficTarget(
                    type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST,
                    percent=traffic_percent,
                )
            ]

            # Update the service
            operation = services_client.update_service(service=current_service)
            result = operation.result()

            return {
                "success": True,
                "message": f"Cloud Run service traffic updated successfully",
                "action": "update_traffic",
                "service_name": service_path,
                "traffic_percent": traffic_percent,
                "service_url": result.uri,
                "assistant_id": assistant_id,
                "project_id": PROJECT_ID,
                "region": DEFAULT_REGION,
            }

        elif action == "scale":
            # Validate instance counts
            if min_instances < 0 or max_instances < 1 or min_instances > max_instances:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid scaling parameters. Min must be >= 0, max must be >= 1, and min <= max",
                )

            # Update scaling configuration
            current_service.template.scaling = run_v2.RevisionScaling(
                min_instance_count=min_instances,
                max_instance_count=max_instances,
            )

            # Update the service
            operation = services_client.update_service(service=current_service)
            result = operation.result()

            return {
                "success": True,
                "message": f"Cloud Run service scaling updated successfully",
                "action": "scale",
                "service_name": service_path,
                "min_instances": min_instances,
                "max_instances": max_instances,
                "service_url": result.uri,
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
                    f"Cloud Run service 'unity-{assistant_id}' not found. "
                    "Please create the service first."
                ),
            )
        else:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to {action} Cloud Run service: {str(e)}",
            )


# get cloud run service status
@router.get("/service/status")
async def get_cloudrun_service_status(assistant_id: str):
    """
    Get the status of a Google Cloud Run service and its configuration.

    Args:
        assistant_id: The assistant ID (service will be unity-<assistant_id>)
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

        # Get service details
        try:
            service = services_client.get_service(name=service_path)
            service_exists = True
        except Exception:
            service_exists = False
            service = None

        if not service_exists:
            return {
                "success": True,
                "service_exists": False,
                "message": f"Cloud Run service 'unity-{assistant_id}' not found",
                "assistant_id": assistant_id,
                "project_id": PROJECT_ID,
                "region": DEFAULT_REGION,
            }

        # Extract service status information
        service_status = "unknown"
        service_url = service.uri if service.uri else None

        # Get conditions to determine status
        if hasattr(service, "conditions") and service.conditions:
            for condition in service.conditions:
                if condition.type == "Ready":
                    if condition.status == "True":
                        service_status = "ready"
                    else:
                        service_status = "not_ready"
                    break

        # Get traffic allocation
        traffic_info = []
        if hasattr(service, "traffic") and service.traffic:
            for traffic_target in service.traffic:
                traffic_info.append(
                    {
                        "type": str(traffic_target.type_),
                        "percent": traffic_target.percent,
                        "revision": (
                            traffic_target.revision
                            if traffic_target.revision
                            else "latest"
                        ),
                    }
                )

        # Get scaling configuration
        scaling_info = {}
        if (
            hasattr(service, "template")
            and hasattr(service.template, "scaling")
            and service.template.scaling
        ):
            scaling_info = {
                "min_instances": service.template.scaling.min_instance_count,
                "max_instances": service.template.scaling.max_instance_count,
            }

        return {
            "success": True,
            "service_exists": True,
            "service_name": service_path,
            "service_url": service_url,
            "assistant_id": assistant_id,
            "project_id": PROJECT_ID,
            "region": DEFAULT_REGION,
            "service_status": {
                "name": service.name,
                "status": service_status,
                "create_time": (
                    service.create_time.isoformat() if service.create_time else None
                ),
                "update_time": (
                    service.update_time.isoformat() if service.update_time else None
                ),
                "generation": service.generation,
            },
            "traffic_allocation": traffic_info,
            "scaling_configuration": scaling_info,
        }

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get Cloud Run service status: {str(e)}"
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
