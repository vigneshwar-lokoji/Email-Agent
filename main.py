"""Single entry point — Telegram listener + Gmail trigger via Pub/Sub pull."""
import os
import asyncio
import threading
from dotenv import load_dotenv

load_dotenv(override=True)

from bot_listener import listen
from orchestrator import run_batch_phase_a, init_db, check_followups, check_response_nudge, send_morning_brief
from gmail_watch import register_watch

FOLLOWUP_CHECK_HOURS = 6
NUDGE_CHECK_HOURS = 3

PROJECT_ID      = os.environ.get("GOOGLE_CLOUD_PROJECT_ID", "")
SUBSCRIPTION_ID = "gmail-push-sub"


def _get_subscriber():
    from google.cloud import pubsub_v1
    from google.oauth2.service_account import Credentials as SACredentials

    creds = SACredentials.from_service_account_file(
        "service_account.json",
        scopes=["https://www.googleapis.com/auth/pubsub"],
    )
    return pubsub_v1.SubscriberClient(credentials=creds)


def _pubsub_thread():
    """Runs entirely in its own thread — no asyncio involvement."""
    subscriber = _get_subscriber()
    path = subscriber.subscription_path(PROJECT_ID, SUBSCRIPTION_ID)

    # Drain old messages
    total = 0
    while True:
        try:
            r = subscriber.pull(
                request={"subscription": path, "max_messages": 100},
                timeout=10,
            )
            if not r.received_messages:
                break
            ack_ids = [m.ack_id for m in r.received_messages]
            subscriber.acknowledge(request={"subscription": path, "ack_ids": ack_ids})
            total += len(ack_ids)
        except Exception:
            break
    if total:
        print(f"[Pub/Sub] Drained {total} old notification(s).", flush=True)

    print("[Pub/Sub] Listening for new emails...", flush=True)

    # Main loop — pull blocks up to 90s, returns when message arrives
    while True:
        try:
            response = subscriber.pull(
                request={"subscription": path, "max_messages": 10},
                timeout=90,
            )

            if not response.received_messages:
                continue

            ack_ids = [m.ack_id for m in response.received_messages]
            subscriber.acknowledge(request={"subscription": path, "ack_ids": ack_ids})

            print("[Pub/Sub] New email notification! Processing inbox...", flush=True)
            try:
                run_batch_phase_a()
                print("[Pub/Sub] Done.", flush=True)
            except Exception as e:
                print(f"[Pub/Sub] Error processing: {e}", flush=True)

        except Exception as e:
            err = str(e)
            if "504" in err or "DEADLINE_EXCEEDED" in err:
                # Normal — no messages within timeout, loop again
                continue
            print(f"[Pub/Sub] Error: {e}. Retrying in 10s...", flush=True)
            import time
            time.sleep(10)


async def main():
    await init_db()

    # Register Gmail watch
    try:
        register_watch()
    except Exception as e:
        print(f"Warning: Could not register Gmail watch: {e}")

    # Process any unread emails from while agent was offline
    print("Checking inbox on startup...")
    try:
        run_batch_phase_a()
    except Exception as e:
        print(f"Startup check error: {e}")

    # Start Pub/Sub listener in a plain thread (NOT asyncio.to_thread)
    t = threading.Thread(target=_pubsub_thread, daemon=True)
    t.start()

    print("Agent started.")

    # Run Telegram listener + background checkers in parallel
    await asyncio.gather(
        listen(),
        _followup_loop(),
        _nudge_loop(),
        _morning_brief_loop(),
    )


async def _followup_loop():
    """Check for stale threads every N hours."""
    while True:
        await asyncio.sleep(FOLLOWUP_CHECK_HOURS * 3600)
        try:
            print("[Follow-up] Checking for stale threads...", flush=True)
            await check_followups(days=5)
        except Exception as e:
            print(f"[Follow-up] Error: {e}", flush=True)


async def _nudge_loop():
    """Nudge about unanswered emails every N hours."""
    while True:
        await asyncio.sleep(NUDGE_CHECK_HOURS * 3600)
        try:
            print("[Nudge] Checking for pending decisions...", flush=True)
            await check_response_nudge(hours=3)
        except Exception as e:
            print(f"[Nudge] Error: {e}", flush=True)


async def _morning_brief_loop():
    """Send morning brief at 8 AM every day."""
    import os
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz_name = os.environ.get("USER_TIMEZONE", "Asia/Kolkata")
    tz = ZoneInfo(tz_name)
    brief_hour = 8  # 8 AM

    while True:
        now = datetime.now(tz)
        # Calculate next 8 AM
        target = now.replace(hour=brief_hour, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        print(f"[Morning Brief] Next brief in {wait_seconds/3600:.1f} hours at {target.strftime('%I:%M %p %Z')}", flush=True)

        await asyncio.sleep(wait_seconds)

        try:
            print("[Morning Brief] Sending...", flush=True)
            await send_morning_brief()
        except Exception as e:
            print(f"[Morning Brief] Error: {e}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
