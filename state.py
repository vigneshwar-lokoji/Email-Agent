from typing import TypedDict


class AgentState(TypedDict):
    # --- Email metadata ---
    thread_id: str
    date_received: str
    email_content: str
    subject: str
    sender_email: str
    message_id: str

    # --- Super-triage (parent agent) ---
    # Values: "job" | "personal" | "business" | "study" | "spam"
    email_category: str

    # --- Job pipeline fields ---
    is_job_related: bool
    thread_decision: str
    matched_row_id: str
    extracted_data: dict
    action_required: str

    # --- General pipeline fields ---
    # Values: "needs_reply" | "needs_attention" | "spam"
    general_action: str
    general_summary: str   # 1-sentence summary of what the email is about

    # --- Drafting ---
    draft_body: str
    critic_pass: str
    critic_feedback: str
    retry_count: int

    # --- Human decision ---
    human_decision: str
    final_draft: str
    telegram_message_id: int
