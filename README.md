# Unity Communication FastAPI App

This project provides a communication API with multiple Twilio and Google integrations. All endpoints are mounted under `/` via the `communication` package.

## Setup

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Run locally with Uvicorn:

   ```bash
   uvicorn main:app --reload --port 8080
   ```

3. The root health endpoint is available at:

   GET `/` → `{ "message": "success!" }`

## API Endpoints

### Phone (prefix `/phone`)

- POST `/phone/call`  
  Form data: `To` (phone number)  
  Returns TwiML XML for SIP dial via VoiceResponse.

- POST `/phone/text`  
  Form data: `Body` (text message)  
  Uses OpenAI and Twilio MessagingResponse to reply with SMS TwiML.

- POST `/phone/create`  
  No body. Purchases and configures a Twilio phone number and LiveKit SIP inbound trunk.  
  Returns `{ success: true, phoneNumber: "+1…" }`.

- DELETE `/phone/delete`  
  JSON body: `{ "phoneNumber": "+1…" }`  
  Releases the purchased Twilio number.

### WhatsApp (prefix `/whatsapp`)

- POST `/whatsapp/senders`  
  JSON body: `{ "phone_number": "+1…", "first_name": "…", "surname": "…" }`  
  Creates a WhatsApp sender via Twilio. Returns `{ "sid": "…" }`.

- DELETE `/whatsapp/senders/{sid}`  
  Deletes the WhatsApp sender with given SID.

### Email (prefix `/email`)

- POST `/email/create`  
  JSON body: `{ "firstName": "…", "lastName": "…" }`  
  Creates a Google Workspace user. Returns the created user resource.

- DELETE `/email/delete`  
  JSON body: `{ "primaryEmail": "user@domain" }`  
  Deletes a Google Workspace user.

- POST `/email/send`  
  JSON body: `{ "from": "…", "to": "…", "cc": ["…"], "bcc": ["…"], "subject": "…", "body": "…" }`  
  Sends an email via Gmail API.

- POST `/email/watch`  
  JSON body: `{ "userEmail": "user@domain" }`  
  Starts a Gmail watch on the user's mailbox for push notifications. Returns `{ "success": true, "historyId": "..." }`.

- POST `/email/reply`  
  Receives a Pub/Sub push payload from Gmail watch topic. Fetches the latest unread message (newer than 1 day), sends it to Unify AI to generate a reply, and sends the reply in-thread. Returns `{ "success": true, "replyId": "..." }`.

## Deployment

Use GitHub Actions or Cloud Build to build and deploy this app. See `.github/workflows` for examples targeting Cloud Run and GCE VM.

## Demos

1. SMS
- Text +15086824873. Due to Twilio trial, there's limitation so simple conversation is suggested, e.g., How are you doing?
- The reply will be through a 5-digit business number, assumed it's due to trial. Otherwise, will investigate later on.
- Simple LLM generation for now, further integration with `unity` is required for task logging and context.

2. Phone Call
- The below LIVEKIT keys are required temporarily before config are made in the new shared account.

`LIVEKIT_URL=wss://unity-test-czi67ew5.livekit.cloud`

`LIVEKIT_API_KEY=REDACTED_LIVEKIT_VALUE`

`LIVEKIT_API_SECRET=REDACTED_LIVEKIT_VALUE`

- In `unity`, execute `python make_call.py dev`. Wait for about 10 seconds in case the launch is pending.
- Call the same number above. The assistant will start greeting and have a conversation like in the console.
- Out of the box with `unity` so task logging and context handling should be in place.

3. Email
- Send mails to `unity.agent@unify.ai`.
- Wait for awhile for auto reply. The `Automated reply:` phrase is temporarily kept in the replies for testing purposes.
- Simple LLM generation for now, further integration with `unity` is required for task logging and context.