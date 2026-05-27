"""Standalone runner for Phase A (batch analysis without server).
Use this for testing: python gmail_agent.py
For production, use server.py instead.
"""
from dotenv import load_dotenv

load_dotenv(override=True)

from orchestrator import run_batch_phase_a


if __name__ == "__main__":
    count = run_batch_phase_a()
    if count:
        print(f"\nProcessed {count} emails. Check Telegram for approval requests.")
    else:
        print("No new emails to process.")
