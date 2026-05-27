import aiosqlite
import json
from datetime import datetime, timezone

DB_PATH = "inbox_agent_app.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        # Emails waiting for you to decide what to do (the queue)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_thread_id TEXT NOT NULL,
                gmail_message_id TEXT NOT NULL,
                sender_email TEXT NOT NULL,
                subject TEXT NOT NULL,
                email_body TEXT NOT NULL,
                company TEXT,
                stage TEXT,
                priority TEXT DEFAULT 'Medium',
                action_type TEXT DEFAULT 'None',
                status TEXT DEFAULT 'PENDING',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            )
        """)
        # Draft approvals waiting for your tap (after "Hand to Agent")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pending_reply_id INTEGER REFERENCES pending_replies(id),
                gmail_thread_id TEXT NOT NULL,
                gmail_message_id TEXT NOT NULL,
                sender_email TEXT NOT NULL,
                subject TEXT NOT NULL,
                draft_body TEXT NOT NULL,
                telegram_message_id INTEGER UNIQUE,
                status TEXT DEFAULT 'PENDING',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gmail_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # First notification asking user what to do — no draft yet
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_thread_id TEXT NOT NULL,
                gmail_message_id TEXT NOT NULL,
                sender_email TEXT NOT NULL,
                subject TEXT NOT NULL,
                email_body TEXT NOT NULL,
                extracted_data TEXT DEFAULT '{}',
                telegram_message_id INTEGER UNIQUE,
                category TEXT DEFAULT 'job',
                recommendation TEXT DEFAULT '',
                status TEXT DEFAULT 'PENDING',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            )
        """)
        # Track sent replies for follow-up detection
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_thread_id TEXT NOT NULL,
                gmail_message_id TEXT,
                sender_email TEXT NOT NULL,
                subject TEXT NOT NULL,
                company TEXT DEFAULT '',
                category TEXT DEFAULT 'job',
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                followup_status TEXT DEFAULT 'WAITING',
                followup_notified_at TEXT,
                response_received_at TEXT
            )
        """)
        await conn.commit()


# --- SENT REPLIES (follow-up tracking) ---

async def log_sent_reply(
    gmail_thread_id: str,
    gmail_message_id: str,
    sender_email: str,
    subject: str,
    company: str = "",
    category: str = "job",
) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            """INSERT INTO sent_replies
               (gmail_thread_id, gmail_message_id, sender_email, subject, company, category)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (gmail_thread_id, gmail_message_id, sender_email, subject, company, category),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_stale_threads(days: int = 5) -> list[dict]:
    """Find threads where we replied but got no response within N days."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT * FROM sent_replies
               WHERE followup_status = 'WAITING'
                 AND followup_notified_at IS NULL
                 AND sent_at <= datetime('now', ? || ' days')
               ORDER BY sent_at""",
            (f"-{days}",),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def mark_followup_notified(sent_reply_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE sent_replies SET followup_notified_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), sent_reply_id),
        )
        await conn.commit()


async def mark_response_received(gmail_thread_id: str) -> None:
    """Mark that we got a response on this thread — no follow-up needed."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """UPDATE sent_replies
               SET followup_status = 'RESPONDED', response_received_at = ?
               WHERE gmail_thread_id = ? AND followup_status = 'WAITING'""",
            (datetime.now(timezone.utc).isoformat(), gmail_thread_id),
        )
        await conn.commit()


async def dismiss_followup(sent_reply_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE sent_replies SET followup_status = 'DISMISSED' WHERE id = ?",
            (sent_reply_id,),
        )
        await conn.commit()


# --- RESPONSE TIME TRACKING ---

async def get_weekly_activity() -> dict:
    """Get this week's email activity stats (Mon-Sun)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        # Emails received this week
        cursor = await conn.execute(
            """SELECT COUNT(*) FROM pending_decisions
               WHERE created_at >= date('now', 'weekday 1', '-7 days')"""
        )
        emails_received = (await cursor.fetchone())[0]

        # Emails handled this week
        cursor = await conn.execute(
            """SELECT COUNT(*) FROM pending_decisions
               WHERE resolved_at IS NOT NULL
                 AND resolved_at >= date('now', 'weekday 1', '-7 days')
                 AND status NOT IN ('PENDING')"""
        )
        emails_handled = (await cursor.fetchone())[0]

        # Replies sent this week
        cursor = await conn.execute(
            """SELECT COUNT(*) FROM sent_replies
               WHERE sent_at >= date('now', 'weekday 1', '-7 days')"""
        )
        replies_sent = (await cursor.fetchone())[0]

        # Reminders cleared this week
        cursor = await conn.execute(
            """SELECT COUNT(*) FROM pending_replies
               WHERE resolved_at IS NOT NULL
                 AND resolved_at >= date('now', 'weekday 1', '-7 days')
                 AND status IN ('ACTION_TAKEN', 'CLEARED', 'IGNORED')"""
        )
        reminders_cleared = (await cursor.fetchone())[0]

        return {
            "emails_received": emails_received,
            "emails_handled": emails_handled,
            "replies_sent": replies_sent,
            "reminders_cleared": reminders_cleared,
        }


async def get_pending_decisions_older_than(hours: int = 3) -> list[dict]:
    """Find pending decisions that have been waiting longer than N hours."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT * FROM pending_decisions
               WHERE status = 'PENDING'
                 AND created_at <= datetime('now', ? || ' hours')
               ORDER BY created_at""",
            (f"-{hours}",),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_response_time_stats() -> dict:
    """Calculate response time stats from resolved decisions."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        # Get all resolved decisions (ones where user took action)
        cursor = await conn.execute(
            """SELECT created_at, resolved_at, category
               FROM pending_decisions
               WHERE status NOT IN ('PENDING', 'IGNORED')
                 AND resolved_at IS NOT NULL
               ORDER BY resolved_at DESC"""
        )
        rows = await cursor.fetchall()

        if not rows:
            return {"count": 0}

        response_times = []
        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                resolved = datetime.fromisoformat(row["resolved_at"].replace("Z", "+00:00"))
                delta_minutes = (resolved - created).total_seconds() / 60
                if delta_minutes >= 0:
                    response_times.append(delta_minutes)
            except Exception:
                continue

        if not response_times:
            return {"count": 0}

        avg = sum(response_times) / len(response_times)
        fastest = min(response_times)
        slowest = max(response_times)

        # Last 7 days
        week_ago = datetime.now(timezone.utc).isoformat()
        cursor2 = await conn.execute(
            """SELECT created_at, resolved_at
               FROM pending_decisions
               WHERE status NOT IN ('PENDING', 'IGNORED')
                 AND resolved_at IS NOT NULL
                 AND resolved_at >= datetime('now', '-7 days')
               ORDER BY resolved_at DESC"""
        )
        recent_rows = await cursor2.fetchall()
        recent_times = []
        for row in recent_rows:
            try:
                created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                resolved = datetime.fromisoformat(row["resolved_at"].replace("Z", "+00:00"))
                delta_minutes = (resolved - created).total_seconds() / 60
                if delta_minutes >= 0:
                    recent_times.append(delta_minutes)
            except Exception:
                continue

        return {
            "count": len(response_times),
            "avg_minutes": avg,
            "fastest_minutes": fastest,
            "slowest_minutes": slowest,
            "this_week_count": len(recent_times),
            "this_week_avg": sum(recent_times) / len(recent_times) if recent_times else 0,
        }


# --- PENDING REPLIES (the queue) ---

async def add_pending_reply(
    gmail_thread_id: str,
    gmail_message_id: str,
    sender_email: str,
    subject: str,
    email_body: str,
    company: str = "",
    stage: str = "",
    priority: str = "Medium",
    action_type: str = "None",
) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            """INSERT INTO pending_replies
               (gmail_thread_id, gmail_message_id, sender_email, subject, email_body, company, stage, priority, action_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (gmail_thread_id, gmail_message_id, sender_email, subject, email_body, company, stage, priority, action_type),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_pending_replies() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM pending_replies WHERE status = 'PENDING' ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_pending_reply_by_id(reply_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM pending_replies WHERE id = ?", (reply_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def resolve_pending_reply(reply_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE pending_replies SET status = ?, resolved_at = ? WHERE id = ?",
            (status, datetime.now(timezone.utc).isoformat(), reply_id),
        )
        await conn.commit()


async def clear_all_reminders() -> int:
    """Dismiss all pending reminders at once. Returns count cleared."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            "UPDATE pending_replies SET status = 'CLEARED', resolved_at = ? WHERE status = 'PENDING'",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await conn.commit()
        return cursor.rowcount


async def store_pending_approval(
    gmail_thread_id: str,
    gmail_message_id: str,
    sender_email: str,
    subject: str,
    draft_body: str,
    telegram_message_id: int,
    pending_reply_id: int | None = None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO pending_approvals
               (pending_reply_id, gmail_thread_id, gmail_message_id, sender_email, subject, draft_body, telegram_message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (pending_reply_id, gmail_thread_id, gmail_message_id, sender_email, subject, draft_body, telegram_message_id),
        )
        await conn.commit()


async def get_pending_approval(telegram_message_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM pending_approvals WHERE telegram_message_id = ? AND status = 'PENDING'",
            (telegram_message_id,),
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None


async def update_draft(telegram_message_id: int, new_draft: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE pending_approvals SET draft_body = ? WHERE telegram_message_id = ?",
            (new_draft, telegram_message_id),
        )
        await conn.commit()


async def mark_approval_resolved(telegram_message_id: int, decision: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE pending_approvals SET status = ?, resolved_at = ? WHERE telegram_message_id = ?",
            (decision, datetime.now(timezone.utc).isoformat(), telegram_message_id),
        )
        await conn.commit()


async def get_all_pending() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM pending_approvals WHERE status = 'PENDING' ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def store_pending_decision(
    gmail_thread_id: str,
    gmail_message_id: str,
    sender_email: str,
    subject: str,
    email_body: str,
    extracted_data: str,
    telegram_message_id: int,
    category: str = "job",
    recommendation: str = "",
) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO pending_decisions
               (gmail_thread_id, gmail_message_id, sender_email, subject, email_body,
                extracted_data, telegram_message_id, category, recommendation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (gmail_thread_id, gmail_message_id, sender_email, subject, email_body,
             extracted_data, telegram_message_id, category, recommendation),
        )
        await conn.commit()


async def get_pending_decision(telegram_message_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM pending_decisions WHERE telegram_message_id = ? AND status = 'PENDING'",
            (telegram_message_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def resolve_pending_decision(telegram_message_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE pending_decisions SET status = ?, resolved_at = ? WHERE telegram_message_id = ?",
            (status, datetime.now(timezone.utc).isoformat(), telegram_message_id),
        )
        await conn.commit()


async def get_gmail_state(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            "SELECT value FROM gmail_state WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_gmail_state(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO gmail_state (key, value) VALUES (?, ?)",
            (key, value),
        )
        await conn.commit()
