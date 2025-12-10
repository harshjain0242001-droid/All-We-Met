import requests
import traceback
import logging
from oauth_manager import refresh_and_get_access
from json_manager import update_user_tokens, update_user_field, get_user
BASE_URL = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"

# Setup logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def create_contact_sheet(access_token, refresh_token=None, telegram_id=None):
    """
    Create 'All We Met Contacts' sheet with 6-column headers.
    Retries on 401; updates JSON.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    meta = {"name": "All We Met Contacts", "mimeType": "application/vnd.google-apps.spreadsheet"}
    
    sheet_id = None
    try:
        logger.info("Creating Contacts sheet...")
        response = requests.post(DRIVE_API, headers=headers, json=meta, timeout=15)
        
        if response.status_code == 200:
            data = response.json()
            sheet_id = data["id"]
            logger.info(f"Sheet created: {sheet_id}")
        else:
            error_msg = response.json().get("error", {}).get("message", f"HTTP {response.status_code}")
            logger.error(f"Creation failed: {error_msg}")
        
        if response.status_code == 401 and refresh_token:
            new_access = refresh_and_get_access(refresh_token)
            if telegram_id:
                update_user_tokens(telegram_id, new_access, refresh_token)
            headers["Authorization"] = f"Bearer {new_access}"
            retry_response = requests.post(DRIVE_API, headers=headers, json=meta, timeout=15)
            if retry_response.status_code == 200:
                data = retry_response.json()
                sheet_id = data["id"]
                logger.info(f"Sheet created after refresh: {sheet_id}")
            else:
                return None
        
        if not sheet_id:
            raise Exception("No ID returned")
        
        # Headers
        header = [["Timestamp", "Name", "Company", "Description", "Phone", "Email"]]
        header_url = f"{BASE_URL}/{sheet_id}/values/Sheet1!A1:F1:append?valueInputOption=RAW"
        header_response = requests.post(header_url, headers=headers, json={"values": header}, timeout=10)
        
        if header_response.status_code != 200:
            if header_response.status_code == 401 and refresh_token:
                new_access = refresh_and_get_access(refresh_token)
                if telegram_id:
                    update_user_tokens(telegram_id, new_access, refresh_token)
                headers["Authorization"] = f"Bearer {new_access}"
                header_response = requests.post(header_url, headers=headers, json={"values": header}, timeout=10)
            logger.warning(f"Headers failed ({header_response.status_code})")
        
        # Save to JSON
        if telegram_id:
            user = get_user(telegram_id)
            if user:
                update_user_field(telegram_id, "sheet_id", sheet_id)
                logger.info(f"Sheet ID saved for {telegram_id}")
        
        return sheet_id
        
    except Exception as e:
        logger.error(f"Creation error: {e}\n{traceback.format_exc()}")
        return None

def append_row(sheet_id, access_token, row, refresh_token=None, telegram_id=None):
    """Append row with retry."""
    if not all([sheet_id, access_token, row]):
        raise ValueError("Required params missing")
    
    sheet_id = validate_and_get_sheet_id(sheet_id, access_token, refresh_token, telegram_id)
    if not sheet_id:
        raise Exception("Sheet validation failed")
    
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{BASE_URL}/{sheet_id}/values/Sheet1!A1:F1:append?valueInputOption=RAW"
    try:
        response = requests.post(url, headers=headers, json={"values": [row]}, timeout=10)
        if response.status_code == 200:
            return True
        if response.status_code == 401 and refresh_token:
            new_access = refresh_and_get_access(refresh_token)
            if telegram_id:
                update_user_tokens(telegram_id, new_access, refresh_token)
            headers["Authorization"] = f"Bearer {new_access}"
            response = requests.post(url, headers=headers, json={"values": [row]}, timeout=10)
            return response.status_code == 200
        return False
    except Exception as e:
        logger.error(f"Append error: {e}")
        return False

def validate_and_get_sheet_id(sheet_id, access_token, refresh_token=None, telegram_id=None):
    """Validate or recreate sheet."""
    if not sheet_id:
        return create_contact_sheet(access_token, refresh_token, telegram_id)
    
    url = f"{BASE_URL}/{sheet_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            return sheet_id
    except:
        pass
    
    logger.info("Sheet invalid—recreating...")
    new_id = create_contact_sheet(access_token, refresh_token, telegram_id)
    if new_id and telegram_id:
        update_user_field(telegram_id, "sheet_id", new_id)
    return new_id or sheet_id

def get_sheet_url(sheet_id):
    """Generate sheet URL."""
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"

def get_rows(sheet_id, access_token, num_rows=10, refresh_token=None, telegram_id=None):
    """Fetch last N data rows (skip headers)."""
    sheet_id = validate_and_get_sheet_id(sheet_id, access_token, refresh_token, telegram_id)
    if not sheet_id:
        return []
    url = f"{BASE_URL}/{sheet_id}/values/Sheet1!A:F"
    params = {"majorDimension": "ROWS"}
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            values = data.get('values', [])
            if values and len(values) > 0 and values[0][0] == "Timestamp":  # FIXED: Skip header
                values = values[1:]  # Data rows only
            return values[-num_rows:] if len(values) > num_rows else values
        return []
    except Exception as e:
        logger.error(f"Get rows error: {e}")
        return []

def update_row(sheet_id, access_token, row_index, field, value, refresh_token=None, telegram_id=None):
    """Update field in row for /edit."""
    sheet_id = validate_and_get_sheet_id(sheet_id, access_token, refresh_token, telegram_id)
    if not sheet_id:
        return False
    col_map = {'name': 'B', 'company': 'C', 'description': 'D', 'phone': 'E', 'email': 'F'}
    col = col_map.get(field)
    if not col:
        return False
    url = f"{BASE_URL}/{sheet_id}/values/Sheet1!{col}{row_index+2}:{col}{row_index+2}"  # Single cell
    body = {"values": [[value]]}
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = requests.put(url, json=body, headers=headers, timeout=10)
        if response.status_code == 200:
            return True
        if response.status_code == 401 and refresh_token:
            new_access = refresh_and_get_access(refresh_token)
            if telegram_id:
                update_user_tokens(telegram_id, new_access, refresh_token)
            headers["Authorization"] = f"Bearer {new_access}"
            response = requests.put(url, json=body, headers=headers, timeout=10)
            return response.status_code == 200
        return False
    except Exception as e:
        logger.error(f"Update row error: {e}")
        return False

def delete_row(sheet_id, access_token, row_indices, refresh_token=None, telegram_id=None):
    """
    Delete multiple rows by their 0-based data row indices (sorted descending).
    Handles token refresh.
    """
    if not sheet_id or not row_indices:
        return 0

    sheet_id = validate_and_get_sheet_id(sheet_id, access_token, refresh_token, telegram_id)
    if not sheet_id:
        return 0

    # Sort descending to avoid index shifting
    row_indices = sorted(set(row_indices), reverse=True)
    requests_body = {
        "requests": []
    }
    for idx in row_indices:
        requests_body["requests"].append({
            "deleteDimension": {
                "range": {
                    "sheetId": 0,
                    "dimension": "ROWS",
                    "startIndex": idx + 1,  # +1 because headers
                    "endIndex": idx + 2
                }
            }
        })

    url = f"{BASE_URL}/{sheet_id}:batchUpdate"
    headers = {"Authorization": f"Bearer {access_token}"}
    success = 0

    try:
        response = requests.post(url, json=requests_body, headers=headers, timeout=15)
        if response.status_code == 200:
            return len(row_indices)

        if response.status_code == 401 and refresh_token:
            new_access = refresh_and_get_access(refresh_token)
            if telegram_id:
                update_user_tokens(telegram_id, new_access, refresh_token)
            headers["Authorization"] = f"Bearer {new_access}"
            response = requests.post(url, json=requests_body, headers=headers, timeout=15)
            if response.status_code == 200:
                return len(row_indices)
    except Exception as e:
        logger.error(f"Batch delete error: {e}")

    # Fallback: delete one by one
    for idx in row_indices:
        if delete_row(sheet_id, access_token, idx + 1, refresh_token, telegram_id):  # +1 for header
            success += 1
    return success