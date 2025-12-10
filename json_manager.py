# json_manager.py
import json
import os
from datetime import datetime
import threading  # <-- NEW

USERS_FILE = "users.json"

# Global thread lock to protect users.json from concurrent writes
file_lock = threading.Lock()  # <-- ADD THIS

def init_json():
    if not os.path.exists(USERS_FILE):
        with file_lock:  # Even init is safe
            with open(USERS_FILE, "w") as f:
                json.dump({}, f, indent=2)
        print(f"Initialized {USERS_FILE}")


def get_user(telegram_id):
    try:
        with file_lock:
            if not os.path.exists(USERS_FILE):
                return None
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return None
                users = json.loads(content)
        telegram_id_str = str(telegram_id)
        return users.get(telegram_id_str)
    except Exception as e:
        print(f"Failed to get user: {e}")
        return None


def save_user(telegram_id, email, access_token, refresh_token, sheet_id, display_name=None):
    try:
        with file_lock:
            users = {}
            if os.path.exists(USERS_FILE):
                with open(USERS_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        users = json.loads(content)

            telegram_id_str = str(telegram_id)
            existing_user = users.get(telegram_id_str, {})

            users[telegram_id_str] = {
                "telegram_id": telegram_id,
                "email": email,
                "sheet_id": sheet_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "display_name": display_name or existing_user.get("display_name") or f"User_{telegram_id}",
                "created_at": existing_user.get("created_at") or datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            }

            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump(users, f, indent=2, ensure_ascii=False)

        print(f"User {telegram_id} saved")
        return True
    except Exception as e:
        print(f"Failed to save user: {e}")
        return False


def update_user_tokens(telegram_id, access_token, refresh_token=None):
    try:
        with file_lock:
            users = {}
            if os.path.exists(USERS_FILE):
                with open(USERS_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        users = json.loads(content)

            telegram_id_str = str(telegram_id)
            if telegram_id_str not in users:
                return False

            users[telegram_id_str]["access_token"] = access_token
            if refresh_token:
                users[telegram_id_str]["refresh_token"] = refresh_token
            users[telegram_id_str]["updated_at"] = datetime.now().isoformat()

            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump(users, f, indent=2, ensure_ascii=False)

        print(f"Tokens updated for {telegram_id}")
        return True
    except Exception as e:
        print(f"Update tokens failed: {e}")
        return False


def update_user_field(telegram_id, field, value):
    try:
        with file_lock:
            users = {}
            if os.path.exists(USERS_FILE):
                with open(USERS_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        users = json.loads(content)

            telegram_id_str = str(telegram_id)
            if telegram_id_str not in users:
                return False

            users[telegram_id_str][field] = value
            users[telegram_id_str]["updated_at"] = datetime.now().isoformat()

            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump(users, f, indent=2, ensure_ascii=False)

        print(f"Updated {field} for {telegram_id}")
        return True
    except Exception as e:
        print(f"Update field failed: {e}")
        return False


# Initialize on import
init_json()