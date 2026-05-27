import os
import asyncio
import base64
import json
from dotenv import load_dotenv

load_dotenv(override=True)

from fastapi import FastAPI, Request, BackgroundTasks
import requests as sync_requests

from db import init_db, get_all_pending, get_pending_replies, get_gmail_state, set_gmail_state
from orchestrator import (
    run_batch_phase_a, run_phase_a_async, run_phase_b,
    handle_email_selection, handle_action_taken, handle_ignore, handle_hand_to_agent,
    send_digest,
)
from gmail_service import get_gmail_service, get_or_create_processed_label, fetch_new_emails, extract_body_text

app = FastAPI(title="Inbox Agent")

BASE_URL = os.environ.get("BASE_URL", "")


@app.on_event("startup")
async def startup():
    await init_db()

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if bot_token and BASE_URL:
        webhook_url = f"{BASE_URL}/webhook/telegram"
        resp = sync_requests.post(
            f"https://api.telegram.org/bot{bot_token}/setWebhook",
            json={"url": webhook_url},
        )
        print(f"[Telegram] Webhook set: {resp.json()}")

    if BASE_URL:
        try:
            from gmail_watch import register_watch
            result = await asyncio.to_thread(register_watch)
            await set_gmail_state("history_id", str(result.get("historyId", "")))
        except Exception as e:
            print(f"[Gmail Watch] Registration failed (will use manual triggers): {e}")

    print("[Server] Ready.")


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Receives Telegram button presses and text messages for Phase B."""
    data = await request.json()

    if "callback_query" in data:
        cb = data["callback_query"]
        callback_data = cb["data"]
        tg_message_id = cb["message"]["message_id"]

        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        sync_requests.get(
            f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
            params={"callback_query_id": cb["id"]},
        )

        # --- Queue management callbacks ---
        if callback_data.startswith("SELECT:"):
            reply_id = int(callback_data.split(":")[1])
            await handle_email_selection(reply_id)
            return {"status": "ok"}

        if callback_data.startswith("ACTION_TAKEN:"):
            reply_id = int(callback_data.split(":")[1])
            await handle_action_taken(reply_id)
            return {"status": "ok"}

        if callback_data.startswith("HAND_TO_AGENT:"):
            reply_id = int(callback_data.split(":")[1])
            await handle_hand_to_agent(reply_id)
            return {"status": "ok"}

        if callback_data.startswith("IGNORE:"):
            reply_id = int(callback_data.split(":")[1])
            await handle_ignore(reply_id)
            return {"status": "ok"}

        # --- Draft approval callbacks (after Hand to Agent) ---
        if callback_data == "EDIT":
            await run_phase_b(tg_message_id, "EDIT")
            return {"status": "waiting_for_feedback"}

        result = await run_phase_b(tg_message_id, callback_data)
        return {"status": "ok", "decision": result}

    if "message" in data:
        text = data["message"].get("text", "")
        if text.startswith("/"):
            return {"status": "ignored_command"}

        # Text input = feedback for the most recent draft being edited
        pending = await get_all_pending()
        if pending:
            latest = pending[-1]
            result = await run_phase_b(
                latest["telegram_message_id"], "EDIT", feedback_text=text
            )
            return {"status": "ok", "result": result}

    return {"status": "no_action"}


@app.post("/webhook/gmail")
async def gmail_push(request: Request, bg: BackgroundTasks):
    """Receives Gmail Pub/Sub push notification.

    Pub/Sub sends: {"message": {"data": base64({"emailAddress": "...", "historyId": "..."})}}
    We decode it, use history().list() to get the delta, and process new messages.
    """
    data = await request.json()

    pubsub_message = data.get("message", {}).get("data", "")
    if pubsub_message:
        decoded = json.loads(base64.urlsafe_b64decode(pubsub_message).decode())
        new_history_id = str(decoded.get("historyId", ""))
        print(f"[Gmail Push] historyId={new_history_id}")

        bg.add_task(_process_gmail_delta, new_history_id)
    else:
        bg.add_task(_process_gmail_fallback)

    return {"status": "ok"}


async def _process_gmail_delta(new_history_id: str):
    """Use history API to get only new messages since last known historyId."""
    last_history_id = await get_gmail_state("history_id")

    if not last_history_id:
        print("[Gmail Push] No stored historyId. Falling back to full unread scan.")
        await _process_gmail_fallback()
        await set_gmail_state("history_id", new_history_id)
        return

    from gmail_watch import get_new_messages_since
    message_ids = await asyncio.to_thread(get_new_messages_since, last_history_id)

    if not message_ids:
        print("[Gmail Push] No new inbox messages in delta.")
        await set_gmail_state("history_id", new_history_id)
        return

    print(f"[Gmail Push] Found {len(message_ids)} new messages via history delta.")

    service = get_gmail_service()
    label_id = get_or_create_processed_label(service)

    for msg_id in message_ids:
        try:
            msg_data = service.users().messages().get(userId="me", id=msg_id).execute()
            payload = msg_data.get("payload", {})
            headers = payload.get("headers", [])

            def header(name):
                return next((h["value"] for h in headers if h["name"] == name), "")

            email_data = {
                "id": msg_id,
                "thread_id": msg_data.get("threadId", ""),
                "subject": header("Subject") or "No Subject",
                "date_received": header("Date") or "N/A",
                "sender_email": header("From") or "N/A",
                "message_id": header("Message-ID"),
                "body": extract_body_text(payload),
            }
            await run_phase_a_async(email_data)
        except Exception as e:
            print(f"[Gmail Push] Error processing message {msg_id}: {e}")

    await set_gmail_state("history_id", new_history_id)


async def _process_gmail_fallback():
    """Fallback: full unread scan when historyId isn't available."""
    await asyncio.to_thread(run_batch_phase_a)


@app.post("/run/batch-analyze")
async def batch_analyze(bg: BackgroundTasks):
    """Manual trigger: fetch all unread emails, run Phase A on each."""
    bg.add_task(asyncio.to_thread, run_batch_phase_a)
    return {"status": "started", "message": "Batch analysis started in background."}


@app.post("/admin/renew-watch")
async def renew_watch():
    """Renew Gmail push notification watch (expires every ~7 days)."""
    from gmail_watch import register_watch
    result = await asyncio.to_thread(register_watch)
    await set_gmail_state("history_id", str(result.get("historyId", "")))
    return {"status": "renewed", "expiration": result.get("expiration")}


@app.post("/run/send-digest")
async def manual_digest():
    """Manually trigger the Telegram digest of pending replies."""
    await send_digest()
    pending = await get_pending_replies()
    return {"status": "sent", "pending_replies": len(pending)}


@app.get("/status")
async def status():
    """Health check + queue state."""
    pending_approvals = await get_all_pending()
    pending_replies = await get_pending_replies()
    history_id = await get_gmail_state("history_id")
    return {
        "status": "running",
        "pending_replies": len(pending_replies),
        "pending_approvals": len(pending_approvals),
        "last_history_id": history_id,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
