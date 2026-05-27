"""Telegram long-polling listener for Phase B.

Handles button taps and text replies without needing a public webhook URL.

Usage:
    Terminal 1: ./run.sh          ← process new emails
    Terminal 2: ./listen.sh       ← handle Telegram button taps
"""
import os
import asyncio
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")


def _tg(method: str, **kwargs) -> dict:
    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=kwargs,
        timeout=35,
    )
    return resp.json()


async def _dispatch_callback(
    data: str,
    tg_message_id: int,
    pending_edit: dict,
    pending_write_own: dict,
    pending_write_own_init: dict,
    chat_id: str,
):
    from orchestrator import (
        run_phase_b,
        handle_email_selection,
        handle_action_taken,
        handle_ignore,
        handle_hand_to_agent,
        handle_draft_for_me,
        handle_ignore_email,
        handle_show_email,
        handle_send_own_initial,
        handle_refine_own_initial,
        handle_followup_draft,
        handle_followup_dismiss,
        handle_clear_all_reminders,
    )

    # ── Initial decision buttons ─────────────────────────────────────────────
    if data == "DRAFT_FOR_ME":
        await handle_draft_for_me(tg_message_id)
        return

    if data == "WRITE_OWN_INITIAL":
        pending_write_own_init[chat_id] = tg_message_id
        _tg("sendMessage",
            chat_id=chat_id,
            text="Type your reply. I'll add a greeting and signature if missing, then send it:")
        return

    if data == "SHOW_EMAIL":
        await handle_show_email(tg_message_id)
        return

    if data == "IGNORE_EMAIL":
        await handle_ignore_email(tg_message_id)
        return

    if data == "CLEAR_ALL_REMINDERS":
        await handle_clear_all_reminders()
        return

    if data == "SEND_OWN_INITIAL":
        await handle_send_own_initial(tg_message_id)
        return

    if data == "REFINE_OWN_INITIAL":
        await handle_refine_own_initial(tg_message_id)
        return

    # ── Draft approval buttons ───────────────────────────────────────────────
    if data in ("APPROVE", "HOLD", "REJECT"):
        await run_phase_b(tg_message_id, data)

    elif data == "EDIT":
        pending_edit[chat_id] = tg_message_id
        await run_phase_b(tg_message_id, "EDIT")

    elif data == "WRITE_OWN":
        pending_write_own[chat_id] = tg_message_id
        await run_phase_b(tg_message_id, "WRITE_OWN")

    elif data.startswith("SELECT:"):
        await handle_email_selection(int(data.split(":")[1]))

    elif data.startswith("ACTION_TAKEN:"):
        await handle_action_taken(int(data.split(":")[1]))

    elif data.startswith("HAND_TO_AGENT:"):
        await handle_hand_to_agent(int(data.split(":")[1]))

    elif data.startswith("IGNORE:"):
        await handle_ignore(int(data.split(":")[1]))

    elif data.startswith("FOLLOWUP_DRAFT:"):
        await handle_followup_draft(int(data.split(":")[1]))

    elif data.startswith("FOLLOWUP_DISMISS:"):
        await handle_followup_dismiss(int(data.split(":")[1]))


async def listen():
    from db import init_db
    await init_db()

    offset = 0
    pending_edit:           dict[str, int] = {}  # awaiting Edit feedback text
    pending_write_own:      dict[str, int] = {}  # awaiting Write My Own body (approval flow)
    pending_write_own_init: dict[str, int] = {}  # awaiting Write My Own body (initial decision)

    print("Listener started. Waiting for Telegram updates... (Ctrl-C to stop)")

    while True:
        try:
            result = _tg("getUpdates", offset=offset, timeout=30)
        except Exception as e:
            print(f"getUpdates error: {e}")
            await asyncio.sleep(5)
            continue

        for update in result.get("result", []):
            offset = update["update_id"] + 1

            if "callback_query" in update:
                q = update["callback_query"]
                chat_id = str(q["message"]["chat"]["id"])
                if chat_id != CHAT_ID:
                    continue

                _tg("answerCallbackQuery", callback_query_id=q["id"])
                tg_msg_id = q["message"]["message_id"]
                print(f"[Button] {q['data']} on msg {tg_msg_id}")

                try:
                    await _dispatch_callback(q["data"], tg_msg_id, pending_edit, pending_write_own, pending_write_own_init, chat_id)
                except Exception as e:
                    print(f"   Error handling callback: {e}")

            elif "message" in update:
                msg = update["message"]
                chat_id = str(msg["chat"]["id"])
                if chat_id != CHAT_ID:
                    continue

                text = msg.get("text", "").strip()
                if not text or text.startswith("/"):
                    continue

                if chat_id in pending_edit:
                    tg_msg_id = pending_edit.pop(chat_id)
                    print(f"[Feedback] Rewriting draft with: {text[:60]}...")
                    from orchestrator import run_phase_b
                    try:
                        await run_phase_b(tg_msg_id, "EDIT", text)
                    except Exception as e:
                        print(f"   Error rewriting draft: {e}")

                elif chat_id in pending_write_own:
                    tg_msg_id = pending_write_own.pop(chat_id)
                    print(f"[Write My Own] Sending user's text verbatim...")
                    from orchestrator import run_phase_b
                    try:
                        await run_phase_b(tg_msg_id, "WRITE_OWN", text)
                    except Exception as e:
                        print(f"   Error sending own body: {e}")

                elif chat_id in pending_write_own_init:
                    tg_msg_id = pending_write_own_init.pop(chat_id)
                    print(f"[Write My Own Initial] Sending user's text verbatim...")
                    from orchestrator import handle_write_own_initial
                    try:
                        await handle_write_own_initial(tg_msg_id, text)
                    except Exception as e:
                        print(f"   Error sending own reply: {e}")

                elif "clear reminder" in text.lower() or "clear all reminder" in text.lower():
                    print(f"[Command] Clear all reminders requested.")
                    from orchestrator import handle_clear_all_reminders
                    try:
                        await handle_clear_all_reminders()
                    except Exception as e:
                        print(f"   Error clearing reminders: {e}")

                elif "reminder" in text.lower():
                    print(f"[Command] Reminder digest requested.")
                    from orchestrator import send_reminder_digest
                    try:
                        await send_reminder_digest()
                    except Exception as e:
                        print(f"   Error sending digest: {e}")

                elif "followup" in text.lower() or "follow up" in text.lower():
                    print(f"[Command] Follow-up check requested.")
                    from orchestrator import check_followups
                    try:
                        count = await check_followups(days=5)
                        if count == 0:
                            _tg("sendMessage", chat_id=chat_id,
                                text="No stale threads! All your replies have been responded to.")
                    except Exception as e:
                        print(f"   Error checking followups: {e}")

                elif "dashboard" in text.lower() or "stats" in text.lower():
                    print(f"[Command] Dashboard requested.")
                    from orchestrator import send_dashboard
                    try:
                        await send_dashboard()
                    except Exception as e:
                        print(f"   Error sending dashboard: {e}")

                elif "response time" in text.lower() or "speed" in text.lower():
                    print(f"[Command] Response time stats requested.")
                    from orchestrator import send_response_stats
                    try:
                        await send_response_stats()
                    except Exception as e:
                        print(f"   Error sending response stats: {e}")

                elif "brief" in text.lower() or "morning" in text.lower():
                    print(f"[Command] Morning brief requested.")
                    from orchestrator import send_morning_brief
                    try:
                        await send_morning_brief()
                    except Exception as e:
                        print(f"   Error sending morning brief: {e}")

                elif "profile" in text.lower():
                    print(f"[Command] Profile requested.")
                    from orchestrator import send_profile
                    try:
                        await send_profile()
                    except Exception as e:
                        print(f"   Error sending profile: {e}")

                elif "help" in text.lower() or "commands" in text.lower():
                    _tg("sendMessage", chat_id=chat_id, text=(
                        "*Available commands:*\n\n"
                        "📬 *reminders* — pending action items\n"
                        "🧹 *clear reminders* — dismiss all reminders\n"
                        "⏰ *followup* — check stale threads\n"
                        "📊 *dashboard* / *stats* — job search pipeline\n"
                        "⚡ *response time* / *speed* — reply speed stats\n"
                        "☀️ *brief* / *morning* — daily summary\n"
                        "👤 *profile* — view your profile data\n"
                        "❓ *help* — this message"
                    ), parse_mode="Markdown")


if __name__ == "__main__":
    asyncio.run(listen())
