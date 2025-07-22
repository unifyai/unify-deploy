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
def renew_idle_job(request):
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

    print(f"Idle jobs: {idle_jobs}")

    # create new idle job
    response = requests.get( f"{COMMS_URL}/infra/image", headers=headers)
    commit_hash = response.json()["commit_hash"]
    image = (
        "us-central1-docker.pkg.dev/gcp-project-runtime/unity"
        + ("/unity:" if not STAGING else "/unity-staging:")
        + commit_hash
    )
    requests.post(
        f"{COMMS_URL}/infra/job/create", data={"image": image}, headers=headers
    )

    # delete all old idle jobs
    for job_name in idle_jobs:
        requests.delete(
            f"{COMMS_URL}/infra/job/delete",
            data={"job_name": job_name},
            headers=headers,
        )

    return idle_jobs
