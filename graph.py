"""LangGraph pipeline with two child agents under a parent super-triage.

Parent:
  super_triage → classify as job / personal / business / study / spam

Child A (job emails):
  triage → resolver → extractor → sheet_operator → END
  (drafting is on-demand from orchestrator, not in this graph)

Child B (general emails):
  general_triage → END
  (orchestrator reads general_action and routes to draft or reminder)
"""
import sqlite3
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

from state import AgentState
from ai_nodes import (
    super_triage_agent,
    triage_agent,
    thread_resolver_agent,
    extractor_agent,
    general_triage_agent,
)
from action_nodes import sheet_operator_node


# ── Routing functions ──────────────────────────────────────────────────────────

def route_after_super_triage(state: AgentState):
    category = state.get("email_category", "spam")
    if category == "job":
        return "job_triage"
    if category in ("personal", "business", "study", "bank"):
        return "general_triage"
    # spam → stop immediately
    print("   Super-triage: spam. Ignoring.")
    return END


def route_after_job_triage(state: AgentState):
    if state.get("is_job_related"):
        return "resolver"
    print("   Job triage: not job-related. Stopping.")
    return END


# ── Build graph ────────────────────────────────────────────────────────────────

builder = StateGraph(AgentState)

# Parent
builder.add_node("super_triage", super_triage_agent)

# Child A — job pipeline
builder.add_node("job_triage",      triage_agent)
builder.add_node("resolver",        thread_resolver_agent)
builder.add_node("extractor",       extractor_agent)
builder.add_node("sheet_operator",  sheet_operator_node)

# Child B — general pipeline
builder.add_node("general_triage",  general_triage_agent)

# Edges — parent
builder.add_edge(START, "super_triage")
builder.add_conditional_edges(
    "super_triage",
    route_after_super_triage,
    {"job_triage": "job_triage", "general_triage": "general_triage", END: END},
)

# Edges — child A (job)
builder.add_conditional_edges(
    "job_triage",
    route_after_job_triage,
    {"resolver": "resolver", END: END},
)
builder.add_edge("resolver",       "extractor")
builder.add_edge("extractor",      "sheet_operator")
builder.add_edge("sheet_operator", END)

# Edges — child B (general)
builder.add_edge("general_triage", END)

# ── Compile ────────────────────────────────────────────────────────────────────

CHECKPOINT_DB = "inbox_agent_checkpoints.db"


def build_app():
    conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    memory = SqliteSaver(conn)
    return builder.compile(checkpointer=memory)


# Module-level app; the connection is shared but WAL + busy_timeout
# means concurrent writers wait up to 30s instead of failing instantly.
app = build_app()


def get_fresh_app():
    """Return a compiled app with a fresh DB connection.
    Use this when the module-level app's connection may be stale or locked.
    """
    return build_app()


# ── Quick test runner ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uuid

    tests = [
        ("Job scheduling email",
         "Hi Vignesh, We'd like to schedule a 30-min call for the Senior Dev role at TechCorp. Are you free Tuesday or Wednesday? — Sarah"),
        ("ATS confirmation",
         "Your application for App Sys Analyst II has been received. Click this link to complete your application: careers.christushealth.org"),
        ("Personal email",
         "Hey Vignesh! Are you coming to the birthday dinner on Saturday? Let me know! — Raj"),
    ]

    for label, body in tests:
        print(f"\n=== {label} ===")
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        initial_state = {
            "email_content": f"Subject: {label}\n\n{body}",
            "subject": label,
            "retry_count": 0,
        }
        for output in app.stream(initial_state, config=config):
            for key in output:
                print(f"  {key}")
        final = app.get_state(config).values
        print(f"  category={final.get('email_category')}  "
              f"action_type={final.get('extracted_data', {}).get('Action Type', '-')}  "
              f"general_action={final.get('general_action', '-')}")
