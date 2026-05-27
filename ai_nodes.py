import os
import json
from dotenv import load_dotenv
from state import AgentState
from prompts import (
    SUPER_TRIAGE_PROMPT,
    GENERAL_TRIAGE_PROMPT,
    GENERAL_DRAFTER_PROMPT,
    TRIAGE_PROMPT,
    THREAD_RESOLVER_PROMPT,
    EXTRACTOR_PROMPT,
    DRAFTER_PROMPT,
    CRITIC_PROMPT,
)

from langchain_google_genai import ChatGoogleGenerativeAI

load_dotenv(override=True)

# --- LLM BACKEND SWITCH ---
# USE_VERTEX_AI = True  → routes through Vertex AI (needs API enabled + IAM role)
# USE_VERTEX_AI = False → uses Gemini API key directly (works immediately)
#
# To enable Vertex AI:
#   1. console.cloud.google.com → APIs & Services → Enable "Vertex AI API"
#   2. Grant service account role: Vertex AI User
#   3. Set USE_VERTEX_AI = True below
USE_VERTEX_AI = True

if USE_VERTEX_AI:
    from google.oauth2.service_account import Credentials as SACredentials
    _sa_creds = SACredentials.from_service_account_file(
        "service_account.json",
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    _common = dict(
        vertexai=True,
        project=os.environ.get("GOOGLE_CLOUD_PROJECT_ID", ""),
        location=os.environ.get("VERTEX_AI_LOCATION", "us-central1"),
        credentials=_sa_creds,
        temperature=0.1,
    )
    print("🤖 Gemini 2.5 Flash via Vertex AI")
else:
    _common = dict(
        google_api_key=os.environ.get("GEMINI_API_KEY"),
        temperature=0.1,
    )
    print("🤖 Gemini 2.5 Flash via Gemini API")

llm_json = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    response_mime_type="application/json",
    **_common,
)

llm_text = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    **_common,
)

# --- THE AGENT NODES ---

def triage_agent(state: AgentState):
    print("🤖 Agent 1: Triaging email...")
    messages = [
        {"role": "system", "content": TRIAGE_PROMPT},
        {"role": "user", "content": state.get("email_content", "")}
    ]
    response = llm_json.invoke(messages)
    # Parse JSON (Vertex AI sometimes returns raw dicts or strings, this handles both)
    if isinstance(response.content, str):
        result = json.loads(response.content.replace("```json", "").replace("```", ""))
    else:
        result = response.content
    
    return {"is_job_related": result.get("job_related", False)}

def _get_tracker_rows() -> str:
    from action_nodes import client, SHEET_NAME
    try:
        sheet = client.open(SHEET_NAME).sheet1
        records = sheet.get_all_records()
        summary = [
            {
                "row_id": i + 2,
                "company": r.get("Company Name", ""),
                "job_title": r.get("Job Title", ""),
                "sender_email": r.get("Sender Email", ""),
                "gmail_thread_id": r.get("thread_id", ""),
            }
            for i, r in enumerate(records)
        ]
        return json.dumps(summary)
    except Exception:
        return "[]"

def thread_resolver_agent(state: AgentState):
    print("🤖 Agent 2: Resolving thread...")
    existing_rows = _get_tracker_rows()

    messages = [
        {"role": "system", "content": THREAD_RESOLVER_PROMPT},
        {"role": "user", "content": f"New Email: {state.get('email_content', '')}\nTracker Rows: {existing_rows}"}
    ]
    response = llm_json.invoke(messages)
    if isinstance(response.content, str):
        result = json.loads(response.content.replace("```json", "").replace("```", ""))
    else:
        result = response.content
    
    return {
        "thread_decision": result.get("decision", "NEW"),
        "matched_row_id": result.get("matched_row_id", "")
    }

def extractor_agent(state: AgentState):
    print("🤖 Agent 3: Extracting data to Enums...")
    messages = [
        {"role": "system", "content": EXTRACTOR_PROMPT},
        {"role": "user", "content": state.get("email_content", "")}
    ]
    response = llm_json.invoke(messages)
    if isinstance(response.content, str):
        extracted = json.loads(response.content.replace("```json", "").replace("```", ""))
    else:
        extracted = response.content
    
    return {
        "extracted_data": extracted,
        "action_required": extracted.get("Action Required", "No")
    }

_SCHEDULING_KEYWORDS = [
    "availability", "available", "when are you free", "schedule a call",
    "schedule a meeting", "time slot", "book a time", "set up a call",
    "arrange a meeting", "find a time", "connect for a",
]


def drafter_agent(state: AgentState):
    print("🤖 Agent 5: Drafting reply...")
    from datetime import date
    from action_nodes import read_profile

    current_retries = state.get("retry_count", 0)
    feedback = f"\nCritic Feedback to fix: {state.get('critic_feedback')}" if current_retries > 0 else ""

    profile = read_profile()
    profile_context = f"\nUser Profile: {json.dumps(profile)}" if profile else "\nUser Profile: Not available."
    today = date.today().isoformat()

    email_content = state.get("email_content", "")
    calendar_context = ""
    if any(kw in email_content.lower() for kw in _SCHEDULING_KEYWORDS):
        try:
            from calendar_service import get_calendar_service, get_free_slots, slots_to_text
            slots = get_free_slots(get_calendar_service())
            if slots:
                calendar_context = f"\n{slots_to_text(slots)}"
                print("   Calendar slots injected into draft context.")
        except Exception as e:
            print(f"   (Calendar unavailable: {e})")

    messages = [
        {"role": "system", "content": DRAFTER_PROMPT.replace("{TODAY}", today)},
        {"role": "user", "content": f"Original Email: {email_content}{profile_context}{calendar_context}{feedback}"}
    ]
    response = llm_json.invoke(messages)
    if isinstance(response.content, str):
        draft_result = json.loads(response.content.replace("```json", "").replace("```", ""))
    else:
        draft_result = response.content
    
    return {
        "draft_body": draft_result.get("draft_body", ""),
        "retry_count": current_retries + 1
    }

def critic_agent(state: AgentState):
    print("🤖 Agent 6: Critiquing draft...")
    messages = [
        {"role": "system", "content": CRITIC_PROMPT},
        {"role": "user", "content": f"Email: {state.get('email_content', '')}\nDraft: {state.get('draft_body', '')}"}
    ]
    response = llm_text.invoke(messages)
    output = response.content.strip()

    if output.startswith("PASS"):
        return {"critic_pass": "PASS", "critic_feedback": ""}
    else:
        reason = output.split("||")[1].strip() if "||" in output else "General tone or rule failure."
        return {"critic_pass": "FAIL", "critic_feedback": reason}


# ── Parent agent (super-triage) ───────────────────────────────────────────────

def super_triage_agent(state: AgentState):
    """Classify email as job / personal / business / study / spam."""
    print("🤖 Super-triage: classifying email category...")
    messages = [
        {"role": "system", "content": SUPER_TRIAGE_PROMPT},
        {"role": "user", "content": state.get("email_content", "")},
    ]
    response = llm_json.invoke(messages)
    if isinstance(response.content, str):
        result = json.loads(response.content.replace("```json", "").replace("```", ""))
    else:
        result = response.content

    category = result.get("category", "spam")
    print(f"   Category: {category} ({result.get('confidence')})")
    return {"email_category": category}


def general_triage_agent(state: AgentState):
    """For non-job emails: decide needs_reply / needs_attention / spam."""
    print("🤖 General triage: classifying non-job email...")
    messages = [
        {"role": "system", "content": GENERAL_TRIAGE_PROMPT},
        {"role": "user", "content": state.get("email_content", "")},
    ]
    response = llm_json.invoke(messages)
    if isinstance(response.content, str):
        result = json.loads(response.content.replace("```json", "").replace("```", ""))
    else:
        result = response.content

    action = result.get("action", "spam")
    summary = result.get("summary", "")
    print(f"   General action: {action}")
    return {"general_action": action, "general_summary": summary}


def general_drafter_agent(state: AgentState):
    """Draft a reply to a non-job email."""
    print("🤖 General drafter: writing reply...")
    from action_nodes import read_profile

    profile = read_profile()
    user_name = profile.get("Name", "").split()[0] if profile.get("Name") else ""
    profile_note = f"\nUser's name: {user_name}" if user_name else ""

    messages = [
        {"role": "system", "content": GENERAL_DRAFTER_PROMPT},
        {"role": "user", "content": f"{state.get('email_content', '')}{profile_note}"},
    ]
    response = llm_json.invoke(messages)
    if isinstance(response.content, str):
        result = json.loads(response.content.replace("```json", "").replace("```", ""))
    else:
        result = response.content

    return {
        "draft_body": result.get("draft_body", ""),
        "retry_count": state.get("retry_count", 0) + 1,
    }
