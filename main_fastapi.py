from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from oauth_manager import exchange_code_for_tokens, get_user_profile
from json_manager import init_json, save_user, get_user
from gsheet_manager import create_contact_sheet, append_row  # Adapted for contacts
from bot import send_oauth_success_message  # Import from bot.py
import datetime
import traceback

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html>
        <head><title>All we met Backend</title></head>
        <body style="font-family: Arial; text-align: center; padding: 50px;">
            <h1>📸 All_we_met OAuth Backend</h1>
            <p>Server running! Ready for <code>/oauth/callback</code>.</p>
            <p><a href="https://t.me/All_we_met_bot">← Open Telegram Bot</a></p>
        </body>
    </html>
    """

@app.on_event("startup")
async def startup_event():
    init_json()
    from config import REDIRECT_URI
    print(f"FastAPI startup: JSON initialized")
    print(f"OAuth Redirect URI configured: {REDIRECT_URI}")
    print("⚠️  Make sure this URL is added to Google Cloud Console OAuth credentials!")

@app.get("/oauth/callback")
async def callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")
    
    print(f"Callback received: code={code is not None}, state={state}, error={error}")
    print(f"Full URL: {request.url}")

    if not state:
        return HTMLResponse("""
        <html><body style="font-family: Arial; text-align: center; padding: 50px;">
            <h2>❌ Missing State Parameter</h2><p>Invalid OAuth callback. Please try /start again.</p>
            <a href="https://t.me/All_we_met_bot" style="background: #0088cc; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">← Back to Telegram Bot</a>
        </body></html>
        """)
    
    try:
        telegram_id = int(state)
    except ValueError:
        return HTMLResponse("""
        <html><body style="font-family: Arial; text-align: center; padding: 50px;">
            <h2>❌ Invalid State Parameter</h2><p>Invalid OAuth callback. Please try /start again.</p>
            <a href="https://t.me/All_we_met_bot" style="background: #0088cc; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">← Back to Telegram Bot</a>
        </body></html>
        """)

    if error:
        print(f"OAuth error: {error}")
        return HTMLResponse(f"""
        <html><body style="font-family: Arial; text-align: center; padding: 50px;">
            <h2>❌ OAuth Error</h2><p>Error: {error}. Try /start again in Telegram.</p>
            <a href="https://t.me/All_we_met_bot" style="background: #0088cc; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">← Back to Telegram Bot</a>
        </body></html>
        """)

    if not code:
        print("No code received")
        return HTMLResponse("""
        <html><body style="font-family: Arial; text-align: center; padding: 50px;">
            <h2>❌ No Auth Code</h2><p>Something went wrong. Restart /start in Telegram.</p>
            <a href="https://t.me/All_we_met_bot" style="background: #0088cc; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">← Back to Telegram Bot</a>
        </body></html>
        """)

    try:
        tokens = exchange_code_for_tokens(code)
    except Exception as e:
        print(f"❌ Exception during token exchange: {e}\n{traceback.format_exc()}")
        return HTMLResponse(f"""
        <html><body style="font-family: Arial; text-align: center; padding: 50px;">
            <h2>❌ Token Exchange Error</h2>
            <p>An error occurred while exchanging the authorization code. Please try again from the Telegram bot.</p>
            <pre style="text-align:left; max-width:800px; margin: 10px auto; background:#f5f5f5; padding:10px; border-radius:4px;">{str(e)}</pre>
            <a href="https://t.me/All_we_met_bot" style="background: #0088cc; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">← Back to Telegram Bot</a>
        </body></html>
        """)

    print(f"Tokens: access={tokens.get('access_token') is not None}, error={tokens.get('error')}")  # Log
    if "access_token" not in tokens:
        print(f"Token fail: {tokens.get('error_description')}")
        return HTMLResponse(f"""
        <html><body style="font-family: Arial; text-align: center; padding: 50px;">
            <h2>❌ Token Exchange Failed</h2><p>{tokens.get('error_description', 'Unknown error')}</p>
            <a href="https://t.me/All_we_met_bot" style="background: #0088cc; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">← Back to Telegram Bot</a>
        </body></html>
        """)

    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    profile = get_user_profile(access, refresh)
    email = profile.get("email", "unknown")
    google_name = profile.get("name") or (email.split("@")[0] if email and email != "unknown" else "User")
    print(f"Email: {email}, Name: {google_name}")

    # Sheet & welcome row (adapted for contacts)
    sheet_id = None
    try:
        # Check if we already have this user in users.json
        existing = get_user(telegram_id)  # from json_manager
        if existing and existing.get("sheet_id"):
            # Reuse the saved sheet ID and update tokens in JSON
            sheet_id = existing["sheet_id"]
            print(f"Reusing existing sheet_id for {telegram_id}: {sheet_id}")
            # Persist latest tokens so future API calls succeed
            # update_user_tokens(telegram_id, access, refresh) updates tokens in users.json
            from json_manager import update_user_tokens
            update_user_tokens(telegram_id, access, refresh)
        else:
            # No saved sheet -> create new one and save user
            sheet_id = create_contact_sheet(access, refresh, telegram_id)
            print(f"Created new sheet for {telegram_id}: {sheet_id}")

        # Add a welcome row only if we have a sheet_id
        if sheet_id:
            now = datetime.datetime.now()
            welcome_row = [str(now), "Welcome Setup", "N/A", "N/A"]
            append_row(sheet_id, access, welcome_row, refresh, telegram_id)
            print(f"Welcome row appended to {sheet_id}")

    except Exception as e:
        print(f"Sheet error: {e}\n{traceback.format_exc()}")

    # Immediate redirect to Telegram (opens chat/app)
    telegram_url = f"https://t.me/All_we_met_bot?start=success"
    print(f"✅ OAuth successful! Redirecting to {telegram_url}")
    return RedirectResponse(url=telegram_url, status_code=302)

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Handle 404 and other HTTP errors with helpful message"""
    if exc.status_code == 404:
        return HTMLResponse(f"""
        <html><body style="font-family: Arial; text-align: center; padding: 50px;">
            <h2>❌ Page Not Found (404)</h2>
            <p>The requested URL was not found on this server.</p>
            <p><strong>Requested:</strong> {request.url.path}</p>
            <p>If you're trying to sign in, make sure:</p>
            <ul style="text-align: left; display: inline-block;">
                <li>The FastAPI server is running</li>
                <li>The REDIRECT_URI in your .env matches this server's URL</li>
                <li>The same URI is added to Google Cloud Console OAuth credentials</li>
            </ul>
            <a href="https://t.me/All_we_met_bot" style="background: #0088cc; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; margin-top: 20px; display: inline-block;">← Back to Telegram Bot</a>
        </body></html>
        """, status_code=404)
    return HTMLResponse(f"<h1>{exc.status_code} Error</h1><p>{exc.detail}</p>", status_code=exc.status_code)