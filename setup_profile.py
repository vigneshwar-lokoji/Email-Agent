"""One-time setup: creates a 'Profile' worksheet in your AI Job Tracker sheet.
Run once: python setup_profile.py
Then fill in row 2 with your actual data.
"""
from google.oauth2.service_account import Credentials
import gspread

SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
client = gspread.authorize(creds)

SHEET_NAME = "AI Job Tracker"

PROFILE_HEADERS = [
    "Name",
    "Email",
    "Phone",
    "LinkedIn URL",
    "Portfolio URL",
    "Resume URL",
    "GitHub URL",
    "Timezone",
    "Availability Rules",
    "Notice Period",
    "Current Location",
    "Preferred Locations",
    "Salary Expectations",
    "Years of Experience",
    "Primary Skills",
    "Current Title",
    "Visa Status",
]


def main():
    spreadsheet = client.open(SHEET_NAME)

    try:
        ws = spreadsheet.worksheet("Profile")
        print("Profile tab already exists. Updating headers...")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="Profile", rows=5, cols=len(PROFILE_HEADERS))
        print("Created 'Profile' tab.")

    ws.update(range_name="A1", values=[PROFILE_HEADERS])
    print(f"Headers written: {PROFILE_HEADERS}")
    print("\nNow open your Google Sheet and fill in row 2 with your data.")
    print(f"Sheet: {SHEET_NAME} -> Profile tab")


if __name__ == "__main__":
    main()
