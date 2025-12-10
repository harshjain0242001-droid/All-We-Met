import requests
from urllib.parse import quote
from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, REDIRECT_URI, OAUTH_SCOPE

def get_auth_url(telegram_id):
    encoded_scope = quote(OAUTH_SCOPE, safe="")
    encoded_redirect = quote(REDIRECT_URI, safe="")
    return (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={encoded_redirect}"
        f"&response_type=code"
        f"&scope={encoded_scope}"
        f"&access_type=offline"
        f"&prompt=consent"
        f"&state={telegram_id}"
    )

def exchange_code_for_tokens(code):
    if not code:
        raise ValueError("Code required")
    url = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    response = requests.post(url, data=data, timeout=10)
    if response.status_code == 200:
        token_data = response.json()
        if "refresh_token" not in token_data:
            print("⚠️ No refresh_token; re-auth needed for new token.")
        return token_data
    raise Exception(f"Exchange failed: {response.json().get('error_description', 'Unknown')}")

def get_user_profile(access_token, refresh_token=None):  # FIXED: Added optional refresh_token
    """
    Fetch user's profile (name + email) from Google Userinfo endpoint.
    
    Args:
        access_token (str): Current access token
        refresh_token (str, optional): Refresh token for auto-refresh
        
    Returns:
        dict: User profile with keys 'email' and 'name'
    """
    url = "https://www.googleapis.com/oauth2/v2/userinfo"

    def fetch(token):
        headers = {"Authorization": f"Bearer {token}"}
        try:
            return requests.get(url, headers=headers, timeout=10)
        except requests.exceptions.RequestException as e:
            print(f"⚠️  Error fetching user profile: {e}")
            return None

    response = fetch(access_token)
    
    if response and response.status_code == 200:
        data = response.json()
        return {
            "email": data.get("email", "unknown"),
            "name": data.get("name") or data.get("given_name") or data.get("email", "User").split("@")[0]
        }
    
    # Try refreshing token if we have one
    if response and response.status_code == 401 and refresh_token:
        print("⚠️  Access token expired, refreshing for user profile...")
        try:
            from .oauth_manager import refresh_access_token  # Self-import
            refreshed = refresh_access_token(refresh_token)
            new_access = refreshed.get("access_token")
            if new_access:
                response = fetch(new_access)
                if response and response.status_code == 200:
                    data = response.json()
                    return {
                        "email": data.get("email", "unknown"),
                        "name": data.get("name") or data.get("given_name") or data.get("email", "User").split("@")[0]
                    }
        except Exception as e:
            print(f"⚠️  Failed to refresh token for user profile: {e}")
    
    # Fallback
    print("⚠️  Could not fetch user profile, using defaults")
    return {"email": "unknown", "name": "User"}

def refresh_access_token(refresh_token):
    url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    response = requests.post(url, data=data, timeout=10)
    if response.status_code == 200:
        return response.json()
    raise Exception(f"Refresh failed: {response.json().get('error_description')}")

def refresh_and_get_access(refresh_token):
    refreshed = refresh_access_token(refresh_token)
    return refreshed["access_token"]