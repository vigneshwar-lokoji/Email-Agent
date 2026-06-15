import gspread
from google.oauth2.service_account import Credentials
from state import AgentState
import os
import requests

# --- GOOGLE SHEETS SETUP ---
scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file("service_account.json", scopes=scopes)
client = gspread.authorize(creds)

SHEET_NAME = "AI Job Tracker"


def read_profile() -> dict:
    try:
        ws = client.open(SHEET_NAME).worksheet("Profile")
        headers = ws.row_values(1)
        values = ws.row_values(2)
        return dict(zip(headers, values))
    except Exception:
        return {}


def get_dashboard_stats() -> dict:
    """Read the tracker sheet and compute job search stats."""
    try:
        sheet = client.open(SHEET_NAME).sheet1
        rows = sheet.get_all_records()
    except Exception as e:
        return {"error": str(e)}

    if not rows:
        return {"error": "No data in tracker sheet."}

    companies = {}        # company → list of roles
    rejections = []       # list of {company, stage, reason_cat, reason_detail}
    stages = {}           # stage → count
    reject_reasons = {}   # reason_category → count

    for row in rows:
        company = row.get("Company Name", "N/A")
        job_title = row.get("Job Title", "N/A")
        stage = row.get("Current Stage", "N/A")
        status = row.get("Final Status", "N/A")
        reason_cat = row.get("Reject Reason Category", "N/A")
        reason_detail = row.get("Reject Reason Detail", "N/A")

        # Track companies and roles
        if company not in companies:
            companies[company] = []
        if job_title and job_title != "N/A" and job_title not in companies[company]:
            companies[company].append(job_title)

        # Track pipeline stages
        if stage and stage != "N/A":
            stages[stage] = stages.get(stage, 0) + 1

        # Track rejections
        if status == "Rejected":
            rejections.append({
                "company": company,
                "stage": stage,
                "reason_cat": reason_cat,
                "reason_detail": reason_detail,
            })
            if reason_cat and reason_cat != "N/A":
                reject_reasons[reason_cat] = reject_reasons.get(reason_cat, 0) + 1

    # Companies with multiple roles
    multi_role = {c: roles for c, roles in companies.items() if len(roles) > 1}

    # Sort stages by count
    sorted_stages = sorted(stages.items(), key=lambda x: x[1], reverse=True)

    # Sort reject reasons by count
    sorted_reasons = sorted(reject_reasons.items(), key=lambda x: x[1], reverse=True)

    # Furthest stage reached
    stage_order = [
        "Applied", "Recruiter Screen", "Phone Screen", "OA",
        "Technical Round", "System Design", "Behavioral",
        "Onsite", "Final Round", "Offer Stage",
    ]
    furthest = "Applied"
    for row in rows:
        s = row.get("Current Stage", "")
        if s in stage_order:
            if stage_order.index(s) > stage_order.index(furthest):
                furthest = s

    return {
        "total_companies": len(companies),
        "total_applications": len(rows),
        "multi_role_companies": multi_role,
        "total_rejections": len(rejections),
        "rejections": rejections,
        "stages": sorted_stages,
        "reject_reasons": sorted_reasons,
        "furthest_stage": furthest,
    }


def get_sheet_stats_for_period(period: str) -> dict:
    """Get job application stats from the tracker sheet for a time window.
    period: 'today', 'yesterday', 'week', 'month'
    """
    from datetime import datetime, timedelta, date

    today = date.today()
    if period == "today":
        start_date = today
        end_date = today
    elif period == "yesterday":
        start_date = today - timedelta(days=1)
        end_date = today - timedelta(days=1)
    elif period == "week":
        start_date = today - timedelta(days=today.weekday())  # Monday
        end_date = today
    elif period == "month":
        start_date = today.replace(day=1)
        end_date = today
    else:
        start_date = today
        end_date = today

    try:
        sheet = client.open(SHEET_NAME).sheet1
        rows = sheet.get_all_records()
    except Exception as e:
        return {"error": str(e)}

    if not rows:
        return {"applied": 0, "rejections": 0, "next_round": 0, "companies": [], "rejection_companies": [], "advancing_companies": []}

    applied = 0
    rejections = 0
    next_round = 0
    companies = []
    rejection_companies = []
    advancing_companies = []

    advancing_stages = {"Recruiter Screen", "Phone Screen", "OA", "Technical Round",
                        "System Design", "Behavioral", "Onsite", "Final Round", "Offer Stage"}

    for row in rows:
        date_str = str(row.get("Date Received", "")).strip()
        if not date_str or date_str == "N/A":
            continue

        # Parse the date (try common formats)
        row_date = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%m-%d-%Y"):
            try:
                row_date = datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                continue

        if not row_date or not (start_date <= row_date <= end_date):
            continue

        applied += 1
        company = row.get("Company Name", "N/A")
        stage = row.get("Current Stage", "N/A")
        status = row.get("Final Status", "N/A")

        if company and company != "N/A":
            companies.append(company)

        if status == "Rejected":
            rejections += 1
            reason = row.get("Reject Reason Category", "N/A")
            rejection_companies.append({"company": company, "stage": stage, "reason": reason})
        elif stage in advancing_stages:
            next_round += 1
            advancing_companies.append({"company": company, "stage": stage})

    return {
        "applied": applied,
        "rejections": rejections,
        "next_round": next_round,
        "companies": companies,
        "rejection_companies": rejection_companies,
        "advancing_companies": advancing_companies,
    }


def sheet_operator_node(state: AgentState):
    print("⚙️ Agent 4: Executing full 20-column Sheet Operation...")
    
    try:
        sheet = client.open(SHEET_NAME).sheet1
        data = state.get("extracted_data", {})
        
        # --- FORCE ALL VALUES TO STRINGS ---
        # Google Sheets crashes with a weird 200 error if it gets raw booleans or dicts
        row_values = [
            str(state.get("thread_id", "N/A")),             # A
            str(state.get("date_received", "N/A")),         # B
            str(data.get("Company Name", "N/A")),           # C
            str(data.get("Sender Type", "N/A")),            # D
            str(data.get("Sender Name", "N/A")),            # E
            str(data.get("Source", "N/A")),                 # F
            str(data.get("Job Title", "N/A")),              # G
            str(data.get("Seniority", "N/A")),              # H
            str(data.get("Location Mode", "N/A")),          # I
            str(data.get("Skills/Stack", "N/A")),           # J
            str(data.get("Current Stage", "N/A")),          # K
            str(data.get("Rounds Completed", "0")),         # L
            str(data.get("Final Status", "N/A")),           # M
            str(data.get("Reject Reason Category", "N/A")), # N
            str(data.get("Reject Reason Detail", "N/A")),   # O
            str(state.get("action_required", "No")),        # P
            str(data.get("Action Type", "N/A")),            # Q
            str(data.get("Priority", "Low")),               # R
            str(state.get("critic_pass", "PENDING")),       # S
            str(data.get("AI Reasoning", "N/A"))            # T
        ]

        decision = state.get("thread_decision", "NEW")

        if decision == "NEW":
            sheet.append_row(row_values)
            print(f"   ✅ SUCCESS: Appended new thread {state.get('thread_id')}")
        else:
            # Use matched_row_id from the resolver (the actual sheet row number).
            # Fall back to thread_id search, then append as new.
            matched_row = state.get("matched_row_id")
            target_row = None

            if matched_row:
                try:
                    target_row = int(matched_row)
                except (ValueError, TypeError):
                    target_row = None

            # Fallback: search by thread_id in column A
            if not target_row:
                cell = sheet.find(str(state.get("thread_id")))
                if cell:
                    target_row = cell.row

            if target_row:
                # Update columns K-T (stage, status, action, etc.) and
                # overwrite thread_id in column A so future lookups work
                sheet.update_cell(target_row, 1, str(state.get("thread_id", "N/A")))
                range_name = f"K{target_row}:T{target_row}"
                sheet.update(range_name=range_name, values=[row_values[10:]])
                print(f"   ✅ SUCCESS: Updated existing thread at row {target_row}")
            else:
                sheet.append_row(row_values)
                print("   ⚠️ Thread ID not found for update, appended as new.")
            
    except Exception as e:
        import traceback
        print(f"   ❌ ERROR in Sheet Operator:")
        traceback.print_exc()  # This will print the exact reason if it fails again
        
    return {}

def telegram_notifier_node(state: AgentState):
    print("⚙️ Agent 7: Sending Telegram notification (non-blocking)...")

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{bot_token}"

    draft = state.get("draft_body", "No draft generated.")
    company = state.get("extracted_data", {}).get("Company Name", "Unknown")
    subject = state.get("subject", "")
    stage = state.get("extracted_data", {}).get("Current Stage", "Unknown")

    keyboard = {
        "inline_keyboard": [
            [{"text": "Approve (Send)", "callback_data": "APPROVE"}],
            [{"text": "Edit & Refine", "callback_data": "EDIT"}],
            [{"text": "Hold (Save Draft)", "callback_data": "HOLD"}],
            [{"text": "Reject (Ignore)", "callback_data": "REJECT"}],
        ]
    }

    msg_text = (
        f"*{company}* | {stage}\n"
        f"_{subject}_\n\n"
        f"{draft}\n\n"
        f"*What would you like to do?*"
    )

    resp = requests.post(
        f"{url}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": msg_text,
            "reply_markup": keyboard,
            "parse_mode": "Markdown",
        },
    )
    result = resp.json()
    tg_message_id = result.get("result", {}).get("message_id", 0)

    print(f"   Telegram message sent (id: {tg_message_id}). Returning immediately.")
    return {"telegram_message_id": tg_message_id}


# ── Usage Tracker (Google Sheet) ────────────────────────────────────────────

USAGE_HEADERS = [
    "Date",
    "Emails Received",
    "Emails Processed",
    "Job",
    "Personal",
    "Business",
    "Bank",
    "Study",
    "Advertisement",
    "Spam",
    "Replies Drafted by Agent",
    "Replies Sent",
    "Reminders Created",
    "Reminders Cleared",
]


def _get_usage_sheet():
    """Get or create the Usage worksheet."""
    spreadsheet = client.open(SHEET_NAME)
    try:
        return spreadsheet.worksheet("Usage")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="Usage", rows=400, cols=len(USAGE_HEADERS))
        ws.update(range_name="A1", values=[USAGE_HEADERS])
        ws.format("A1:N1", {"textFormat": {"bold": True}})
        print("[Usage] Created 'Usage' worksheet.")
        return ws


def _get_today_row(ws) -> int | None:
    """Find today's row in the Usage sheet. Returns row number or None."""
    from datetime import date
    today_str = date.today().isoformat()
    try:
        cell = ws.find(today_str, in_column=1)
        return cell.row if cell else None
    except Exception:
        return None


def log_usage_event(event_type: str, category: str = ""):
    """Increment a counter in today's Usage row.

    event_type: 'received', 'processed', 'draft', 'sent', 'reminder_add', 'reminder_clear'
    category: email category (for 'processed' events)
    """
    from datetime import date

    try:
        ws = _get_usage_sheet()
        today_str = date.today().isoformat()
        row_num = _get_today_row(ws)

        if not row_num:
            # Create today's row with zeros
            new_row = [today_str] + [0] * (len(USAGE_HEADERS) - 1)
            ws.append_row(new_row, value_input_option="RAW")
            row_num = _get_today_row(ws)
            if not row_num:
                print("[Usage] Could not find newly created row.")
                return

        # Column mapping (1-indexed)
        col_map = {
            "received": 2,       # B: Emails Received
            "processed": 3,      # C: Emails Processed
            "draft": 11,         # K: Replies Drafted by Agent
            "sent": 12,          # L: Replies Sent
            "reminder_add": 13,  # M: Reminders Created
            "reminder_clear": 14,# N: Reminders Cleared
        }

        # Category columns
        cat_col_map = {
            "job": 4,            # D
            "personal": 5,       # E
            "business": 6,       # F
            "bank": 7,           # G
            "study": 8,          # H
            "advertisement": 9,  # I
            "spam": 10,          # J
        }

        # Increment the main event counter
        if event_type in col_map:
            col = col_map[event_type]
            current = ws.cell(row_num, col).value
            ws.update_cell(row_num, col, int(current or 0) + 1)

        # Increment the category counter for 'processed' events
        if event_type == "processed" and category in cat_col_map:
            cat_col = cat_col_map[category]
            current = ws.cell(row_num, cat_col).value
            ws.update_cell(row_num, cat_col, int(current or 0) + 1)

    except Exception as e:
        print(f"[Usage] Error logging {event_type}: {e}")
