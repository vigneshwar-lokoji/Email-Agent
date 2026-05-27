import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# The 'Scope' defines exactly what our app is allowed to do.
# gmail.modify allows us to read emails, archive them, and apply labels.
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

def authenticate_gmail():
    creds = None
    # We check if we already have a valid token saved from a previous run.
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    # If we don't have a token, or it's expired, we need to log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # This line reads your downloaded JSON key
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            # This opens your web browser to ask for permission
            creds = flow.run_local_server(port=0)
        
        # Save the official access token so we don't have to log in again
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
            
    print("Authentication Successful! Your factory is connected to your inbox.")
    return creds

if __name__ == '__main__':
    authenticate_gmail()

