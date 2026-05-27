"""Gmail Push Notifications via Pub/Sub.

gmail.users.watch() tells Gmail to send a Pub/Sub message whenever the inbox changes.
The push message contains only { emailAddress, historyId } — not the email content.
We use history().list() to get the actual delta since the last known historyId.

Watch expires after ~7 days. Must be renewed before expiration.
"""
import os
from gmail_service import get_gmail_service


def register_watch(topic_name: str | None = None) -> dict:
    service = get_gmail_service()

    if not topic_name:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT_ID", "")
        topic_name = f"projects/{project_id}/topics/gmail-push"

    request_body = {
        "labelIds": ["INBOX"],
        "topicName": topic_name,
        "labelFilterBehavior": "INCLUDE",
    }

    result = service.users().watch(userId="me", body=request_body).execute()
    print(f"[Gmail Watch] Registered. historyId={result.get('historyId')}, expiration={result.get('expiration')}")
    return result


def stop_watch():
    service = get_gmail_service()
    service.users().stop(userId="me").execute()
    print("[Gmail Watch] Stopped.")


def get_new_messages_since(history_id: str) -> list[str]:
    """Returns list of message IDs added since the given historyId."""
    service = get_gmail_service()
    message_ids = []

    try:
        response = (
            service.users()
            .history()
            .list(userId="me", startHistoryId=history_id, historyTypes=["messageAdded"])
            .execute()
        )

        for record in response.get("history", []):
            for msg_added in record.get("messagesAdded", []):
                msg = msg_added.get("message", {})
                labels = msg.get("labelIds", [])
                if "INBOX" in labels and "AI-Processed" not in labels:
                    message_ids.append(msg["id"])

    except Exception as e:
        if "404" in str(e) or "notFound" in str(e):
            print(f"[Gmail Watch] historyId {history_id} expired. Full sync needed.")
        else:
            raise

    return message_ids


if __name__ == "__main__":
    result = register_watch()
    print(f"Watch result: {result}")
