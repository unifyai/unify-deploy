"""
Local development server for adapters.

This file allows the adapters to run locally as a Flask web app
for testing and development purposes.
"""

import base64
import json
import sys
from pathlib import Path

adapters_dir = Path(__file__).parent
sys.path.insert(0, str(adapters_dir))

from flask import request, Flask, Request, Response
from main import (
    twilio_call_webhook as _twilio_call_webhook,
    twilio_call_status_webhook as _twilio_call_status_webhook,
    twilio_msg_webhook as _twilio_msg_webhook,
    twilio_whatsapp_webhook as _twilio_whatsapp_webhook,
    unify_message_webhook as _unify_message_webhook,
    unify_call_webhook as _unify_call_webhook,
    log_pre_hire_chats_webhook as _log_pre_hire_chats_webhook,
    unity_system_event_webhook as _unity_system_event_webhook,
    email_watch_renewer as _email_watch_renewer,
    email_notification_processor as _email_notification_processor,
    idle_job_creator as _idle_job_creator,
    idle_job_cleaner as _idle_job_cleaner,
    assistant_update_webhook as _assistant_update_webhook,
)

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint."""
    return Response("OK", status=200)


@app.route("/call-status", methods=["POST"])
def twilio_call_status():
    """Phone call status webhook endpoint."""
    try:
        response = _twilio_call_status_webhook(request)
        if isinstance(response, str):
            return Response(response)
        return response
    except Exception as e:
        return Response(f"Error: {str(e)}", status=500)


@app.route("/call", methods=["POST"])
def twilio_call_webhook():
    """Phone call webhook endpoint."""
    try:
        response = _twilio_call_webhook(request)
        if isinstance(response, str):
            return Response(response, mimetype="text/xml")
        return response
    except Exception as e:
        return Response(f"Error: {str(e)}", status=500)


@app.route("/msg", methods=["POST"])
def twilio_msg_webhook():
    """Phone SMS webhook endpoint."""
    try:
        response = _twilio_msg_webhook(request)
        if isinstance(response, str):
            return Response(response, mimetype="text/xml")
        return response
    except Exception as e:
        return Response(f"Error: {str(e)}", status=500)


@app.route("/unify_message", methods=["POST"])
def unify_message_webhook():
    """Unify Message webhook endpoint."""
    try:
        response = _unify_message_webhook(request)
        if isinstance(response, str):
            return Response(response)
        return response
    except Exception as e:
        return Response(f"Error: {str(e)}", status=500)


@app.route("/unify_call", methods=["POST"])
def unify_call_webhook():
    """Unify Call webhook endpoint."""
    try:
        response = _unify_call_webhook(request)
        if isinstance(response, str):
            return Response(response)
        return response
    except Exception as e:
        return Response(f"Error: {str(e)}", status=500)


@app.route("/log_pre_hire_chats", methods=["POST"])
def log_pre_hire_chats_webhook():
    """Log pre-hire chats webhook endpoint."""
    try:
        response = _log_pre_hire_chats_webhook(request)
        if isinstance(response, str):
            return Response(response)
        return response
    except Exception as e:
        return Response(f"Error: {str(e)}", status=500)


@app.route("/unity_system_event", methods=["POST"])
def unity_system_event_webhook():
    """Unity system event webhook endpoint."""
    try:
        response = _unity_system_event_webhook(request)
        if isinstance(response, str):
            return Response(response)
        return response
    except Exception as e:
        return Response(f"Error: {str(e)}", status=500)


@app.route("/whatsapp", methods=["POST"])
def twilio_whatsapp_webhook():
    """WhatsApp webhook endpoint."""
    try:
        response = _twilio_whatsapp_webhook(request)
        if isinstance(response, str):
            return Response(response, mimetype="text/xml")
        return response
    except Exception as e:
        return Response(f"Error: {str(e)}", status=500)


@app.route("/email/watch", methods=["POST"])
def email_watch_renewer():
    """Email watch renewer endpoint."""
    try:
        response = _email_watch_renewer(request)
        return response
    except Exception as e:
        return Response(f"Error: {str(e)}", status=500)


@app.route("/email", methods=["POST"])
def email_notification_processor():
    """Email webhook endpoint."""
    try:
        json_payload = request.get_json(silent=True)
        msg = json_payload["message"]
        fake_event = type("CE", (), {})()
        fake_event.data = {
            "message": {"data": base64.b64encode(json.dumps(msg).encode()).decode()}
        }
        handler = getattr(
            _email_notification_processor, "__wrapped__", _email_notification_processor
        )
        return handler(fake_event)
    except Exception as e:
        print(f"Error: {str(e)}")
        return Response(f"Error: {str(e)}", status=500)


@app.route("/job/create", methods=["POST"])
def idle_job_creator():
    """Create idle job endpoint."""
    try:
        response = _idle_job_creator(request)
        return response
    except Exception as e:
        return Response(f"Error: {str(e)}", status=500)


@app.route("/job/clean", methods=["POST"])
def idle_job_cleaner():
    """Clean idle jobs endpoint."""
    try:
        response = _idle_job_cleaner(request)
        return response
    except Exception as e:
        return Response(f"Error: {str(e)}", status=500)


@app.route("/assistant/update", methods=["POST"])
def assistant_update_webhook():
    """Assistant update webhook endpoint."""
    try:
        response = _assistant_update_webhook(request)
        return response
    except Exception as e:
        return Response(f"Error: {str(e)}", status=500)


if __name__ == "__main__":
    print("🚀 Starting local adapters server...")
    print("📱 Available endpoints:")
    print("   - POST /call - Phone call webhook")
    print("   - POST /msg - Phone SMS webhook")
    print("   - POST /whatsapp - WhatsApp webhook")
    print("   - POST /email/watch - Email watch renewer")
    print("   - POST /email - Email notification processor")
    print("   - POST /job/create - Create idle job")
    print("   - POST /job/clean - Clean idle jobs")
    print("   - POST /assistant/update - Assistant update webhook")
    print(f"🌐 Server running at: http://localhost:3000")

    app.run(debug=True, host="0.0.0.0", port=3000)
