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
            # Removed value_input_option to prevent API conflict
            sheet.append_row(row_values)
            print(f"   ✅ SUCCESS: Appended new thread {state.get('thread_id')}")
        else:
            try:
                cell = sheet.find(str(state.get("thread_id")))
                range_name = f"K{cell.row}:T{cell.row}"
                # The update method requires a list of lists
                sheet.update(range_name=range_name, values=[row_values[10:]])
                print(f"   ✅ SUCCESS: Updated existing thread at row {cell.row}")
            except gspread.exceptions.CellNotFound:
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
