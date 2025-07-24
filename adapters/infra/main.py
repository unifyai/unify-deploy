from datetime import datetime, timedelta
import functions_framework
import os
import requests


STAGING = os.getenv("STAGING")
COMMS_URL = (
    "https://unity-comms-app-000000000000.us-central1.run.app"
    if not STAGING
    else "https://unity-comms-app-staging-000000000000.us-central1.run.app"
)


@functions_framework.http
def create_idle_job(request):
    """Cloud Function that creates a new idle job."""
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = requests.get( f"{COMMS_URL}/infra/image", headers=headers)
    commit_hash = response.json()["commit_hash"]
    image = (
        "us-central1-docker.pkg.dev/gcp-project-runtime/unity"
        + ("/unity:" if not STAGING else "/unity-staging:")
        + commit_hash
    )
    response =requests.post(
        f"{COMMS_URL}/infra/job/create", data={"image": image}, headers=headers
    )
    return response.json()


@functions_framework.http
def clean_idle_jobs(request):
    """Cloud Function that renews idle jobs that have been around for >24 hours.."""
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    idle_jobs = []

    # get all jobs
    jobs = requests.get(f"{COMMS_URL}/infra/jobs", headers=headers).json()
    job_names = [job["job_name"] for job in jobs["jobs"]]
    print(f"Job names: {job_names}")

    for job_name in job_names:
        # get logs
        logs = requests.get(
            f"{COMMS_URL}/infra/job/logs",
            params={"job_name": job_name},
            headers=headers,
        ).json().get("logs", [])
        print(f"Logs: {logs}")

        # check if job is idle
        if "ping received - keeping event manager alive" in logs:
            idle_jobs.append(job_name)

    new_idle_jobs = []
    for job_name in idle_jobs:
        # check if job is older than 10 minutes
        job_timestamp_str = job_name.replace("unity-", "").replace("-staging", "")
        job_timestamp = datetime.strptime(job_timestamp_str, "%Y-%m-%d-%H-%M-%S")
        now = datetime.now()
        delta = now - job_timestamp
        if delta > timedelta(minutes=11):
            new_idle_jobs.append(job_name)

    print(f"Idle jobs: {idle_jobs}")
    print(f"New idle jobs: {new_idle_jobs}")
    if len(new_idle_jobs) == 0:
        if len(idle_jobs) != 0:
            idle_jobs = sorted(idle_jobs)[:-1]

    # delete all old idle jobs
    for job_name in idle_jobs:
        requests.delete(
            f"{COMMS_URL}/infra/job/delete",
            data={"job_name": job_name},
            headers=headers,
        )

    return idle_jobs
