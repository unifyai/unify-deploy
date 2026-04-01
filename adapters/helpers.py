import base64
from datetime import datetime, timedelta, timezone
import httpx
import json
import os
import re
import time
import traceback
import requests
import logging

logger = logging.getLogger(__name__)

from common.metrics import (
    ORCHESTRA_GET_ASSISTANT_DURATION,
    BUILD_WEBHOOK_CONTEXT_DURATION,
    JOB_DEMAND_TOTAL,
    STALE_JOBS_LAST_SWEEP,
    UNITY_JOBS_RUNNING,
    UNITY_JOBS_IDLE,
)

from concurrent.futures import ThreadPoolExecutor, as_completed

_WEBHOOK_BG_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webhook-bg")
from google.cloud import pubsub_v1

from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse

from azure.core.credentials import AccessToken, TokenCredential
from msgraph import GraphServiceClient
from msgraph.generated.users.item.messages.item.message_item_request_builder import (
    MessageItemRequestBuilder,
)

from common.settings import SETTINGS

_pubsub_client = None


def get_pubsub_client():
    global _pubsub_client
    if _pubsub_client is None:
        _pubsub_client = pubsub_v1.PublisherClient()
    return _pubsub_client


def parse_teams_resource_id(resource: str, key: str) -> str | None:
    """
    Extract an ID from a Teams Graph resource path.
    Handles both formats: key('value') and key/value

    Args:
        resource: The Graph API resource path (e.g., "teams('uuid')/channels('19:xxx')")
        key: The key to extract (e.g., "teams", "channels", "chats", "messages")

    Returns:
        The extracted ID or None if not found
    """
    # Format: key('value')
    if f"{key}('" in resource:
        return resource.split(f"{key}('")[1].split("')")[0]
    # Format: /key/value/
    if f"/{key}/" in resource or f"{key}/" in resource:
        parts = resource.split("/")
        try:
            idx = parts.index(key)
            if idx >= 0 and len(parts) > idx + 1:
                return parts[idx + 1]
        except ValueError:
            pass
    return None


def get_assistant(
    email_address: str = None,
    phone_number: str = None,
    assistant_id: str = None,
) -> dict[str, str]:
    """
    Get the assistant id from the email address or phone number.

    Args:
        email_address: The email address of the assistant.
        phone_number: The phone number of the assistant.

    Returns:
        The assistant id.
    """
    params = dict()
    if email_address:
        params["email"] = email_address
    if phone_number:
        params["phone"] = phone_number
    if assistant_id:
        params["agent_id"] = assistant_id
    email_check = email_address or ""
    phone_check = phone_number or ""

    local_assistant_data = {
        "assistant_id": "local-assistant",
        "deploy_env": None,
        "user_id": "local-user",
        "voice_provider": "cartesia",
        "voice_id": None,
        "api_key": "",
        "user_first_name": "",
        "user_surname": "",
        "assistant_first_name": "Local",
        "assistant_surname": "Assistant",
        "assistant_age": "20",
        "assistant_nationality": "United States",
        "assistant_about": "Local Assistant",
        "assistant_timezone": "UTC",
        "assistant_email": "unity.agent@unify.ai",
        "user_email": "unity.agent@unify.ai",
        "user_number": "",
        "assistant_number": "",
        "user_whatsapp_number": "",
        "assistant_whatsapp_number": "",
        "desktop_mode": "ubuntu",
        "user_desktop_mode": None,
        "user_desktop_filesys_sync": False,
        "user_desktop_url": None,
        "is_local": True,
    }
    if "+15550100002" in phone_check or assistant_id == "local-assistant":
        return local_assistant_data
    if (
        "+0123456789" in phone_check
        or "local-test-assistant@unify.ai" in email_check
        or assistant_id == "local-test-assistant"
    ):
        return {
            **local_assistant_data,
            "assistant_id": "local-test-assistant",
            "user_first_name": "Test",
            "user_surname": "User",
            "user_number": "+9876543210",
            "user_email": "test@unify.ai",
            "assistant_first_name": "Test",
            "assistant_surname": "Assistant",
            "assistant_number": "+0123456789",
            "assistant_email": "local-test-assistant@unify.ai",
            "user_whatsapp_number": "+9876543210",
        }

    _lookup_type = "id" if assistant_id else ("email" if email_address else "phone")
    _t0 = time.perf_counter()
    _status = "error"
    try:
        response = requests.get(
            f"{SETTINGS.orchestra_url}/admin/assistant",
            params=params,
            headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        ).json()
        _status = "error" if "detail" in response else "success"
    except Exception:
        raise
    finally:
        ORCHESTRA_GET_ASSISTANT_DURATION.labels(
            lookup_type=_lookup_type,
            status=_status,
        ).observe(time.perf_counter() - _t0)

    logger.info(f"get_assistant params: {params}")
    logger.info(f"get_assistant response: {response}")

    if "detail" in response:
        return {**local_assistant_data, "assistant_id": None}
    assistants = response["info"]
    if len(assistants) == 0:
        return {**local_assistant_data, "assistant_id": None}

    return {
        "assistant_id": assistants[0]["agent_id"],
        "deploy_env": assistants[0].get("deploy_env"),
        "user_id": assistants[0]["user_id"],
        "api_key": assistants[0]["api_key"],
        "user_first_name": assistants[0]["user_first_name"],
        "user_surname": assistants[0]["user_last_name"],
        "assistant_first_name": assistants[0]["first_name"],
        "assistant_surname": assistants[0]["surname"],
        "assistant_age": str(assistants[0].get("age", "")),
        "assistant_nationality": assistants[0]["nationality"],
        "assistant_about": assistants[0]["about"],
        "assistant_timezone": assistants[0].get("timezone", "UTC"),
        "assistant_number": assistants[0]["phone"] or "",
        "assistant_whatsapp_number": assistants[0].get("assistant_whatsapp_number")
        or "",
        "assistant_email": assistants[0]["email"] or "",
        "user_number": assistants[0]["user_phone"] or "",
        "user_whatsapp_number": assistants[0].get("user_whatsapp_number") or "",
        "user_email": assistants[0]["user_email"] or "",
        "voice_provider": assistants[0]["voice_provider"],
        "voice_id": assistants[0]["voice_id"],
        "secrets": assistants[0].get("secrets", {}),
        "desktop_mode": assistants[0].get("desktop_mode", "ubuntu"),
        "user_desktop_mode": assistants[0].get("user_desktop_mode", None),
        "user_desktop_filesys_sync": assistants[0].get(
            "user_desktop_filesys_sync",
            False,
        ),
        "user_desktop_url": assistants[0].get("user_desktop_url", None),
        "demo_id": assistants[0].get("demo_id", None),
        "is_local": assistants[0].get("is_local", False),
        "team_ids": assistants[0].get("team_ids", []),
        "org_id": assistants[0].get("organization_id", None),
    }


def get_contacts(context: str, api_key: str) -> tuple[list[dict[str, str]], int]:
    response = requests.get(
        f"{SETTINGS.orchestra_url}/logs",
        params={"project_name": "Assistants", "context": context},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return response.json(), response.status_code


def get_default_contacts(assistant_data: dict) -> list[dict[str, str]]:
    return [
        {
            "contact_id": 0,
            "first_name": assistant_data["assistant_first_name"],
            "surname": assistant_data["assistant_surname"],
            "email_address": assistant_data["assistant_email"],
            "phone_number": assistant_data["assistant_number"],
            "whatsapp_number": assistant_data.get("assistant_whatsapp_number", ""),
            "bio": "",
            "rolling_summary": "",
            "should_respond": False,
            "response_policy": "",
        },
        {
            "contact_id": 1,
            "first_name": assistant_data["user_first_name"],
            "surname": assistant_data["user_surname"],
            "email_address": assistant_data["user_email"],
            "phone_number": assistant_data["user_number"],
            "whatsapp_number": assistant_data.get("user_whatsapp_number", ""),
            "bio": "",
            "rolling_summary": "",
            "should_respond": True,
            "response_policy": "",
        },
    ]


def resolve_whatsapp_route(pool_number: str, sender: str) -> dict | None:
    """Resolve an inbound WhatsApp message to an assistant via Orchestra.

    Returns {"assistant_id": int, "role": str} on success, or None if no
    route exists (404). Raises on unexpected errors.
    """
    resp = requests.get(
        f"{SETTINGS.orchestra_url}/admin/whatsapp/resolve",
        params={"pool_number": pool_number, "sender": sender},
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        timeout=10,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.error(
            f"WhatsApp resolve failed: {resp.status_code} {resp.text}",
        )
        raise RuntimeError(f"WhatsApp resolve error: {resp.status_code}")
    return resp.json()


def check_contact_details(
    email_address: str = None,
    phone_number: str = None,
    medium: str = None,
    user_number: str = None,
    user_whatsapp_number: str = None,
    user_email: str = None,
) -> bool:
    """
    Check if the contact details are valid.

    WhatsApp is handled separately via Orchestra's resolve endpoint and
    never reaches this function (the adapter passes validate_contact=False
    for WhatsApp).

    Args:
        email_address: The email address of the contact.
        phone_number: The phone number of the contact.
        medium: The medium of the contact.
        user_number: The phone number of the user.
        user_whatsapp_number: The whatsapp number of the user.
        user_email: The email of the user.
    """
    logger.info(
        f"Checking contact details: {email_address}, {phone_number}, {medium}, "
        f"{user_number}, {user_whatsapp_number}, {user_email}",
    )
    if medium == "email" and user_email == email_address:
        return True
    if medium in ["msg", "phone"] and user_number == phone_number:
        return True
    return False


def check_valid_contact(
    email_address: str = None,
    phone_number: str = None,
    medium: str = None,
    assistant_context: str = None,
    api_key: str = None,
    user_number: str = None,
    user_whatsapp_number: str = None,
    user_email: str = None,
    assistant_data: dict = None,
) -> list[dict[str, str]]:
    """
    Check if the contact is valid.

    Args:
        email_address: The email address of the contact.
        phone_number: The phone number of the contact.
        medium: The medium of the contact.
        assistant_context: The context of the assistant.
        api_key: The API key of the assistant.
        user_number: The phone number of the user.
        user_whatsapp_number: The whatsapp number of the user.
        user_email: The email of the user.
        assistant_data: The data of the assistant.
    """
    logger.info(
        f"Checking valid contact: {email_address}, {phone_number}, {medium}, "
        f"{user_number}, {user_whatsapp_number}, {user_email}, {assistant_context}",
    )

    # check for contact in assistant contacts
    context = f"{assistant_context}/Contacts"
    response_json, status_code = get_contacts(context, api_key)
    default_contacts = get_default_contacts(assistant_data)
    resp_contacts = response_json["logs"] if status_code == 200 else []
    if len(resp_contacts) < 2:
        # if the context or project isn't created yet (first time user)
        # len(resp_contacts) < 2 is to deal with race conditions right on
        # hiring a new assistant, whenever the wakeup message is sent, the contact
        # manager gets initialized in unity so there's a stage where the context is
        # created but the contacts haven't been added yet
        if (
            status_code != 200
            and response_json.get("detail")
            in [
                "Project Assistants not found.",
                f"Context '{context}' not found",
            ]
        ) or len(resp_contacts) < 2:
            # check for boss user
            if check_contact_details(
                email_address=email_address,
                phone_number=phone_number,
                medium=medium,
                user_number=user_number,
                user_whatsapp_number=user_whatsapp_number,
                user_email=user_email,
            ):
                logger.info(
                    f"Boss user found: {email_address}, {phone_number}, {medium}, "
                    f"{user_number}, {user_whatsapp_number}, {user_email}",
                )
                return default_contacts, True

        # otherwise
        logger.info(f"Failed to get contacts for assistant {assistant_context}")
        logger.info(response_json)
        return default_contacts, False
    contacts = [c["entries"] for c in resp_contacts]
    logger.info(f"Contacts: {contacts}")
    if len(contacts) == 0:
        return default_contacts, False

    # check for boss user
    boss_contact = [contact for contact in contacts if contact["contact_id"] == 1]
    logger.info(f"Boss contact: {boss_contact}")
    if len(boss_contact) > 0:
        boss_contact = boss_contact[0]
        boss_user_number = boss_contact.get("phone_number", "")
        boss_user_email = boss_contact.get("email_address", "")
        if check_contact_details(
            email_address=email_address,
            phone_number=phone_number,
            medium=medium,
            user_number=boss_user_number,
            user_whatsapp_number=user_whatsapp_number,
            user_email=boss_user_email,
        ):
            logger.info(
                f"Boss user found: {email_address}, {phone_number}, {medium}, "
                f"{boss_user_number}, {user_whatsapp_number}, {boss_user_email}",
            )
            return contacts, True
    else:
        logger.info("No boss user found")
        return default_contacts, False

    # check all contacts
    for contact in contacts:
        if check_contact_details(
            email_address=email_address,
            phone_number=phone_number,
            medium=medium,
            user_number=contact.get("phone_number", ""),
            user_whatsapp_number=assistant_data["user_whatsapp_number"],
            user_email=contact.get("email_address", ""),
        ):
            logger.info(f"Contact found: {contact}")
            return contacts, True
    return default_contacts, False


def expire_all_stale_jobs(max_age_hours: int = 24) -> dict:
    """Suspend K8s jobs that have been running longer than *max_age_hours*.

    For each stale job:
    - Suspends the K8s job
    - Releases any leaked pool VM for the assistant

    Uses K8s as the source of truth (via /infra/jobs).
    """
    admin_key = SETTINGS.orchestra_admin_key
    if not SETTINGS.comms_url:
        return {"total_running": 0, "expired": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    headers = {"Authorization": f"Bearer {admin_key}"}

    try:
        resp = requests.get(
            f"{SETTINGS.comms_url}/infra/jobs",
            params={
                "label_selector": "app=unity,unity-status=running",
                "hours": max(max_age_hours + 12, 36),
            },
            headers=headers,
        )
        if resp.status_code != 200:
            logger.error(
                f"[expire_all_stale_jobs] /infra/jobs returned {resp.status_code}",
            )
            return {"total_running": 0, "expired": 0, "error": resp.text}
    except Exception as e:
        logger.error(f"[expire_all_stale_jobs] /infra/jobs request failed: {e}")
        return {"total_running": 0, "expired": 0, "error": str(e)}

    all_jobs = resp.json().get("jobs", [])

    stale = []
    for job in all_jobs:
        ts_str = job.get("creation_timestamp", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                stale.append(job)
        except (ValueError, TypeError):
            continue

    STALE_JOBS_LAST_SWEEP.set(len(stale))

    logger.info(
        f"[expire_all_stale_jobs] Found {len(all_jobs)} running job(s), "
        f"{len(stale)} stale (>{max_age_hours}h old)",
    )

    if not stale:
        return {"total_running": len(all_jobs), "expired": 0}

    jobs_to_suspend = []
    unique_assistants = set()
    for job in stale:
        job_name = job.get("job_name")
        assistant_id = job.get("assistant_id", "unknown")
        logger.info(
            f"[expire_all_stale_jobs] Stale job: {job_name} "
            f"assistant_id={assistant_id} "
            f"created={job.get('creation_timestamp')}",
        )
        if job_name:
            jobs_to_suspend.append(job_name)
        if assistant_id and assistant_id != "unknown":
            unique_assistants.add(assistant_id)

    suspended_jobs = []

    def _suspend_job(jn):
        try:
            requests.post(
                f"{SETTINGS.comms_url}/infra/job/stop",
                data={"job_name": jn},
                headers=headers,
                timeout=10,
            )
            logger.info(f"[expire_all_stale_jobs] Suspended K8s job: {jn}")
            return jn
        except Exception as exc:
            logger.info(
                f"[expire_all_stale_jobs] Job suspend non-fatal for {jn}: {exc}",
            )
            return None

    if jobs_to_suspend:
        with ThreadPoolExecutor(max_workers=len(jobs_to_suspend)) as pool:
            results = list(pool.map(_suspend_job, jobs_to_suspend))
        suspended_jobs = [r for r in results if r is not None]

    released_assistants = []

    def _release_vm(aid):
        try:
            requests.post(
                f"{SETTINGS.comms_url}/infra/vm/pool/release",
                headers=headers,
                json={"assistant_id": aid},
                timeout=10,
            )
            return aid
        except Exception as exc:
            logger.info(
                f"[expire_all_stale_jobs] VM release non-fatal for {aid}: {exc}",
            )
            return None

    if unique_assistants:
        with ThreadPoolExecutor(max_workers=len(unique_assistants)) as pool:
            results = list(pool.map(_release_vm, unique_assistants))
        released_assistants = [r for r in results if r is not None]

    return {
        "total_running": len(all_jobs),
        "expired": len(stale),
        "suspended_k8s_jobs": suspended_jobs,
        "released_assistants": released_assistants,
    }


def start_unity_job(assistant: dict, medium: str):
    """Start the service using values from assistant dict."""
    api_key = assistant["api_key"]
    assistant_id = assistant["assistant_id"]

    if api_key == "":
        logger.info(f"No user name for assistant {assistant_id}")
        return

    desktop_mode = assistant.get("desktop_mode", "ubuntu")
    user_desktop_mode = assistant.get("user_desktop_mode", None)
    user_desktop_filesys_sync = assistant.get("user_desktop_filesys_sync", False)
    user_desktop_url = assistant.get("user_desktop_url", None)

    demo_id = assistant.get("demo_id", None)

    # start job
    headers = {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}
    try:
        response = requests.post(
            f"{SETTINGS.comms_url}/infra/job/start",
            headers=headers,
            data={
                "api_key": api_key,
                "medium": medium,
                "assistant_id": assistant_id,
                "user_id": assistant["user_id"],
                "user_first_name": assistant["user_first_name"],
                "user_surname": assistant["user_surname"],
                "user_email": assistant["user_email"],
                "assistant_first_name": assistant["assistant_first_name"],
                "assistant_surname": assistant["assistant_surname"],
                "assistant_age": assistant["assistant_age"],
                "assistant_nationality": assistant["assistant_nationality"],
                "assistant_about": assistant["assistant_about"],
                "assistant_timezone": assistant["assistant_timezone"],
                "user_number": assistant["user_number"],
                "assistant_number": assistant["assistant_number"],
                "assistant_email": assistant["assistant_email"],
                "user_whatsapp_number": assistant["user_whatsapp_number"],
                "assistant_whatsapp_number": assistant.get(
                    "assistant_whatsapp_number", ""
                ),
                "voice_provider": assistant["voice_provider"],
                "voice_id": assistant["voice_id"],
                "desktop_mode": desktop_mode,
                "user_desktop_mode": user_desktop_mode or "",
                "user_desktop_filesys_sync": (
                    "true" if user_desktop_filesys_sync else "false"
                ),
                "user_desktop_url": user_desktop_url or "",
                # Pass demo_id directly; Unity derives demo_mode from demo_id presence
                "demo_id": str(demo_id) if demo_id else "",
                "team_ids": json.dumps(assistant.get("team_ids", [])),
                "org_id": (
                    str(assistant.get("org_id", ""))
                    if assistant.get("org_id") is not None
                    else ""
                ),
                "deploy_env": assistant.get("deploy_env", ""),
            },
            timeout=0.1,
        )
        if response.status_code == 200:
            logger.info(f"Job started for assistant {assistant_id}")
        elif response.status_code == 202:
            logger.info(
                f"Job start queued for assistant {assistant_id} (pool exhausted)",
            )
        else:
            logger.warning(
                f"Job start failed for assistant {assistant_id}: "
                f"{response.status_code} {response.text}",
            )
    except requests.exceptions.Timeout:
        logger.info(
            f"Job start dispatched for assistant {assistant_id} (fire-and-forget)",
        )
    except requests.RequestException as e:
        logger.error(f"Job start request failed for assistant {assistant_id}: {e}")


class IdlePoolTarget:
    __slots__ = ("target", "min_floor", "demand_buffer")

    def __init__(self, target: int, min_floor: int, demand_buffer: int):
        self.target = target
        self.min_floor = min_floor
        self.demand_buffer = demand_buffer

    @property
    def demand_exceeds_floor(self) -> bool:
        return self.demand_buffer > self.min_floor


def get_target_idle_count(running_count: int) -> IdlePoolTarget:
    """Calculate the target number of idle jobs based on current demand.

    Returns an IdlePoolTarget with:
    - target: max(min_floor, demand_buffer)
    - min_floor: the UNITY_MIN_IDLE_JOBS value
    - demand_buffer: ceil(running_count / UNITY_IDLE_JOB_DEMAND_FACTOR)
    - demand_exceeds_floor: whether demand-based scaling has kicked in
    """
    min_floor = int(os.getenv("UNITY_MIN_IDLE_JOBS", "3"))
    demand_factor = int(os.getenv("UNITY_IDLE_JOB_DEMAND_FACTOR", "5"))

    if demand_factor <= 0:
        return IdlePoolTarget(min_floor, min_floor, 0)

    demand_buffer = -(-running_count // demand_factor)
    return IdlePoolTarget(max(min_floor, demand_buffer), min_floor, demand_buffer)


def _fetch_infra_jobs(
    params: dict,
    *,
    retries: int = 2,
    backoff: float = 0.5,
    caller: str = "",
) -> requests.Response | None:
    """GET /infra/jobs with retries and exponential backoff.

    Returns the Response on 200, or None after all attempts are exhausted.
    """
    tag = f"[{caller}] " if caller else ""
    headers = {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}

    for attempt in range(1 + retries):
        try:
            resp = requests.get(
                f"{SETTINGS.comms_url}/infra/jobs",
                params=params,
                headers=headers,
            )
            if resp.status_code == 200:
                return resp
            logger.warning(
                f"{tag}/infra/jobs returned {resp.status_code} "
                f"(attempt {attempt + 1}/{1 + retries})",
            )
        except Exception as e:
            logger.error(
                f"{tag}/infra/jobs error (attempt {attempt + 1}/{1 + retries}): {e}",
            )
        if attempt < retries:
            time.sleep(backoff * (2**attempt))

    return None


def get_unity_jobs_inventory() -> dict[str, list[dict]]:
    """Get a categorized inventory of Unity jobs from GKE in a single request.

    Returns:
        A dict with 'running' and 'idle' keys, each containing a list of job dicts
        filtered by the current environment (staging vs production).
    """
    resp = _fetch_infra_jobs(
        {"label_selector": "app=unity,unity-status!=done"},
        caller="get_unity_jobs_inventory",
    )
    if resp is None:
        return {"running": [], "idle": []}

    all_jobs = resp.json().get("jobs", [])
    inventory: dict[str, list[dict]] = {"running": [], "idle": []}

    for job in all_jobs:
        labels = job.get("labels", {})
        unity_status = labels.get("unity-status")

        if unity_status in ("running", "starting"):
            inventory["running"].append(job)
        elif unity_status == "idle":
            inventory["idle"].append(job)

    return inventory


def _trigger_pending_reconciliation() -> dict:
    """Call the comms app to process pending startup requests.

    Returns the JSON response from the reconciler, or an error dict.
    Logs failures so the caller (and Cloud Scheduler) can observe them.
    """
    try:
        resp = requests.post(
            f"{SETTINGS.comms_url}/infra/pending/process",
            headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning(
            "Pending reconciliation returned %s: %s",
            resp.status_code,
            resp.text[:200],
        )
        return {"error": resp.status_code}
    except Exception as e:
        logger.error("Pending reconciliation failed: %s", e)
        return {"error": str(e)}


def _trigger_pending_vm_reconciliation() -> dict:
    """Call the comms app to process pending VM assignment requests.

    Returns the JSON response from the reconciler, or an error dict.
    """
    try:
        resp = requests.post(
            f"{SETTINGS.comms_url}/infra/vm/pending/process",
            headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
            timeout=60,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning(
            "Pending VM reconciliation returned %s: %s",
            resp.status_code,
            resp.text[:200],
        )
        return {"error": resp.status_code}
    except Exception as e:
        logger.error("Pending VM reconciliation failed: %s", e)
        return {"error": str(e)}


def replenish_idle_pool(refresh: bool = False) -> dict:
    """Core logic for idle job pool replenishment.

    Called by build_webhook_context() and by the /scheduled/jobs/create endpoint.

    Fill mode has two regimes:
    - Floor regime (demand_buffer <= min_idle_floor): Creates exactly 1 job per
      call with no pool size check. This avoids race conditions when multiple
      webhooks fire concurrently — each sees the same stale inventory and would
      otherwise over- or under-provision. The hourly cleanup trims the excess.
    - Demand regime (demand_buffer > min_idle_floor): Checks inventory and fills
      the gap to the demand-based target. At this scale, small race-induced
      discrepancies are negligible relative to pool size.
    """
    inventory = get_unity_jobs_inventory()
    running_count = len(inventory["running"])
    current_idle_count = len(inventory["idle"])
    UNITY_JOBS_RUNNING.set(running_count)
    UNITY_JOBS_IDLE.set(current_idle_count)

    pool_target = get_target_idle_count(running_count)

    if refresh:
        num_to_create = pool_target.target
    elif not pool_target.demand_exceeds_floor:
        num_to_create = 1
    else:
        num_to_create = max(0, pool_target.target - current_idle_count)

    if num_to_create == 0:
        UNITY_JOBS_RUNNING.set(running_count)
        UNITY_JOBS_IDLE.set(current_idle_count)
        logger.info(
            f"Idle pool is healthy (current: {current_idle_count}, target: {pool_target.target}). No jobs created.",
        )
        return {
            "status": "healthy",
            "current": current_idle_count,
            "target": pool_target.target,
        }

    mode = (
        "refresh"
        if refresh
        else ("fill-floor" if not pool_target.demand_exceeds_floor else "fill-demand")
    )
    logger.info(
        f"[{mode}] Creating {num_to_create} idle jobs (current: {current_idle_count}, target: {pool_target.target})...",
    )
    headers = {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}
    response = requests.get(f"{SETTINGS.comms_url}/infra/image", headers=headers)
    commit_hash = response.json()["commit_hash"]
    image = f"{SETTINGS.image_registry}/{SETTINGS.unity_image_name}:{commit_hash}"

    def _create_single_job():
        try:
            resp = requests.post(
                f"{SETTINGS.comms_url}/infra/job/create",
                data={"image": image},
                headers=headers,
                timeout=0.1,
            )
            return resp.json()
        except requests.exceptions.Timeout:
            return {"status": "dispatched"}

    with ThreadPoolExecutor(max_workers=num_to_create) as pool:
        futures = [pool.submit(_create_single_job) for _ in range(num_to_create)]
        created_jobs = [f.result() for f in as_completed(futures)]

    UNITY_JOBS_RUNNING.set(running_count)
    UNITY_JOBS_IDLE.set(current_idle_count + len(created_jobs))
    reconcile_result = _trigger_pending_reconciliation()
    if "error" in reconcile_result:
        logger.warning(
            "Reactive container reconciliation after replenish failed: %s",
            reconcile_result,
        )

    vm_reconcile_result = _trigger_pending_vm_reconciliation()
    if "error" in vm_reconcile_result:
        logger.warning(
            "Reactive VM reconciliation after replenish failed: %s",
            vm_reconcile_result,
        )

    return {
        "mode": mode,
        "created": len(created_jobs),
        "target": pool_target.target,
        "details": created_jobs,
    }


def cleanup_idle_pool() -> dict:
    """Core logic for idle job pool cleanup.

    Called by the /scheduled/jobs/cleanup endpoint via run_in_executor.
    """
    headers = {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}

    inventory = get_unity_jobs_inventory()
    running_count = len(inventory["running"])
    idle_count = len(inventory["idle"])
    UNITY_JOBS_RUNNING.set(running_count)
    UNITY_JOBS_IDLE.set(idle_count)
    target_retain = get_target_idle_count(running_count).target

    # Get all idle jobs via K8s label selector
    resp = requests.get(
        f"{SETTINGS.comms_url}/infra/jobs",
        params={"label_selector": "app=unity,unity-status=idle"},
        headers=headers,
    )
    jobs = resp.json()
    idle_jobs = {
        job["job_name"]: job.get("resource_version")
        for job in jobs["jobs"]
        if (
            job["job_name"].endswith(SETTINGS.env_suffix)
            if SETTINGS.env_suffix
            else not any(job["job_name"].endswith(s) for s in ("-staging", "-preview"))
        )
    }

    # Classify idle jobs by age into three buckets
    very_new_idle_jobs = []  # < 1 min: always retained, exempt from quota
    new_idle_jobs = []  # 1–11 min: preferred when filling the quota
    old_idle_jobs = []  # >= 11 min: used to fill quota if new ones aren't enough
    now = datetime.now(timezone.utc)
    for job_name in idle_jobs:
        # job_name format: unity-{YYYY-MM-DD-HH-MM-SS}-{random_id}{-staging}
        job_timestamp_str = "-".join(
            filter(
                lambda part: part.isdigit() and len(part) in [2, 4],
                job_name.split("-"),
            ),
        )
        job_timestamp = datetime.strptime(
            job_timestamp_str,
            "%Y-%m-%d-%H-%M-%S",
        ).replace(tzinfo=timezone.utc)
        delta = now - job_timestamp
        if delta < timedelta(minutes=1):
            very_new_idle_jobs.append(job_name)
        elif delta < timedelta(minutes=11):
            new_idle_jobs.append(job_name)
        else:
            old_idle_jobs.append(job_name)

    # Very-new jobs are unconditionally retained (not counted against the quota).
    # From the remaining jobs, retain up to target_retain, preferring newer ones.
    new_idle_jobs = sorted(new_idle_jobs, reverse=True)
    old_idle_jobs = sorted(old_idle_jobs, reverse=True)
    retain = list(very_new_idle_jobs)
    quota_retain = new_idle_jobs[:target_retain]
    if len(quota_retain) < target_retain:
        quota_retain += old_idle_jobs[: target_retain - len(quota_retain)]
    retain += quota_retain
    retain_set = set(retain)

    to_delete = {j: idle_jobs[j] for j in idle_jobs if j not in retain_set}
    logger.info(
        f"Cleanup: retain={len(retain)} "
        f"(very_new={len(very_new_idle_jobs)}, quota={len(quota_retain)}, "
        f"target={target_retain}), delete={len(to_delete)}, running={running_count}",
    )
    logger.info(f"Idle jobs to retain: {sorted(retain)}")
    logger.info(f"Idle jobs to delete: {sorted(to_delete)}")

    def _delete_single_job(job_name, resource_version):
        data = {"job_name": job_name}
        if resource_version is not None:
            data["resource_version"] = resource_version
        resp = requests.delete(
            f"{SETTINGS.comms_url}/infra/job/delete",
            data=data,
            headers=headers,
        )
        if resp.status_code == 409:
            logger.info(
                f"Skipped deleting {job_name}: job changed since listing (409 Conflict)",
            )
        elif resp.status_code != 200:
            logger.warning(
                f"Failed to delete {job_name}: {resp.status_code} {resp.text}",
            )

    if to_delete:
        with ThreadPoolExecutor(max_workers=len(to_delete)) as pool:
            futures = [
                pool.submit(_delete_single_job, name, rv)
                for name, rv in to_delete.items()
            ]
            for f in as_completed(futures):
                f.result()

    UNITY_JOBS_RUNNING.set(running_count)
    UNITY_JOBS_IDLE.set(len(retain))

    return {
        "retained": len(retain),
        "deleted": len(to_delete),
        "target": target_retain,
        "running": running_count,
    }


def _resolve_contacts(
    validate_contact: bool,
    sender: str,
    is_email: bool,
    normalized_sender: str,
    channel: str,
    user_id: str,
    assistant_id: str,
    api_key: str,
    user_number: str,
    user_whatsapp_number: str,
    user_email: str,
    assistant_data: dict,
) -> tuple[list, bool]:
    """Resolve contacts for an assistant. Returns (contacts, is_valid_contact)."""
    if validate_contact:
        return check_valid_contact(
            email_address=(sender if is_email else ""),
            phone_number=("" if is_email else normalized_sender),
            medium=channel,
            assistant_context=f"{user_id}/{assistant_id}",
            api_key=api_key,
            user_number=user_number,
            user_whatsapp_number=user_whatsapp_number,
            user_email=user_email,
            assistant_data=assistant_data,
        )
    response, status_code = get_contacts(
        f"{user_id}/{assistant_id}/Contacts",
        api_key,
    )
    # len(resp_contacts) < 2 handles the race condition on hiring:
    # the contact manager gets initialized in unity so there's a stage
    # where the context is created but contacts haven't been added yet
    logger.info(f"response status_code: {status_code}, contacts: {response}")
    resp_contacts = response["logs"] if status_code == 200 else []
    if len(resp_contacts) < 2:
        logger.info("contact fetching failed, using default contacts")
        return get_default_contacts(assistant_data), True
    return [c["entries"] for c in resp_contacts], True


def build_webhook_context(
    channel: str,
    destination: str,
    sender: str,
    assistant_id: str = None,
    validate_contact: bool = True,
    ensure_job: bool = True,
    force_start: bool = False,
    assistant_data: dict = None,
):
    """Build a shared context for webhooks.

    Args:
        assistant_data: Optional pre-fetched assistant data to avoid duplicate Orchestra calls.
    """
    _t0 = time.perf_counter()
    _ctx_status = "error"
    # normalize identifiers and resolve assistant by channel
    is_email = channel in ["email", "teams"]
    normalized_sender = (
        sender.replace("whatsapp:", "") if channel == "whatsapp" else sender
    ).strip()

    # get assistant data (skip if pre-fetched)
    if assistant_data is None:
        if assistant_id:
            assistant_data = get_assistant(assistant_id=assistant_id)
        else:
            logger.info(
                f"Getting assistant data for {destination} with is_email: {is_email}",
            )
            assistant_data = (
                get_assistant(email_address=destination)
                if is_email
                else get_assistant(phone_number=destination)
            )
    api_key = assistant_data["api_key"]
    assistant_id = assistant_data["assistant_id"]
    user_id = assistant_data["user_id"]
    user_number = assistant_data["user_number"]
    user_whatsapp_number = assistant_data["user_whatsapp_number"]
    user_email = assistant_data["user_email"]
    logger.info(f"assistant_data: {assistant_data}")

    # resolve contacts and check job status in parallel
    logger.info(f"validate_contact: {validate_contact}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        contacts_future = pool.submit(
            _resolve_contacts,
            validate_contact,
            sender,
            is_email,
            normalized_sender,
            channel,
            user_id,
            assistant_id,
            api_key,
            user_number,
            user_whatsapp_number,
            user_email,
            assistant_data,
        )
    contacts, is_valid_contact = contacts_future.result()
    logger.info(f"contacts: {contacts}")

    # check contact validity
    is_local_assistant = bool(assistant_data.get("is_local", False))
    is_test_assistant = "test" in assistant_id
    is_valid_contact = is_valid_contact or is_local_assistant

    # Start a container if needed. The /infra/job/start endpoint handles
    # deduplication atomically via K8s labels — if a container is already
    # serving this assistant, the endpoint returns early without publishing.
    # This replaces the previous is_job_running() + mark_job_running() flow
    # which was non-atomic and could leave stale records.
    job_started = False
    is_running = False
    skip_auto_start = is_test_assistant or is_local_assistant
    should_start_job = (
        ensure_job and is_valid_contact and (force_start or not skip_auto_start)
    )
    if should_start_job:
        JOB_DEMAND_TOTAL.labels(channel=channel).inc()
        _WEBHOOK_BG_POOL.submit(start_unity_job, assistant_data, channel)
        _WEBHOOK_BG_POOL.submit(replenish_idle_pool, False)
        job_started = True
        is_running = True

    logger.info(f"is_valid_contact: {is_valid_contact}")
    _ctx_status = "error" if assistant_data.get("assistant_id") is None else "success"
    BUILD_WEBHOOK_CONTEXT_DURATION.labels(
        channel=channel,
        job_started=str(job_started).lower(),
        status=_ctx_status,
    ).observe(time.perf_counter() - _t0)
    return {
        "assistant": assistant_data,
        "contacts": contacts,
        "is_valid_contact": is_valid_contact,
        "is_job_running": is_running,
        "job_started": job_started,
    }


# phone helpers
def get_twilio_client():
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    return TwilioClient(account_sid, auth_token)


def create_conference_response(conference_name, with_status=False):
    resp_user = VoiceResponse()
    dial_user = resp_user.dial()
    if with_status:
        dial_user.conference(
            conference_name,
            startConferenceOnEnter=True,
            endConferenceOnExit=True,
            muted=False,
            wait_url="https://auburn-eagle-6359.twil.io/assets/ring-tone-68676.mp3",
            status_callback=f"{SETTINGS.comms_url}/phone/conference-status",
            status_callback_event="end",
        )
        return resp_user
    dial_user.conference(
        conference_name,
        startConferenceOnEnter=True,
        endConferenceOnExit=True,
        muted=False,
        wait_url="https://auburn-eagle-6359.twil.io/assets/ring-tone-68676.mp3",
    )
    return resp_user


def add_user_to_conference(
    conference_name,
    from_number,
    to_number_uri,
    connect_third_party=False,
):
    twilio_client = get_twilio_client()

    if connect_third_party:
        conferences = twilio_client.conferences.list(
            friendly_name=conference_name,
            status="in-progress",
        )
        participants = twilio_client.conferences(conferences[0].sid).participants.list()
        for participant in participants:
            call = twilio_client.calls(participant.call_sid).fetch()
            # Identify Livekit Agent and mute
            if "livekit.cloud" in call.to:
                twilio_client.conferences(conferences[0].sid).participants(
                    participant.sid,
                ).update(muted=True)
                break
        response = create_conference_response(conference_name, with_status=True)
    else:
        response = create_conference_response(conference_name)

    call = twilio_client.calls.create(
        to=to_number_uri,
        from_=from_number,
        twiml=str(response),
    )
    return call.sid


# email helpers
def _strip_quoted_text(text: str) -> str:
    """Remove quoted text and signatures from email content."""
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(">"):
            continue
        if re.match(r"On .+wrote:", stripped) or stripped.startswith(
            "-----Original Message-----",
        ):
            break
        cleaned.append(line)
    return "\n".join(cleaned).strip()


# =============================================================================
# Outlook Helpers
# =============================================================================


class TokenCredentialFromSecret(TokenCredential):
    """Wraps a stored access token for use with Microsoft Graph SDK."""

    def __init__(self, access_token: str):
        self._token = access_token

    def get_token(self, *scopes, **kwargs) -> AccessToken:
        # Expiry doesn't matter - scheduled job keeps token fresh
        return AccessToken(
            self._token,
            int(datetime.now(tz=timezone.utc).timestamp()) + 3600,
        )


def get_graph_client_from_token(access_token: str) -> GraphServiceClient:
    """
    Create a Microsoft Graph client from an access token (delegated permissions).

    Args:
        access_token: The Microsoft access token

    Returns:
        GraphServiceClient configured with the access token
    """
    return GraphServiceClient(
        credentials=TokenCredentialFromSecret(access_token),
        scopes=["https://graph.microsoft.com/.default"],
    )


async def get_outlook_thread_id(email_id: str, graph_client):
    """
    Fetch Outlook message details using delegated permissions.
    Similar to get_thread_id for Gmail - extracts conversation data from a notification.

    Args:
        email_id: The message ID from the notification
        graph_client: GraphServiceClient configured with user's access token

    Returns:
        tuple: (conversation_id, email_id, last_message) or (None, None, None) if not found
    """
    try:
        # Fetch the message with body in text format (not HTML)
        # Must explicitly select uniqueBody as it's not returned by default
        request_config = (
            MessageItemRequestBuilder.MessageItemRequestBuilderGetRequestConfiguration()
        )
        request_config.headers.add("Prefer", 'outlook.body-content-type="text"')
        request_config.query_parameters = (
            MessageItemRequestBuilder.MessageItemRequestBuilderGetQueryParameters(
                select=[
                    "id",
                    "conversationId",
                    "subject",
                    "body",
                    "uniqueBody",
                    "from",
                    "toRecipients",
                    "ccRecipients",
                    "bccRecipients",
                    "receivedDateTime",
                    "hasAttachments",
                ],
            )
        )

        # Use /me endpoint for delegated permissions
        message = await graph_client.me.messages.by_message_id(email_id).get(
            request_configuration=request_config,
        )

        if not message:
            print(f"Message {email_id} not found")
            return None, None, None

        # Note: Not marking as read - subscription only triggers on "created" events,
        # so we don't need to track read status for duplicate prevention

        # Extract message details (similar to Gmail's last_message format)
        # Use unique_body to get only the new content, not the quoted thread history
        last_message = {
            "sender": message.from_.email_address.address if message.from_ else "",
            "to": [r.email_address.address for r in (message.to_recipients or [])],
            "cc": [r.email_address.address for r in (message.cc_recipients or [])],
            "bcc": [r.email_address.address for r in (message.bcc_recipients or [])],
            "subject": message.subject or "",
            "content": message.unique_body.content if message.unique_body else "",
            "received_at": (
                message.received_date_time.isoformat()
                if message.received_date_time
                else None
            ),
            "has_attachments": message.has_attachments,
            "attachments": [],  # TODO: fetch attachment details if needed
        }

        conversation_id = message.conversation_id
        print(
            f"conversation_id: {conversation_id}, email_id: {email_id}, last_message: {last_message}",
        )

        return conversation_id, email_id, last_message

    except Exception as e:
        print(f"Error fetching Outlook message: {e}")
        traceback.print_exc()
        return None, None, None


# =============================================================================
# Gmail Helpers
# =============================================================================


def _header(headers, name: str) -> str:
    """Extract a specific header from email headers."""
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _payload_text(payload) -> str:
    """Extract text content from email payload."""
    mime_type = payload.get("mimeType", "")
    if mime_type.startswith("text/") and payload.get("body", {}).get("data"):
        data = payload["body"]["data"]
        decoded = base64.urlsafe_b64decode(data.encode("utf-8"))
        latest = _strip_quoted_text(decoded.decode("utf-8", errors="replace"))
        return latest

    for part in payload.get("parts", []):
        txt = _payload_text(part)
        if txt:
            return txt
    return ""


def _collect_attachments(payload):
    attachments = []
    if not payload:
        return attachments
    body = payload.get("body", {})
    filename = payload.get("filename")
    attachment_id = body.get("attachmentId")
    mime_type = payload.get("mimeType")
    size = body.get("size")
    if attachment_id:
        attachments.append(
            {
                "id": attachment_id,
                "filename": filename or "",
                "mimeType": mime_type,
                "size": size,
            },
        )
    for part in payload.get("parts", []):
        attachments.extend(_collect_attachments(part))
    return attachments


def _gmail_thread_to_conversation(thread):
    """Convert a Gmail thread to a structured conversation."""
    convo = []
    for msg in thread.get("messages", []):
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        convo.append(
            {
                "sender": _header(headers, "From"),
                "to": (
                    [_addr.strip() for _addr in _header(headers, "To").split(",")]
                    if _header(headers, "To")
                    else []
                ),
                "cc": (
                    [_addr.strip() for _addr in _header(headers, "Cc").split(",")]
                    if _header(headers, "Cc")
                    else []
                ),
                "bcc": (
                    [_addr.strip() for _addr in _header(headers, "Bcc").split(",")]
                    if _header(headers, "Bcc")
                    else []
                ),
                "subject": _header(headers, "Subject").replace("Re: ", ""),
                "content": _payload_text(payload),
            },
        )
    return convo


def get_thread_id(user_id, history_id, gmail_service):
    """Process Gmail history and thread to extract conversation data."""
    try:
        # Get history events for label changes
        histories = (
            gmail_service.users()
            .history()
            .list(
                userId=user_id,
                startHistoryId=history_id,
            )
            .execute()
        )
        print(f"pre-histories: {histories}")

        # Safeguard for thread replies
        if "history" not in histories or not histories["history"]:
            histories["history"] = [
                (
                    gmail_service.users()
                    .messages()
                    .list(
                        userId=user_id,
                        q="is:unread newer_than:1d",
                    )
                    .execute()
                ),
            ]

        # Process each history entry
        print(f"histories: {histories}")
        for history in histories["history"]:
            print(f"history: {history}")
            messages = history.get("messages", [])
            print(f"messages: {messages}")
            if len(messages) == 0:
                continue

            # Get the message details (Gmail message resource id)
            msg_id = messages[-1]["id"]
            message = (
                gmail_service.users()
                .messages()
                .get(userId=user_id, id=msg_id)
                .execute()
            )
            print(f"message: {message} {msg_id}")
            message_headers = message["payload"].get("headers", [])
            print(f"message_headers: {message_headers}")
            message_id_header = [
                header
                for header in message_headers
                if header.get("name") == "Message-ID"
            ][0]
            email_id = message_id_header.get("value")
            print(f"email_id: {email_id}")

            # Extract attachments from the Gmail message payload
            attachments = _collect_attachments(message.get("payload", {}))
            print(f"attachments: {attachments}")

            labels = message.get("labelIds", [])
            print(f"labels: {labels}")
            if labels and "UNREAD" not in labels:
                print(f"Message {msg_id} is read, skipping")
                continue

            gmail_service.users().messages().modify(
                userId=user_id,
                id=msg_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()

            # Get the thread for this message
            thread_id = message["threadId"]
            thread = (
                gmail_service.users()
                .threads()
                .get(userId=user_id, id=thread_id, format="full")
                .execute()
            )
            print(f"thread: {thread} {thread_id}")

            # Convert to conversation format
            conversation = _gmail_thread_to_conversation(thread)
            print(f"conversation: {conversation}")
            last_message = conversation[-1]
            print(f"last_message: {last_message}")

            # Attach filenames with IDs to the last_message for publishing
            last_message["attachments"] = [
                {"id": att["id"], "filename": att.get("filename", "")}
                for att in attachments
            ]

            # Return the conversation plus Gmail message id
            return thread_id, email_id, last_message, msg_id

        return None, None, None, None

    except Exception as e:
        print(f"Error processing history for user {user_id}: {str(e)}")
        traceback.print_exc()
        return None, None, None, None


def publish_gmail_thread_id(
    assistant_id,
    user_id,
    thread_id,
    email_id,
    last_message,
    contacts,
    gmail_message_id=None,
):
    """Publish the thread_id and user_id to a different pub/sub topic."""
    try:
        publisher = get_pubsub_client()
        topic_name = SETTINGS.assistant_topic(assistant_id)
        topic_path = publisher.topic_path(SETTINGS.gcp_project_id, topic_name)

        message_dict = {
            "thread": "email",
            "publish_timestamp": time.time(),
            "event": {
                "contacts": contacts,
                "thread_id": thread_id,
                "email_id": email_id,
                "gmail_message_id": gmail_message_id,
                "attachments": last_message.get("attachments", []),
                "from": last_message["sender"],
                "to": last_message["to"],
                "cc": last_message["cc"],
                "bcc": last_message["bcc"],
                "subject": last_message["subject"],
                "body": last_message["content"],
            },
        }
        data = json.dumps(message_dict).encode("utf-8")

        # Publish asynchronously
        publish_future = publisher.publish(topic_path, data=data, thread="inbound")
        if "test" in assistant_id:
            pubsub_message_id = publish_future.result(timeout=10)
            print(f"Message ID: {pubsub_message_id}")
        print(f"Published thread_id {thread_id} for user {user_id} to {topic_path}")
    except Exception as e:
        print(f"Failed to publish thread_id {thread_id} for user {user_id}: {e}")


def publish_outlook_thread_id(
    assistant_id,
    user_id,
    conversation_id,
    email_id,
    last_message,
    contacts,
):
    """Publish the Outlook conversation to pub/sub topic."""
    try:
        publisher = get_pubsub_client()
        topic_name = SETTINGS.assistant_topic(assistant_id)
        topic_path = publisher.topic_path(SETTINGS.gcp_project_id, topic_name)

        message_dict = {
            "thread": "email",
            "publish_timestamp": time.time(),
            "event": {
                "contacts": contacts,
                "thread_id": conversation_id,
                "email_id": email_id,
                "attachments": last_message.get("attachments", []),
                "from": last_message["sender"],
                "to": last_message["to"],
                "cc": last_message["cc"],
                "bcc": last_message.get("bcc", ""),
                "subject": last_message["subject"],
                "body": last_message["content"],
            },
        }
        data = json.dumps(message_dict).encode("utf-8")

        publish_future = publisher.publish(topic_path, data=data, thread="inbound")
        if "test" in assistant_id:
            msg_id = publish_future.result(timeout=10)
            logger.info(f"Message ID: {msg_id}")
        logger.info(
            f"Published conversation_id {conversation_id} for user {user_id} to {topic_path}",
        )
    except Exception as e:
        logger.info(
            f"Failed to publish conversation_id {conversation_id} for user {user_id}: {e}",
        )


def dispatch_livekit_agent(room_name: str):
    response = requests.post(
        f"{SETTINGS.comms_url}/phone/dispatch-livekit-agent",
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        json={"room_name": room_name},
    )
    if response.status_code != 200:
        logger.info(f"Failed to dispatch LiveKit agent. Status: {response.status_code}")
        return False
    return True


# =============================================================================
# Microsoft OAuth Helpers
# =============================================================================


async def exchange_microsoft_code_for_tokens(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict:
    """Exchange an authorization code for tokens."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

        if response.status_code != 200:
            raise Exception(f"Token exchange failed: {response.text}")

        data = response.json()
        data["expires_at"] = (
            datetime.now(tz=timezone.utc)
            + timedelta(seconds=data.get("expires_in", 3600))
        ).isoformat()
        return data


async def get_microsoft_user_info(access_token: str) -> dict:
    """Get user info (email, name, etc.) from an access token."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        if response.status_code != 200:
            raise Exception(f"Failed to get user info: {response.text}")

        return response.json()


async def store_microsoft_tokens(
    assistant_id: str,
    old_secrets: dict,
    new_secrets: dict,
    api_key: str,
) -> bool:
    """
    Store Microsoft OAuth tokens as assistant secrets.

    Stores:
    - MICROSOFT_ACCESS_TOKEN
    - MICROSOFT_REFRESH_TOKEN
    - MICROSOFT_TOKEN_EXPIRES_AT
    """
    if not SETTINGS.orchestra_url:
        logger.info("SETTINGS.orchestra_url not configured")
        return False

    secrets_to_store = {
        "MICROSOFT_ACCESS_TOKEN": new_secrets["access_token"],
        "MICROSOFT_REFRESH_TOKEN": new_secrets.get("refresh_token", ""),
        "MICROSOFT_TOKEN_EXPIRES_AT": new_secrets.get("expires_at", ""),
    }

    if not api_key:
        logger.info("api_key not configured")
        return False

    success = True
    async with httpx.AsyncClient() as client:
        for secret_name, secret_value in secrets_to_store.items():
            try:
                args = {
                    "url": f"{SETTINGS.orchestra_url}/assistant/{assistant_id}/secret",
                    "json": {"secret_name": secret_name, "secret_value": secret_value},
                    "headers": {"Authorization": f"Bearer {api_key}"},
                    "timeout": 30.0,
                }
                if old_secrets and secret_name in old_secrets:
                    logger.info(
                        f"Updating secret {secret_name} for assistant {assistant_id}",
                    )
                    args["url"] += f"/{secret_name}"
                    args["json"].pop("secret_name")
                    response = await client.put(**args)
                else:
                    logger.info(
                        f"Creating secret {secret_name} for assistant {assistant_id}",
                    )
                    response = await client.post(**args)
                if response.status_code in (200, 201):
                    logger.info(f"Stored {secret_name} for assistant {assistant_id}")
                else:
                    logger.info(
                        f"Failed to store {secret_name}: {response.status_code} - {response.text}",
                    )
                    success = False
            except Exception as e:
                logger.info(f"Error storing {secret_name}: {e}")
                success = False

    return success
