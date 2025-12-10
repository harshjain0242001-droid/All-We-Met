import requests
from config import TELEGRAM_BOT_TOKEN, REDIRECT_URI
from urllib.parse import urljoin

TOKEN = TELEGRAM_BOT_TOKEN
if not REDIRECT_URI or REDIRECT_URI == "http://localhost:8000":
    print("ERROR: Set REDIRECT_URI in .env to ngrok/prod URL")
    exit(1)

WEBHOOK_URL = urljoin(REDIRECT_URI.rstrip('/'), '/webhook')
response = requests.post(
    f"https://api.telegram.org/bot{TOKEN}/setWebhook",
    json={"url": WEBHOOK_URL, "drop_pending_updates": True}
)
print("Set Webhook:", response.json())

result = response.json()
print("Set Webhook Response:", result)

if result.get("ok"):
    print("✅ Webhook set successfully!")
else:
    print(f"❌ Error setting webhook: {result.get('description', 'Unknown error')}")

info = requests.get(f"https://api.telegram.org/bot{TOKEN}/getWebhookInfo")
print("Webhook Info:", info.json())