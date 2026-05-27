import os
import base64
from dotenv import load_dotenv

load_dotenv(override=True)

from email.message import EmailMessage
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def get_gmail_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


_label_cache: dict[str, str] = {}


def get_or_create_label(service, name: str) -> str:
    if name in _label_cache:
        return _label_cache[name]
    results = service.users().labels().list(userId="me").execute()
    for label in results.get("labels", []):
        if label["name"] == name:
            _label_cache[name] = label["id"]
            return label["id"]
    created = service.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    _label_cache[name] = created["id"]
    return created["id"]


def apply_label(service, msg_id: str, label_id: str):
    service.users().messages().modify(
        userId="me", id=msg_id, body={"addLabelIds": [label_id]}
    ).execute()


def get_or_create_processed_label(service) -> str:
    label_name = "AI-Processed"
    results = service.users().labels().list(userId="me").execute()
    for label in results.get("labels", []):
        if label["name"] == label_name:
            return label["id"]

    label_body = {
        "name": label_name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    created = service.users().labels().create(userId="me", body=label_body).execute()
    return created["id"]


def extract_body_text(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode()
    if "parts" in payload:
        for part in payload["parts"]:
            text = extract_body_text(part)
            if text:
                return text
    return ""


def fetch_new_emails(service) -> list[dict]:
    results = (
        service.users()
        .messages()
        .list(userId="me", q="is:unread in:inbox -label:AI-Processed")
        .execute()
    )
    raw_messages = results.get("messages", [])
    if not raw_messages:
        return []

    emails = []
    for msg in raw_messages:
        msg_data = service.users().messages().get(userId="me", id=msg["id"]).execute()
        payload = msg_data.get("payload", {})
        headers = payload.get("headers", [])

        def header(name):
            return next((h["value"] for h in headers if h["name"] == name), "")

        emails.append(
            {
                "id": msg["id"],
                "thread_id": msg["threadId"],
                "subject": header("Subject") or "No Subject",
                "date_received": header("Date") or "N/A",
                "sender_email": header("From") or "N/A",
                "message_id": header("Message-ID"),
                "body": extract_body_text(payload),
            }
        )
    return emails


def send_reply(service, thread_id: str, message_id: str, sender: str, subject: str, body: str):
    reply = EmailMessage()
    reply.set_content(body)
    reply["To"] = sender
    reply["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
    reply["In-Reply-To"] = message_id
    reply["References"] = message_id

    encoded = base64.urlsafe_b64encode(reply.as_bytes()).decode()
    service.users().messages().send(
        userId="me", body={"raw": encoded, "threadId": thread_id}
    ).execute()


def save_draft(service, thread_id: str, message_id: str, sender: str, subject: str, body: str):
    reply = EmailMessage()
    reply.set_content(body)
    reply["To"] = sender
    reply["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
    reply["In-Reply-To"] = message_id
    reply["References"] = message_id

    encoded = base64.urlsafe_b64encode(reply.as_bytes()).decode()
    service.users().drafts().create(
        userId="me",
        body={"message": {"raw": encoded, "threadId": thread_id}},
    ).execute()


def mark_as_processed(service, msg_id: str, label_id: str):
    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"removeLabelIds": ["UNREAD"], "addLabelIds": [label_id]},
    ).execute()
