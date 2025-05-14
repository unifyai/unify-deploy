import os
import requests

watch_response = requests.post(
    f'{os.getenv("UNIFY_COMMS_URL")}/email/watch',
    json={
        "primary_email": "unity.agent@unify.ai",
    }
)
if watch_response.status_code >= 400:
    # update watch failed!
    raise Exception(f"Update mail watch failed: unity.agent@unify.ai")

# from dotenv import load_dotenv

# load_dotenv()

# # Get list of agent emails from unify
# assistants_response = requests.get(
#     "https://api.unify.ai/v0/assistant",
#     headers={
#         "Authorization": f"Bearer {os.getenv("UNIFY_KEY")}"
#     }
# )
# if assistants_response.status_code == 200:
#     assistants_response = assistants_response.json()
#     assistants_response = assistants_response["info"]
#     # Post to /email/watch to update the mail watch
#     for assistant in assistants_response:
#         email = assistant["email"]
#         watch_response = requests.post(
#             f"{os.getenv("UNIFY_COMMS_URL")}/email/watch",
#             json={
#                 "primary_email": email,
#             }
#         )
#         if watch_response.status_code >= 400:
#             # update watch failed!
#             raise Exception(f"Update mail watch failed: {email}")
        