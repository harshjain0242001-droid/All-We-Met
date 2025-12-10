from google_auth_oauthlib.flow import InstalledAppFlow
from config import OAUTH_SCOPE

SCOPES = OAUTH_SCOPE.split()

def get_refresh_token():
    flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
    creds = flow.run_local_server(port=8080)
    print(f"Refresh Token: {creds.refresh_token}")
    return creds.refresh_token

if __name__ == "__main__":
    print("Run this for initial token gen (not per-user).")
    get_refresh_token()