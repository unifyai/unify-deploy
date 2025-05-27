import os
import requests


def get_assistant_id(
    email_id: str = None,
    phone_number: str = None,
) -> str:
    """
    Get the assistant id from the email id or phone number.

    Args:
        email_id: The email id of the assistant.
        phone_number: The phone number of the assistant.

    Returns:
        The assistant id.
    """
    params = dict()
    if email_id:
        params["email"] = email_id
    if phone_number:
        params["phone_number"] = phone_number
    assistants = requests.get(
        "https://api.unify.ai/v0/admin/assistant",
        params=params,
        headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"},
    ).json()["info"]
    if len(assistants) == 0:
        raise ValueError("No assistants found")
    return assistants[0]["agent_id"]
