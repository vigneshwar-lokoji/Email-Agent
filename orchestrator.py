"""Two-path orchestrator.

Path 1 — Reply emails (Action Type == "Reply"):
  classify → sheet → drafter → critic → Telegram approval card (direct)

Path 2 — Everything else job-related (forms, confirmations, schedules, etc.):
  classify → sheet → add to reminders queue → Telegram reminder update

Both notifications show up in your Telegram. You choose what to do with each.
"""
import os
import re
import asyncio
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

from graph import app
from action_nodes import log_usage_event
from gmail_service import (
    get_gmail_service,
    get_or_create_processed_label,
    get_or_create_label,
    apply_label,
    fetch_new_emails,
    send_reply,
    save_draft,
    mark_as_processed,
)
from db import (
    init_db,
    add_pending_reply,
    get_pending_replies,
    get_pending_reply_by_id,
    resolve_pending_reply,
    store_pending_approval,
    get_pending_approval,
    mark_approval_resolved,
    update_draft,
    store_pending_decision,
    get_pending_decision,
    resolve_pending_decision,
    log_sent_reply,
    log_email_processed,
    get_stale_threads,
    mark_followup_notified,
    mark_response_received,
    dismiss_followup,
    get_pending_decisions_older_than,
    get_response_time_stats,
)

PRIORITY_EMOJI = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}

ACTION_LABEL = {
    "Schedule Interview": "Schedule an interview",
    "Submit Task":        "Submit a task/assignment",
    "Send Documents":     "Send documents",
    "Negotiate":          "Negotiate offer/terms",
    "Decline":            "Decline this opportunity",
    "None":               "No action needed (FYI)",
}

# Per-intent recommendation shown in the notification card
RECOMMENDATION = {
    "Scheduling":      "Reply with 2-3 available time slots — the recruiter is waiting on your calendar.",
    "Outreach":        "Reply to express interest and ask for more details about the role.",
    "Request for Info":"They need specific info from you — reply with the requested details promptly.",
    "Task Assignment": "Acknowledge receipt and ask any clarifying questions before starting.",
    "Offer":           "Review the full offer carefully before responding. Negotiate if needed.",
    "Follow-up":       "Stay engaged — a quick reply keeps you top of mind.",
    "Rejection":       "No action needed, but a brief thank-you reply can leave a good impression.",
}

# Per-action-type suggestion shown in reminder digest
REMINDER_SUGGESTION = {
    "Submit Task":        "Open the link in the email and complete the form/assessment.",
    "Schedule Interview": "Reply or click the booking link to lock in your interview slot.",
    "Send Documents":     "Attach the requested files (resume, ID, etc.) and send them.",
    "Negotiate":          "Review the offer terms and prepare your counter-response.",
    "Decline":            "Send a polite decline if you're no longer interested.",
    "Check & Action":     "Read the email and take the next appropriate step.",
}


INTERVIEW_STAGES = {
    "Recruiter Screen", "Phone Screen", "Technical Round",
    "System Design", "Behavioral", "Onsite", "Final Round", "Offer Stage",
}


def _gmail_label_for(category: str, final: dict) -> str:
    """Return the Gmail label name to apply based on category and extracted data."""
    if category == "job":
        extracted = final.get("extracted_data", {})
        if extracted.get("Final Status") == "Rejected":
            return "Job - Rejected"
        if extracted.get("Current Stage") in INTERVIEW_STAGES:
            return "Job - Interview"
        return "Job - Applied"
    return {
        "personal":      "Personal",
        "bank":          "Finance",
        "business":      "Business",
        "study":         "Study",
        "advertisement": "Advertisements",
        "spam":          "Spam",
    }.get(category, "")


_GREETING_STARTERS = ("hi ", "hello ", "hey ", "dear ", "good morning", "good afternoon", "good evening", "greetings")
_SIG_WORDS         = ("regards", "thanks", "best", "sincerely", "cheers", "yours", "warm regards", "kind regards")


def _parse_sender_first_name(sender_email: str) -> str:
    m = re.match(r'^"?([^"<]+)"?\s*<', sender_email)
    if m:
        full = m.group(1).strip()
        return full.split()[0] if full else "there"
    return "there"


def _ensure_greeting_and_signature(body: str, sender_email: str) -> tuple[str, list[str]]:
    """Add greeting and/or signature if missing. Returns (patched_body, [changes])."""
    changes: list[str] = []
    body = body.strip()

    first_line = (body.split("\n")[0] + " ").lower()
    if not any(first_line.startswith(g) for g in _GREETING_STARTERS):
        name = _parse_sender_first_name(sender_email)
        body = f"Hi {name},\n\n{body}"
        changes.append(f"greeting (Hi {name})")

    if not any(w in body[-250:].lower() for w in _SIG_WORDS):
        try:
            from action_nodes import read_profile
            user_name = (read_profile().get("Name") or os.environ.get("USER_FIRST_NAME", "")).split()[0]
        except Exception:
            user_name = os.environ.get("USER_FIRST_NAME", "")
        body = f"{body}\n\nBest regards,\n{user_name}"
        changes.append("signature")

    return body, changes


def _tg(method: str, **kwargs) -> dict:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    resp = requests.post(f"https://api.telegram.org/bot{bot_token}/{method}", json=kwargs)
    return resp.json()


# ── Core graph runner ──────────────────────────────────────────────────────────

def _run_graph(email_data: dict) -> dict:
    """Run the classification graph for one email. Returns final state."""
    initial_state = {
        "thread_id": email_data["thread_id"],
        "date_received": email_data["date_received"],
        "sender_email": email_data["sender_email"],
        "message_id": email_data["message_id"],
        "subject": email_data["subject"],
        "email_content": f"Subject: {email_data['subject']}\n\n{email_data['body']}",
        "retry_count": 0,
    }
    config = {"configurable": {"thread_id": email_data["thread_id"]}}

    for output in app.stream(initial_state, config=config):
        for key in output:
            print(f"   {key}")

    return app.get_state(config).values


# ── Phase A: classify + route ──────────────────────────────────────────────────

def _log_email(email_data: dict, category: str, action: str):
    """Log every email to both SQLite (email_log) and Google Sheet (Usage tab)."""
    try:
        asyncio.run(log_email_processed(
            gmail_message_id=email_data.get("message_id", ""),
            sender_email=email_data.get("sender_email", ""),
            subject=email_data.get("subject", ""),
            category=category,
            action_taken=action,
        ))
    except Exception as e:
        print(f"   [email_log] DB error: {e}")
    try:
        log_usage_event("received")
        log_usage_event("processed", category)
    except Exception:
        pass


def run_phase_a(email_data: dict):
    """Classify one email and route it to either direct draft or reminders."""
    print(f"[Phase A] {email_data['subject']}")

    # If we previously replied to this thread, mark that we got a response
    asyncio.run(mark_response_received(email_data["thread_id"]))

    final = _run_graph(email_data)
    extracted = final.get("extracted_data", {})
    is_job_related = final.get("is_job_related", False)

    category = final.get("email_category", "spam")

    if category == "spam":
        print("   Spam. Ignored.")
        _log_email(email_data, "spam", "ignored")
        _mark_processed(email_data)
        return

    if category == "advertisement":
        print("   Advertisement (job board digest / promo). Labelling and skipping.")
        try:
            service = get_gmail_service()
            label_id = get_or_create_label(service, "Advertisements")
            apply_label(service, email_data["id"], label_id)
            print("   Labelled: Advertisements")
        except Exception as e:
            print(f"   Label error: {e}")
        _log_email(email_data, "advertisement", "labelled")
        _mark_processed(email_data)
        return

    if category == "job":
        _route_job_email(email_data, final)
    else:
        _route_general_email(email_data, final, category)

    # Determine action for logging
    action = final.get("general_action", "")
    if category == "job":
        ext = final.get("extracted_data", {})
        action = ext.get("Action Type", "logged")
    _log_email(email_data, category, action or "processed")

    # Apply Gmail category label
    label_name = _gmail_label_for(category, final)
    if label_name:
        try:
            service = get_gmail_service()
            label_id = get_or_create_label(service, label_name)
            apply_label(service, email_data["id"], label_id)
            print(f"   Labelled: {label_name}")
        except Exception as e:
            print(f"   Label error: {e}")

    _mark_processed(email_data)


def _route_job_email(email_data: dict, final: dict):
    """Handle a classified job email."""
    extracted = final.get("extracted_data", {})
    is_job_related = final.get("is_job_related", False)

    if not is_job_related:
        print("   Job category but triage says not job-related. Logged silently.")
        return

    action_type = extracted.get("Action Type", "None")
    final_status = extracted.get("Final Status", "")
    company = extracted.get("Company Name", "")
    stage = extracted.get("Current Stage", "")
    priority = extracted.get("Priority", "Medium")

    # ATS confirmations and rejections are already in the sheet — no notification needed
    if action_type == "None" or final_status == "Rejected":
        print(f"   Logged silently ({final_status or 'no action'}).")
        return

    if action_type == "Reply":
        print(f"   Reply needed → notifying (job) for {company}")
        _notify_and_ask(email_data, extracted, category="job")
    else:
        print(f"   Queuing job reminder ({action_type}) for {company}")
        asyncio.run(
            _queue_reminder(email_data, company, stage, priority, action_type)
        )


def _route_general_email(email_data: dict, final: dict, category: str):
    """Handle a personal / business / study / bank email."""
    general_action = final.get("general_action", "spam")
    summary = final.get("general_summary", "")

    if general_action == "spam":
        print("   General email classified as spam. Ignored.")
        return

    # Bank/finance emails that need attention → notify immediately (money-related)
    if category == "bank" and general_action == "needs_attention":
        print(f"   [{category}] money-related action needed → notifying immediately...")
        _notify_and_ask(email_data, {}, category=category, summary=summary)
        return

    # Other needs_attention emails → log silently, no reminder, no notification.
    # Only truly actionable items (needs_reply) get surfaced.
    if general_action == "needs_attention":
        print(f"   [{category}] needs_attention → logged silently (no action needed from you).")
        return

    if general_action == "needs_reply":
        print(f"   [{category}] needs reply → notifying...")
        _notify_and_ask(email_data, {}, category=category, summary=summary)


def _mark_processed(email_data: dict):
    service = get_gmail_service()
    label_id = get_or_create_processed_label(service)
    mark_as_processed(service, email_data["id"], label_id)


# ── Notify-first flow ──────────────────────────────────────────────────────────

def _notify_and_ask(
    email_data: dict,
    extracted: dict,
    category: str = "job",
    summary: str = "",
):
    """Send a notification asking what to do — no draft yet."""
    import json as _json
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    company   = extracted.get("Company Name") or f"[{category.capitalize()}]"
    intent    = extracted.get("Email Intent", "")
    ai_summary = extracted.get("Summary") or summary or email_data["subject"]
    rec       = RECOMMENDATION.get(intent, "Reply needed — the sender is waiting for your response.")
    emoji     = PRIORITY_EMOJI.get(extracted.get("Priority", "High"), "🔴")

    resp = _tg(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"{emoji} *{company}*\n"
            f"_{email_data['subject']}_\n\n"
            f"{ai_summary}\n\n"
            f"📋 *Suggested:* {rec}\n\n"
            f"What would you like to do?"
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "Show Email",            "callback_data": "SHOW_EMAIL"}],
                [{"text": "Draft a Reply for me",  "callback_data": "DRAFT_FOR_ME"}],
                [{"text": "Write My Own",           "callback_data": "WRITE_OWN_INITIAL"}],
                [{"text": "Ignore",                 "callback_data": "IGNORE_EMAIL"}],
            ]
        },
        parse_mode="Markdown",
    )
    tg_message_id = resp.get("result", {}).get("message_id", 0)

    asyncio.run(store_pending_decision(
        gmail_thread_id=email_data["thread_id"],
        gmail_message_id=email_data["message_id"],
        sender_email=email_data["sender_email"],
        subject=email_data["subject"],
        email_body=email_data["body"],
        extracted_data=_json.dumps(extracted),
        telegram_message_id=tg_message_id,
        category=category,
        recommendation=rec,
    ))


async def handle_draft_for_me(telegram_message_id: int):
    """User tapped 'Draft a Reply for me' — run drafter then show approval card."""
    import json as _json
    decision = await get_pending_decision(telegram_message_id)
    if not decision:
        return

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    extracted = _json.loads(decision["extracted_data"] or "{}")
    company = extracted.get("Company Name") or decision["sender_email"]

    _tg("sendMessage", chat_id=chat_id,
        text=f"Drafting a reply to *{company}*...", parse_mode="Markdown")

    from ai_nodes import drafter_agent, critic_agent, general_drafter_agent

    state = {
        "email_content": f"Subject: {decision['subject']}\n\n{decision['email_body']}",
        "subject":       decision["subject"],
        "retry_count":   0,
        "critic_feedback": "",
        "draft_body":    "",
        "extracted_data": extracted,
    }
    drafter = drafter_agent if decision["category"] == "job" else general_drafter_agent

    for _ in range(3):
        state.update(await asyncio.to_thread(drafter, state))
        state.update(await asyncio.to_thread(critic_agent, state))
        if state.get("critic_pass") == "PASS":
            break

    draft_body = state.get("draft_body", "")
    resp = _tg(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"*Draft — {company}*\n\n"
            f"{draft_body}\n\n"
            f"*Looks good?*"
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "Approve (Send)",    "callback_data": "APPROVE"}],
                [{"text": "Edit & Refine",     "callback_data": "EDIT"}],
                [{"text": "Write My Own",      "callback_data": "WRITE_OWN"}],
                [{"text": "Hold (Save Draft)", "callback_data": "HOLD"}],
                [{"text": "Reject (Ignore)",   "callback_data": "REJECT"}],
            ]
        },
        parse_mode="Markdown",
    )
    new_tg_id = resp.get("result", {}).get("message_id", 0)

    await store_pending_approval(
        gmail_thread_id=decision["gmail_thread_id"],
        gmail_message_id=decision["gmail_message_id"],
        sender_email=decision["sender_email"],
        subject=decision["subject"],
        draft_body=draft_body,
        telegram_message_id=new_tg_id,
    )
    await resolve_pending_decision(telegram_message_id, "DRAFTED")


async def handle_write_own_initial(telegram_message_id: int, body_text: str):
    """User typed their own reply — show preview and ask Refine or Send."""
    decision = await get_pending_decision(telegram_message_id)
    if not decision:
        return

    polished, changes = _ensure_greeting_and_signature(body_text, decision["sender_email"])
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    note = f" (auto-added: {', '.join(changes)})" if changes else ""

    resp = _tg(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"*Your reply preview{note}:*\n\n"
            f"{polished}\n\n"
            f"What next?"
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "Send",   "callback_data": "SEND_OWN_INITIAL"}],
                [{"text": "Refine (AI polish)", "callback_data": "REFINE_OWN_INITIAL"}],
            ]
        },
        parse_mode="Markdown",
    )
    new_tg_id = resp.get("result", {}).get("message_id", 0)

    # Store as a pending approval so Phase B can handle Send/Refine
    await store_pending_approval(
        gmail_thread_id=decision["gmail_thread_id"],
        gmail_message_id=decision["gmail_message_id"],
        sender_email=decision["sender_email"],
        subject=decision["subject"],
        draft_body=polished,
        telegram_message_id=new_tg_id,
    )
    await resolve_pending_decision(telegram_message_id, "WRITE_OWN_PREVIEW")


async def handle_send_own_initial(telegram_message_id: int):
    """User confirmed sending their own written reply."""
    approval = await get_pending_approval(telegram_message_id)
    if not approval:
        return

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    service = get_gmail_service()
    send_reply(service, approval["gmail_thread_id"], approval["gmail_message_id"],
               approval["sender_email"], approval["subject"], approval["draft_body"])
    _tg("sendMessage", chat_id=chat_id,
        text=f"Sent to {approval['sender_email']}.")
    try:
        log_usage_event("sent")
    except Exception:
        pass
    await log_sent_reply(
        gmail_thread_id=approval["gmail_thread_id"],
        gmail_message_id=approval["gmail_message_id"],
        sender_email=approval["sender_email"],
        subject=approval["subject"],
    )
    await mark_approval_resolved(telegram_message_id, "SENT_OWN")


async def handle_refine_own_initial(telegram_message_id: int):
    """User wants AI to refine their own written reply."""
    approval = await get_pending_approval(telegram_message_id)
    if not approval:
        return

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    _tg("sendMessage", chat_id=chat_id, text="Refining your reply...")

    from ai_nodes import llm_text
    prompt = (
        f"Polish and refine the following email reply. "
        f"Keep the same meaning and tone, but improve clarity, grammar, and professionalism. "
        f"Output ONLY the refined email text. No introductory phrases.\n\n"
        f"Original email being replied to:\nSubject: {approval['subject']}\n\n"
        f"User's reply to refine:\n{approval['draft_body']}"
    )
    response = await asyncio.to_thread(llm_text.invoke, prompt)
    refined = response.content.strip()

    resp = _tg(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"*Refined reply:*\n\n"
            f"{refined}\n\n"
            f"*Looks good?*"
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "Approve (Send)",    "callback_data": "APPROVE"}],
                [{"text": "Edit & Refine",     "callback_data": "EDIT"}],
                [{"text": "Hold (Save Draft)", "callback_data": "HOLD"}],
                [{"text": "Reject (Ignore)",   "callback_data": "REJECT"}],
            ]
        },
        parse_mode="Markdown",
    )
    new_tg_id = resp.get("result", {}).get("message_id", 0)

    await store_pending_approval(
        gmail_thread_id=approval["gmail_thread_id"],
        gmail_message_id=approval["gmail_message_id"],
        sender_email=approval["sender_email"],
        subject=approval["subject"],
        draft_body=refined,
        telegram_message_id=new_tg_id,
    )
    await mark_approval_resolved(telegram_message_id, "REFINED")


async def handle_ignore_email(telegram_message_id: int):
    """User chose to ignore the email from the initial notification."""
    await resolve_pending_decision(telegram_message_id, "IGNORED")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    _tg("sendMessage", chat_id=chat_id, text="Got it, ignored.")


def _escape_markdown(text: str) -> str:
    """Escape Telegram Markdown special characters in user-generated text."""
    for ch in ("_", "*", "`", "[", "]", "(", ")", "~", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"):
        text = text.replace(ch, f"\\{ch}")
    return text


async def handle_show_email(telegram_message_id: int):
    """User tapped 'Show Email' — display the full email body with action buttons."""
    decision = await get_pending_decision(telegram_message_id)
    if not decision:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        _tg("sendMessage", chat_id=chat_id,
            text="Could not find that email. It may have already been handled.")
        return

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    body = (decision["email_body"] or "").strip()

    # Telegram messages have a 4096 char limit
    if len(body) > 3500:
        body = body[:3500] + "\n\n... (truncated)"

    # Send as plain text (no parse_mode) to avoid Markdown issues with email content
    resp = _tg(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"From: {decision['sender_email']}\n"
            f"Subject: {decision['subject']}\n\n"
            f"{body}\n\n"
            f"What would you like to do?"
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "Draft a Reply for me",  "callback_data": "DRAFT_FOR_ME"}],
                [{"text": "Write My Own",           "callback_data": "WRITE_OWN_INITIAL"}],
                [{"text": "Ignore",                 "callback_data": "IGNORE_EMAIL"}],
            ]
        },
    )
    # Link the new message to the same pending decision
    new_tg_id = resp.get("result", {}).get("message_id", 0)
    if new_tg_id:
        import json as _json
        await store_pending_decision(
            gmail_thread_id=decision["gmail_thread_id"],
            gmail_message_id=decision["gmail_message_id"],
            sender_email=decision["sender_email"],
            subject=decision["subject"],
            email_body=decision["email_body"],
            extracted_data=decision["extracted_data"] or "{}",
            telegram_message_id=new_tg_id,
            category=decision["category"],
            recommendation=decision["recommendation"],
        )


def _draft_and_notify(email_data: dict, extracted: dict, final_state: dict):
    """Path 1: run drafter + critic, send direct Telegram approval card."""
    from ai_nodes import drafter_agent, critic_agent

    state = {
        "email_content": f"Subject: {email_data['subject']}\n\n{email_data['body']}",
        "subject": email_data["subject"],
        "retry_count": 0,
        "critic_feedback": "",
        "draft_body": "",
        "extracted_data": extracted,
    }

    for attempt in range(3):
        state.update(drafter_agent(state))
        state.update(critic_agent(state))
        if state.get("critic_pass") == "PASS":
            break
        print(f"   Critic FAIL (attempt {attempt + 1}): {state.get('critic_feedback')}")

    draft_body = state.get("draft_body", "")
    company = extracted.get("Company Name", "Unknown")
    stage = extracted.get("Current Stage", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    try:
        log_usage_event("draft")
    except Exception:
        pass

    keyboard = {
        "inline_keyboard": [
            [{"text": "Approve (Send)",    "callback_data": "APPROVE"}],
            [{"text": "Edit & Refine",     "callback_data": "EDIT"}],
            [{"text": "Write My Own",      "callback_data": "WRITE_OWN"}],
            [{"text": "Hold (Save Draft)", "callback_data": "HOLD"}],
            [{"text": "Reject (Ignore)",   "callback_data": "REJECT"}],
        ]
    }

    resp = _tg(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"*{company}* | {stage}\n"
            f"_{email_data['subject']}_\n\n"
            f"{draft_body}\n\n"
            f"*Approve this reply?*"
        ),
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    tg_message_id = resp.get("result", {}).get("message_id", 0)

    asyncio.run(
        store_pending_approval(
            gmail_thread_id=email_data["thread_id"],
            gmail_message_id=email_data["message_id"],
            sender_email=email_data["sender_email"],
            subject=email_data["subject"],
            draft_body=draft_body,
            telegram_message_id=tg_message_id,
        )
    )


def _draft_and_notify_general(email_data: dict, summary: str, category: str):
    """Draft a reply to a personal/business/study email and send for approval."""
    from ai_nodes import general_drafter_agent, critic_agent

    label = f"[{category.capitalize()}]"
    state = {
        "email_content": f"Subject: {email_data['subject']}\n\n{email_data['body']}",
        "subject": email_data["subject"],
        "retry_count": 0,
        "critic_feedback": "",
        "draft_body": "",
    }

    for attempt in range(2):
        state.update(general_drafter_agent(state))
        state.update(critic_agent(state))
        if state.get("critic_pass") == "PASS":
            break

    draft_body = state.get("draft_body", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    try:
        log_usage_event("draft")
    except Exception:
        pass

    resp = _tg(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"*{label} {email_data['sender_email']}*\n"
            f"_{email_data['subject']}_\n\n"
            f"{draft_body}\n\n"
            f"*Approve this reply?*"
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "Approve (Send)",    "callback_data": "APPROVE"}],
                [{"text": "Edit & Refine",     "callback_data": "EDIT"}],
                [{"text": "Write My Own",      "callback_data": "WRITE_OWN"}],
                [{"text": "Hold (Save Draft)", "callback_data": "HOLD"}],
                [{"text": "Reject (Ignore)",   "callback_data": "REJECT"}],
            ]
        },
        parse_mode="Markdown",
    )
    tg_message_id = resp.get("result", {}).get("message_id", 0)

    asyncio.run(
        store_pending_approval(
            gmail_thread_id=email_data["thread_id"],
            gmail_message_id=email_data["message_id"],
            sender_email=email_data["sender_email"],
            subject=email_data["subject"],
            draft_body=draft_body,
            telegram_message_id=tg_message_id,
        )
    )


async def _queue_reminder(
    email_data: dict,
    company: str,
    stage: str,
    priority: str,
    action_type: str,
):
    """Add to reminders. High-priority items notify immediately via Telegram."""
    await add_pending_reply(
        gmail_thread_id=email_data["thread_id"],
        gmail_message_id=email_data["message_id"],
        sender_email=email_data["sender_email"],
        subject=email_data["subject"],
        email_body=email_data["body"],
        company=company,
        stage=stage,
        priority=priority,
        action_type=action_type,
    )
    try:
        log_usage_event("reminder_add")
    except Exception:
        pass

    # High-priority job items → notify immediately, don't wait for user to ask
    if priority == "High":
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        suggestion = REMINDER_SUGGESTION.get(action_type, "Review and take action.")
        _tg(
            "sendMessage",
            chat_id=chat_id,
            text=(
                f"🔴 *Urgent: {company}*\n"
                f"_{email_data['subject']}_\n\n"
                f"Action needed: *{action_type}*\n"
                f"💡 {suggestion}\n\n"
                f"_Added to your reminders. Type 'reminders' to manage._"
            ),
            parse_mode="Markdown",
        )


# ── Reminder digest ────────────────────────────────────────────────────────────

async def send_reminder_update(new_company: str = "", new_action: str = ""):
    """Send (or refresh) the Telegram reminders list.
    Called every time a new reminder is added so the list stays current.
    """
    pending = await get_pending_replies()
    if not pending:
        return

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    header = f"New reminder added ({new_company})\n" if new_company else ""
    lines = [f"{header}*Reminders — {len(pending)} waiting*\n"]
    buttons = []

    for i, item in enumerate(pending, start=1):
        emoji = PRIORITY_EMOJI.get(item["priority"], "⚪")
        company = item["company"] or "Unknown"
        action_label = ACTION_LABEL.get(item["action_type"], item["action_type"])
        lines.append(f"{i}. {emoji} *{company}* — {action_label}")
        buttons.append([{
            "text": f"{i}. {company}",
            "callback_data": f"SELECT:{item['id']}",
        }])

    lines.append("\nTap one to handle it.")

    _tg(
        "sendMessage",
        chat_id=chat_id,
        text="\n".join(lines),
        reply_markup={"inline_keyboard": buttons},
        parse_mode="Markdown",
    )


async def _add_to_reminders_silent(email_data: dict, category: str, summary: str):
    """Queue a needs_attention or bank email silently — no Telegram push."""
    await add_pending_reply(
        gmail_thread_id=email_data["thread_id"],
        gmail_message_id=email_data["message_id"],
        sender_email=email_data["sender_email"],
        subject=email_data["subject"],
        email_body=email_data["body"],
        company=f"[{category.capitalize()}]",
        stage=summary,
        priority="Low",
        action_type="Check & Action",
    )


async def send_reminder_digest():
    """On-demand digest. Called when the user messages the bot 'reminders'."""
    pending = await get_pending_replies()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    user_name = os.environ.get("USER_FIRST_NAME", "there")

    if not pending:
        _tg("sendMessage", chat_id=chat_id,
            text=f"You're all caught up, {user_name}! No pending reminders.")
        return

    lines = [f"Hey {user_name}! You have *{len(pending)} reminder(s)* waiting:\n"]
    buttons = []

    for i, item in enumerate(pending, start=1):
        emoji = PRIORITY_EMOJI.get(item["priority"], "⚪")
        company = item["company"] or "Unknown"
        stage = (item.get("stage") or "")[:70]
        suggestion = REMINDER_SUGGESTION.get(item.get("action_type", ""), "Review and take action.")
        lines.append(f"{i}. {emoji} *{company}*")
        if stage:
            lines.append(f"   _{stage}_")
        lines.append(f"   💡 {suggestion}")
        buttons.append([{"text": f"{i}. {company}", "callback_data": f"SELECT:{item['id']}"}])

    lines.append("\nTap one to handle it, or clear them all.")

    buttons.append([{"text": "Clear All Reminders", "callback_data": "CLEAR_ALL_REMINDERS"}])

    _tg(
        "sendMessage",
        chat_id=chat_id,
        text="\n".join(lines),
        reply_markup={"inline_keyboard": buttons},
        parse_mode="Markdown",
    )


async def send_digest():
    """Manual trigger: send the current reminders list to Telegram."""
    await send_reminder_update()


# ── Insights (time-windowed analytics) ──────────────────────────────────────

PERIOD_LABELS = {
    "today": "Today",
    "yesterday": "Yesterday",
    "week": "This Week",
    "month": "This Month",
}


async def send_insights_menu():
    """Send Telegram message with time-window buttons."""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    _tg(
        "sendMessage",
        chat_id=chat_id,
        text="Pick a time window for your insights:",
        reply_markup={
            "inline_keyboard": [
                [{"text": "Today",     "callback_data": "INSIGHTS:today"}],
                [{"text": "Yesterday", "callback_data": "INSIGHTS:yesterday"}],
                [{"text": "This Week", "callback_data": "INSIGHTS:week"}],
                [{"text": "This Month","callback_data": "INSIGHTS:month"}],
            ]
        },
    )


async def send_insights(period: str):
    """Send time-windowed analytics to Telegram."""
    from db import get_insights_for_period
    from action_nodes import get_sheet_stats_for_period
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    label = PERIOD_LABELS.get(period, period)

    _tg("sendMessage", chat_id=chat_id, text=f"Crunching your {label.lower()} numbers...")

    # Get DB stats (email activity)
    activity = await get_insights_for_period(period)

    # Get sheet stats (job applications) — runs in thread since it's sync
    job_stats = await asyncio.to_thread(get_sheet_stats_for_period, period)

    lines = [f"📊 *Insights — {label}*\n"]

    # ── Email Activity ──
    lines.append("*Email Activity:*")
    lines.append(f"  📬 {activity['emails_received']} received")
    lines.append(f"  ✅ {activity['emails_handled']} handled")
    if activity['emails_pending']:
        lines.append(f"  ⏳ {activity['emails_pending']} still pending")
    lines.append(f"  ✉️ {activity['replies_sent']} replies sent")

    # Breakdown by category
    if activity['by_category']:
        cats = activity['by_category']
        cat_parts = []
        cat_emojis = {"job": "💼", "personal": "👤", "bank": "🏦", "business": "📎", "study": "📚", "advertisement": "📢"}
        for cat, count in sorted(cats.items(), key=lambda x: x[1], reverse=True):
            emoji = cat_emojis.get(cat, "📧")
            cat_parts.append(f"{emoji} {cat}: {count}")
        lines.append(f"  _Breakdown: {' | '.join(cat_parts)}_")
    lines.append("")

    # ── Reminders ──
    if activity['reminders_added'] or activity['reminders_cleared']:
        lines.append("*Reminders:*")
        lines.append(f"  📋 {activity['reminders_added']} added")
        lines.append(f"  🧹 {activity['reminders_cleared']} cleared")
        lines.append("")

    # ── Job Search ──
    if "error" not in job_stats:
        lines.append("*Job Search:*")
        lines.append(f"  📝 {job_stats['applied']} applications tracked")
        lines.append(f"  ❌ {job_stats['rejections']} rejected")
        lines.append(f"  ✅ {job_stats['next_round']} advancing to next round")

        if job_stats['advancing_companies']:
            lines.append("\n  *Moving forward:*")
            for item in job_stats['advancing_companies'][:5]:
                lines.append(f"    🟢 {item['company']} — _{item['stage']}_")

        if job_stats['rejection_companies']:
            lines.append("\n  *Rejections:*")
            for item in job_stats['rejection_companies'][:5]:
                reason = item['reason'] if item['reason'] != 'N/A' else 'No reason given'
                lines.append(f"    🔴 {item['company']} at _{item['stage']}_ — {reason}")
            if len(job_stats['rejection_companies']) > 5:
                lines.append(f"    ... and {len(job_stats['rejection_companies']) - 5} more")

        lines.append("")

    # ── Summary ──
    if activity['emails_received'] == 0 and (job_stats.get('applied', 0) == 0):
        lines.append(f"_Quiet {label.lower()} — no activity recorded._")

    _tg(
        "sendMessage",
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="Markdown",
    )


# ── Job Search Dashboard ─────────────────────────────────────────────────────

async def send_dashboard():
    """Send job search stats to Telegram."""
    from action_nodes import get_dashboard_stats
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    stats = await asyncio.to_thread(get_dashboard_stats)

    if "error" in stats:
        _tg("sendMessage", chat_id=chat_id, text=f"Dashboard error: {stats['error']}")
        return

    lines = ["📊 *Job Search Dashboard*\n"]

    # Overview
    lines.append(f"🏢 Companies applied: *{stats['total_companies']}*")
    lines.append(f"📄 Total applications: *{stats['total_applications']}*")
    lines.append(f"❌ Total rejections: *{stats['total_rejections']}*")
    lines.append(f"🏆 Furthest stage reached: *{stats['furthest_stage']}*")

    # Multiple roles at same company
    if stats["multi_role_companies"]:
        lines.append(f"\n*Multiple roles at one company:*")
        for company, roles in stats["multi_role_companies"].items():
            lines.append(f"  • {company}: {len(roles)} roles")

    # Pipeline breakdown
    if stats["stages"]:
        lines.append(f"\n*Pipeline stages:*")
        for stage, count in stats["stages"]:
            bar = "█" * min(count, 15)
            lines.append(f"  {stage}: {bar} {count}")

    # Rejection details
    if stats["rejections"]:
        lines.append(f"\n*Rejected by ({stats['total_rejections']}):*")
        for rej in stats["rejections"][:10]:  # Show top 10
            stage_info = f" at {rej['stage']}" if rej["stage"] != "N/A" else ""
            lines.append(f"  • {rej['company']}{stage_info}")

    # Top rejection reasons
    if stats["reject_reasons"]:
        lines.append(f"\n*Top rejection reasons:*")
        for reason, count in stats["reject_reasons"][:5]:
            lines.append(f"  • {reason}: {count}")

    _tg(
        "sendMessage",
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="Markdown",
    )


# ── Profile Viewer ───────────────────────────────────────────────────────────

async def send_profile():
    """Show the user's profile data from the Google Sheet."""
    from action_nodes import read_profile
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    profile = await asyncio.to_thread(read_profile)

    if not profile:
        _tg("sendMessage", chat_id=chat_id,
            text=(
                "No profile found. Run `setup_profile.py` on the VM to create the Profile tab, "
                "then fill in row 2 in your Google Sheet."
            ))
        return

    lines = ["👤 *Your Profile*\n"]
    filled = 0
    empty = []

    for key, value in profile.items():
        if value and value.strip():
            lines.append(f"  *{key}:* {value}")
            filled += 1
        else:
            empty.append(key)

    if empty:
        lines.append(f"\n⚠️ *{len(empty)} empty field(s):* {', '.join(empty)}")
        lines.append("_Fill these in your Google Sheet → Profile tab for better drafts._")

    lines.append(f"\n✅ {filled}/{filled + len(empty)} fields filled")

    _tg(
        "sendMessage",
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="Markdown",
    )


# ── Daily Morning Brief ──────────────────────────────────────────────────────

async def send_morning_brief():
    """Send a morning summary focused on reminders + this week's analytics."""
    from db import get_weekly_activity
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    user_name = os.environ.get("USER_FIRST_NAME", "there")
    lines = [f"☀️ *Good morning, {user_name}!*\n"]

    # 1. Pending reminders — what you need to act on
    reminders = await get_pending_replies()
    if reminders:
        high = [r for r in reminders if r.get("priority") == "High"]
        lines.append(f"📋 *{len(reminders)} reminder(s) pending:*")
        if high:
            lines.append(f"  🔴 {len(high)} high priority!")
        for item in reminders[:5]:
            emoji = PRIORITY_EMOJI.get(item["priority"], "⚪")
            company = item["company"] or "Unknown"
            suggestion = REMINDER_SUGGESTION.get(item.get("action_type", ""), "Review and take action.")
            lines.append(f"  {emoji} *{company}* — {suggestion}")
        if len(reminders) > 5:
            lines.append(f"  ... and {len(reminders) - 5} more")
        lines.append("")
    else:
        lines.append("📋 *No pending reminders* — you're all caught up!\n")

    # 2. Follow-ups overdue
    stale = await get_stale_threads(days=5)
    if stale:
        lines.append(f"⏰ *{len(stale)} follow-up(s) needed:*")
        for item in stale[:3]:
            company = item.get("company") or item["sender_email"]
            from datetime import datetime, timezone
            sent_dt = datetime.fromisoformat(item["sent_at"].replace("Z", "+00:00"))
            days_ago = (datetime.now(timezone.utc) - sent_dt).days
            lines.append(f"  • {company} — {days_ago} days, no reply")
        lines.append("")

    # 3. Today's calendar
    try:
        from calendar_service import get_todays_events
        events = await asyncio.to_thread(get_todays_events)
        if events:
            lines.append(f"📅 *Today's schedule:*")
            for ev in events:
                link = f" [Join]({ev['link']})" if ev.get("link") else ""
                lines.append(f"  • {ev['time']} — {ev['summary']}{link}")
            lines.append("")
    except Exception as e:
        print(f"[Morning Brief] Calendar error: {e}", flush=True)

    # 4. This week's analytics
    try:
        weekly = await get_weekly_activity()
        lines.append("📊 *This week so far:*")
        lines.append(f"  📬 {weekly['emails_received']} emails received")
        lines.append(f"  ✅ {weekly['emails_handled']} handled")
        lines.append(f"  ✉️ {weekly['replies_sent']} replies sent")
        lines.append(f"  🧹 {weekly['reminders_cleared']} reminders cleared")
        lines.append("")
    except Exception as e:
        print(f"[Morning Brief] Weekly stats error: {e}", flush=True)

    # 5. Pipeline snapshot
    try:
        from action_nodes import get_dashboard_stats
        stats = await asyncio.to_thread(get_dashboard_stats)
        if "error" not in stats:
            active = stats["total_applications"] - stats["total_rejections"]
            lines.append(
                f"🏢 *Pipeline:* {stats['total_applications']} applied "
                f"| {active} active | {stats['total_rejections']} rejected"
            )
            lines.append("")
    except Exception as e:
        print(f"[Morning Brief] Stats error: {e}", flush=True)

    # 6. Action summary
    total_actions = len(reminders) + len(stale)
    if total_actions == 0:
        lines.append("✅ *Clean slate!* Focus on applications today.")
    else:
        lines.append(f"👉 *{total_actions} action(s)* — type _reminders_ or _followup_ for details.")

    _tg(
        "sendMessage",
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="Markdown",
    )
    print("[Morning Brief] Sent.", flush=True)


# ── Response Time Tracking ────────────────────────────────────────────────────

def _format_time(minutes: float) -> str:
    """Format minutes into human-readable time."""
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h"
    days = hours / 24
    return f"{days:.1f}d"


async def check_response_nudge(hours: int = 3):
    """Nudge user about pending decisions older than N hours."""
    stale = await get_pending_decisions_older_than(hours)
    if not stale:
        return 0

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    count = len(stale)

    subjects = []
    for item in stale[:5]:
        from datetime import datetime, timezone
        created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        hours_ago = (now - created).total_seconds() / 3600
        subjects.append(f"  • _{item['subject']}_ ({_format_time(hours_ago * 60)} ago)")

    _tg(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"⏳ *{count} email(s) waiting for your response*\n\n"
            + "\n".join(subjects)
            + "\n\n_Quick responses keep you top of mind with recruiters._"
        ),
        parse_mode="Markdown",
    )

    print(f"[Nudge] Reminded about {count} pending decision(s).", flush=True)
    return count


async def send_response_stats():
    """Send response time stats to Telegram."""
    stats = await get_response_time_stats()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if stats["count"] == 0:
        _tg("sendMessage", chat_id=chat_id,
            text="No response time data yet. Reply to some emails and check back!")
        return

    lines = ["⚡ *Response Time Stats*\n"]
    lines.append(f"Total replies tracked: *{stats['count']}*\n")
    lines.append(f"📊 *All time:*")
    lines.append(f"  Average: *{_format_time(stats['avg_minutes'])}*")
    lines.append(f"  Fastest: *{_format_time(stats['fastest_minutes'])}*")
    lines.append(f"  Slowest: *{_format_time(stats['slowest_minutes'])}*")

    if stats["this_week_count"] > 0:
        lines.append(f"\n📅 *This week:*")
        lines.append(f"  Replies: *{stats['this_week_count']}*")
        lines.append(f"  Average: *{_format_time(stats['this_week_avg'])}*")

    # Rating
    avg = stats["avg_minutes"]
    if avg <= 60:
        rating = "🟢 Excellent — you're lightning fast!"
    elif avg <= 180:
        rating = "🟡 Good — but there's room to be faster."
    elif avg <= 480:
        rating = "🟠 Okay — try to respond within 2 hours."
    else:
        rating = "🔴 Slow — recruiters may lose interest. Speed it up!"

    lines.append(f"\n{rating}")

    _tg(
        "sendMessage",
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="Markdown",
    )


# ── Follow-up checker ────────────────────────────────────────────────────────

async def check_followups(days: int = 5):
    """Check for threads where we replied but got no response in N days."""
    stale = await get_stale_threads(days)
    if not stale:
        return 0

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    count = 0

    for item in stale:
        sender = item["sender_email"]
        subject = item["subject"]
        company = item["company"] or sender

        # Calculate days since we sent
        from datetime import datetime, timezone
        sent_dt = datetime.fromisoformat(item["sent_at"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days_ago = (now - sent_dt).days

        _tg(
            "sendMessage",
            chat_id=chat_id,
            text=(
                f"⏰ *Follow-up needed*\n\n"
                f"You replied to *{company}* {days_ago} days ago but got no response.\n"
                f"_{subject}_\n\n"
                f"Want to follow up?"
            ),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "Draft Follow-up", "callback_data": f"FOLLOWUP_DRAFT:{item['id']}"}],
                    [{"text": "Dismiss",         "callback_data": f"FOLLOWUP_DISMISS:{item['id']}"}],
                ]
            },
            parse_mode="Markdown",
        )

        await mark_followup_notified(item["id"])
        count += 1

    print(f"[Follow-up] Notified about {count} stale thread(s).", flush=True)
    return count


async def handle_followup_draft(sent_reply_id: int):
    """User wants to draft a follow-up for a stale thread."""
    from db import get_stale_threads
    import aiosqlite

    async with aiosqlite.connect("inbox_agent_app.db") as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM sent_replies WHERE id = ?", (sent_reply_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return
        item = dict(row)

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    company = item["company"] or item["sender_email"]
    _tg("sendMessage", chat_id=chat_id,
        text=f"Drafting follow-up to *{company}*...", parse_mode="Markdown")

    from ai_nodes import llm_text
    from datetime import datetime, timezone

    sent_dt = datetime.fromisoformat(item["sent_at"].replace("Z", "+00:00"))
    days_ago = (datetime.now(timezone.utc) - sent_dt).days

    prompt = (
        f"Write a polite, professional follow-up email. Keep it brief (3-5 sentences).\n"
        f"Context: I replied to this email {days_ago} days ago but haven't heard back.\n"
        f"Original subject: {item['subject']}\n"
        f"Recipient: {item['sender_email']}\n\n"
        f"Output ONLY the email body text. No introductory phrases. "
        f"Include a greeting and sign off."
    )
    response = await asyncio.to_thread(llm_text.invoke, prompt)
    draft = response.content.strip()

    resp = _tg(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"*Follow-up draft — {company}*\n\n"
            f"{draft}\n\n"
            f"*Send this follow-up?*"
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "Approve (Send)",    "callback_data": "APPROVE"}],
                [{"text": "Edit & Refine",     "callback_data": "EDIT"}],
                [{"text": "Hold (Save Draft)", "callback_data": "HOLD"}],
                [{"text": "Reject (Ignore)",   "callback_data": "REJECT"}],
            ]
        },
        parse_mode="Markdown",
    )
    new_tg_id = resp.get("result", {}).get("message_id", 0)

    await store_pending_approval(
        gmail_thread_id=item["gmail_thread_id"],
        gmail_message_id=item["gmail_message_id"] or "",
        sender_email=item["sender_email"],
        subject=item["subject"],
        draft_body=draft,
        telegram_message_id=new_tg_id,
    )
    await dismiss_followup(sent_reply_id)


async def handle_followup_dismiss(sent_reply_id: int):
    """User dismissed a follow-up notification."""
    await dismiss_followup(sent_reply_id)
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    _tg("sendMessage", chat_id=chat_id, text="Follow-up dismissed.")


# ── Batch runner ───────────────────────────────────────────────────────────────

def run_batch_phase_a() -> int:
    try:
        asyncio.get_running_loop()
        # Already in an event loop — init_db was called by main()
    except RuntimeError:
        # No event loop — we're in the Pub/Sub thread
        asyncio.run(init_db())
    service = get_gmail_service()
    emails = fetch_new_emails(service)

    if not emails:
        print("[Batch] No new emails.")
        return 0

    print(f"[Batch] {len(emails)} emails...")
    for email_data in emails:
        try:
            run_phase_a(email_data)
        except Exception as e:
            print(f"   ERROR on {email_data['subject']}: {e}")

    return len(emails)


async def run_phase_a_async(email_data: dict):
    await asyncio.to_thread(run_phase_a, email_data)


# ── Reminder detail card ───────────────────────────────────────────────────────

async def handle_email_selection(reply_id: int):
    """User tapped a reminder. Send detail card with 3 options."""
    item = await get_pending_reply_by_id(reply_id)
    if not item:
        return

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    preview = (item["email_body"] or "")[:300].strip()
    company = item["company"] or "Unknown"
    action_label = ACTION_LABEL.get(item["action_type"], item["action_type"])

    _tg(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"*{company}* | {item.get('stage', '')}\n"
            f"Action needed: _{action_label}_\n"
            f"From: `{item['sender_email']}`\n\n"
            f"{preview}{'...' if len(item['email_body']) > 300 else ''}"
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "Done (I handled it)",      "callback_data": f"ACTION_TAKEN:{reply_id}"}],
                [{"text": "Hand to Agent (draft reply)", "callback_data": f"HAND_TO_AGENT:{reply_id}"}],
                [{"text": "Ignore (dismiss)",           "callback_data": f"IGNORE:{reply_id}"}],
            ]
        },
        parse_mode="Markdown",
    )


async def handle_action_taken(reply_id: int):
    await resolve_pending_reply(reply_id, "ACTION_TAKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    _tg("sendMessage", chat_id=chat_id, text="Marked as done.")
    try:
        log_usage_event("reminder_clear")
    except Exception:
        pass
    await send_reminder_update()


async def handle_ignore(reply_id: int):
    await resolve_pending_reply(reply_id, "IGNORED")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    _tg("sendMessage", chat_id=chat_id, text="Dismissed from queue.")
    try:
        log_usage_event("reminder_clear")
    except Exception:
        pass
    await send_reminder_update()


async def handle_clear_all_reminders():
    """Clear all pending reminders at once."""
    from db import clear_all_reminders
    count = await clear_all_reminders()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if count:
        try:
            for _ in range(count):
                log_usage_event("reminder_clear")
        except Exception:
            pass
        _tg("sendMessage", chat_id=chat_id, text=f"Cleared {count} reminder(s). You're all caught up!")
    else:
        _tg("sendMessage", chat_id=chat_id, text="No reminders to clear.")


async def handle_hand_to_agent(reply_id: int):
    """Draft a reply for a reminder item, then send for approval."""
    item = await get_pending_reply_by_id(reply_id)
    if not item:
        return

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    company = item["company"] or "Unknown"
    _tg("sendMessage", chat_id=chat_id, text=f"Drafting reply to *{company}*...", parse_mode="Markdown")

    from ai_nodes import drafter_agent, critic_agent

    state = {
        "email_content": f"Subject: {item['subject']}\n\n{item['email_body']}",
        "subject": item["subject"],
        "retry_count": 0,
        "critic_feedback": "",
        "draft_body": "",
    }

    for attempt in range(3):
        state.update(await asyncio.to_thread(drafter_agent, state))
        state.update(await asyncio.to_thread(critic_agent, state))
        if state.get("critic_pass") == "PASS":
            break

    draft_body = state.get("draft_body", "")

    resp = _tg(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"*Draft for {company}*\n\n{draft_body}\n\n*Approve this reply?*"
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "Approve (Send)",    "callback_data": "APPROVE"}],
                [{"text": "Edit & Refine",     "callback_data": "EDIT"}],
                [{"text": "Write My Own",      "callback_data": "WRITE_OWN"}],
                [{"text": "Hold (Save Draft)", "callback_data": "HOLD"}],
                [{"text": "Reject (Ignore)",   "callback_data": "REJECT"}],
            ]
        },
        parse_mode="Markdown",
    )
    tg_message_id = resp.get("result", {}).get("message_id", 0)

    await store_pending_approval(
        gmail_thread_id=item["gmail_thread_id"],
        gmail_message_id=item["gmail_message_id"],
        sender_email=item["sender_email"],
        subject=item["subject"],
        draft_body=draft_body,
        telegram_message_id=tg_message_id,
        pending_reply_id=reply_id,
    )


# ── Phase B: approve / edit / reject a draft ──────────────────────────────────

async def run_phase_b(telegram_message_id: int, decision: str, feedback_text: str | None = None):
    approval = await get_pending_approval(telegram_message_id)
    if not approval:
        print(f"[Phase B] No pending approval for tg_msg_id {telegram_message_id}")
        return None

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if decision == "EDIT":
        if not feedback_text:
            _tg("sendMessage", chat_id=chat_id, text="Type your feedback and I'll rewrite the draft.")
            return "WAITING_FOR_FEEDBACK"

        from ai_nodes import llm_text
        prompt = (
            f"Rewrite the following email draft based on the user's feedback.\n"
            f"Output ONLY the raw email text. No introductory phrases.\n\n"
            f"User Feedback: {feedback_text}\n\nOriginal Draft: {approval['draft_body']}"
        )
        response = await asyncio.to_thread(llm_text.invoke, prompt)
        new_draft = response.content.strip()
        await update_draft(telegram_message_id, new_draft)

        resp = _tg(
            "sendMessage",
            chat_id=chat_id,
            text=f"*Updated Draft*\n\n{new_draft}\n\n*Approve this reply?*",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "Approve (Send)",    "callback_data": "APPROVE"}],
                    [{"text": "Edit & Refine",     "callback_data": "EDIT"}],
                    [{"text": "Hold (Save Draft)", "callback_data": "HOLD"}],
                    [{"text": "Reject (Ignore)",   "callback_data": "REJECT"}],
                ]
            },
            parse_mode="Markdown",
        )
        new_tg_id = resp.get("result", {}).get("message_id", 0)
        await store_pending_approval(
            gmail_thread_id=approval["gmail_thread_id"],
            gmail_message_id=approval["gmail_message_id"],
            sender_email=approval["sender_email"],
            subject=approval["subject"],
            draft_body=new_draft,
            telegram_message_id=new_tg_id,
            pending_reply_id=approval.get("pending_reply_id"),
        )
        await mark_approval_resolved(telegram_message_id, "EDITED")
        return "DRAFT_UPDATED"

    if decision == "WRITE_OWN":
        if not feedback_text:
            _tg("sendMessage", chat_id=chat_id,
                text="Type your email body. I'll add a greeting and signature if you haven't included them, then send it:")
            return "WAITING_FOR_OWN_BODY"
        polished, changes = _ensure_greeting_and_signature(feedback_text, approval["sender_email"])
        service = get_gmail_service()
        send_reply(service, approval["gmail_thread_id"], approval["gmail_message_id"],
                   approval["sender_email"], approval["subject"], polished)
        note = f" (auto-added: {', '.join(changes)})" if changes else ""
        _tg("sendMessage", chat_id=chat_id,
            text=f"Your message sent to {approval['sender_email']}{note}.")
        try:
            log_usage_event("sent")
        except Exception:
            pass
        await log_sent_reply(
            gmail_thread_id=approval["gmail_thread_id"],
            gmail_message_id=approval["gmail_message_id"],
            sender_email=approval["sender_email"],
            subject=approval["subject"],
        )
        if approval.get("pending_reply_id"):
            await resolve_pending_reply(approval["pending_reply_id"], "SENT")
        await mark_approval_resolved(telegram_message_id, "SENT_OWN")
        return "SENT_OWN"

    service = get_gmail_service()
    draft_body = approval["draft_body"]

    if decision == "APPROVE":
        send_reply(service, approval["gmail_thread_id"], approval["gmail_message_id"],
                   approval["sender_email"], approval["subject"], draft_body)
        _tg("sendMessage", chat_id=chat_id, text=f"Sent to {approval['sender_email']}.")
        try:
            log_usage_event("sent")
        except Exception:
            pass
        await log_sent_reply(
            gmail_thread_id=approval["gmail_thread_id"],
            gmail_message_id=approval["gmail_message_id"],
            sender_email=approval["sender_email"],
            subject=approval["subject"],
        )
        if approval.get("pending_reply_id"):
            await resolve_pending_reply(approval["pending_reply_id"], "SENT")

    elif decision == "HOLD":
        save_draft(service, approval["gmail_thread_id"], approval["gmail_message_id"],
                   approval["sender_email"], approval["subject"], draft_body)
        _tg("sendMessage", chat_id=chat_id, text="Saved to Gmail Drafts.")

    elif decision == "REJECT":
        _tg("sendMessage", chat_id=chat_id, text="Rejected.")

    await mark_approval_resolved(telegram_message_id, decision)
    return decision
