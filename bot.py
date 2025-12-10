import os
import json
import io
import logging
import tempfile
import time
import telegram
from json_manager import get_user, update_user_field
import re
import httpx
import shutil
import concurrent.futures
import re  # NEW: For regex in cleanup
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import easyocr  # Primary OCR
from PIL import Image, ImageEnhance  # For preprocessing
import pytesseract  # Fallback OCR
import warnings  # For suppressing warnings
from llm_manager import extract_with_llm  # FIXED: Import from root (no bin/)
from gsheet_manager import append_row, validate_and_get_sheet_id, get_sheet_url, get_rows, update_row, delete_row
from json_manager import get_user, save_user  # Added save_user if needed, but mainly get_user
from oauth_manager import get_auth_url
from config import TELEGRAM_BOT_TOKEN as BOT_TOKEN

# Suppress warnings for clean logs
warnings.filterwarnings("ignore", message=".*pin_memory.*")

logging.getLogger('easyocr').setLevel(logging.WARNING)
logging.getLogger('torch').setLevel(logging.WARNING)

# Robust logging (console + file)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
# Suppress httpx INFO spam (keep WARNING/ERROR for issues)
logging.getLogger('httpx').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Global bot instance for FastAPI integration
bot = None

def send_oauth_success_message(telegram_id: int, email: str, display_name: str, sheet_id: str = None):
    """Send success message after OAuth (sync for v20)."""
    global bot
    if not bot:
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
    try:
        message = f"✅ Welcome to All We Met, {display_name}! ({email})\nYour Contacts sheet is ready—let's connect the people you've met!"
        if sheet_id:
            message += f"\nView it: {get_sheet_url(sheet_id)}"
        message += "\n\n📸 Send a business card photo to extract name, phone, and email!"
        bot.send_message(chat_id=telegram_id, text=message)
        logger.info(f"Success message sent to {telegram_id}")
    except Exception as e:
        logger.error(f"Failed to send success message: {e}")

def get_main_menu_keyboard(is_authenticated: bool = False):
    """Generate main menu keyboard."""
    keyboard = []
    if not is_authenticated:
        keyboard.append([InlineKeyboardButton("🔐 Sign In with Google", url=get_auth_url(""))])  # URL will be set dynamically
    else:
        keyboard.append([InlineKeyboardButton("📊 View Drive Sheet", callback_data="drive_link")])
        keyboard.append([InlineKeyboardButton("📋 List Contacts", callback_data="list_contacts")])
        keyboard.append([InlineKeyboardButton("✏️ Edit Contact", callback_data="edit_contact")])
        keyboard.append([InlineKeyboardButton("🗑️ Delete Contact", callback_data="delete_contact")])
        keyboard.append([InlineKeyboardButton("🚪 Sign Out", callback_data="sign_out")])
    return InlineKeyboardMarkup(keyboard)

async def send_or_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None, parse_mode=None):
    """Helper: Send reply_text if message update, edit_message_text if callback."""
    try:
        if update.callback_query:
            query = update.callback_query
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Send/edit error: {e}")
        # Fallback: Try reply if edit fails
        if update.message:
            await update.message.reply_text(text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome handler with sign-in button or main menu."""
    try:
        user = get_user(update.effective_user.id)
        if not user:
            # Dynamic auth URL for button
            auth_url = get_auth_url(update.effective_user.id)
            keyboard = [[InlineKeyboardButton("🔐 Sign In with Google", url=auth_url)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await send_or_edit(update, context,
                "Welcome to All We Met Bot! 🤝\n\n"
                "I'm here to help you capture and organize contacts from business cards you collect at events.\n\n"
                "To get started:\n1. Click the button below to sign in with Google.\n2. After signing in, send me a photo of a business card.\n\n"
                "Ready to meet and connect? 👇",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        else:
            reply_markup = get_main_menu_keyboard(True)
            sheet_url = get_sheet_url(user.get('sheet_id', ''))
            await send_or_edit(update, context,
                f"Hi {user['display_name']}! 👋 Welcome back to All We Met.\n"
                f"View your contacts: {sheet_url}\n\n"
                f"📸 Just send a business card photo, and I'll extract the details for you.\n\n"
                "Use the menu below for more options:",
                reply_markup=reply_markup
            )
    except Exception as e:
        logger.error(f"Start handler error: {e}")
        await send_or_edit(update, context, "❌ Oops! Try /start again or contact support.")

async def signin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for /start with signin focus."""
    await start(update, context)

async def signout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sign out: clear auth tokens only, keep sheet_id so user can reuse it."""
    try:
        telegram_id = update.effective_user.id
        user = get_user(telegram_id)
        if not user:
            await send_or_edit(update, context, "👋 You're not signed in yet! Use /start to sign in.")
            return

        # Clear only tokens / auth flag — keep sheet_id
        update_user_field(telegram_id, "access_token", None)
        update_user_field(telegram_id, "refresh_token", None)
        update_user_field(telegram_id, "is_authenticated", False)

        await send_or_edit(update, context,
            "✅ You've been signed out successfully.\n"
            "Your user.json is preserved and your Google Sheet remains available when you sign in again."
        )

        # Provide a sign-in button with proper state (telegram_id) so OAuth callback has state
        auth_url = get_auth_url(telegram_id)
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Sign In with Google", url=auth_url)]])
        await send_or_edit(update, context, "What would you like to do next?", reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Signout handler error: {e}")
        await send_or_edit(update, context, "❌ Sign out error—try again!")

async def drive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Drive sheet link."""
    try:
        user = get_user(update.effective_user.id if not update.callback_query else update.callback_query.from_user.id)
        if not user:
            await send_or_edit(update, context, "👋 Please /start to sign in first!")
            return
        sheet_url = get_sheet_url(user.get('sheet_id', ''))
        await send_or_edit(update, context,
            f"🔗 Your Contacts Sheet:\n{sheet_url}\n\n"
            "💡 Tip: Share this link to collaborate with your team!"
        )
    except Exception as e:
        logger.error(f"Drive link error: {e}")
        await send_or_edit(update, context, "❌ Sheet access error—try /start to refresh.")

def run_ocr_with_timeout(temp_path, timeout=60):  # Extended timeout for robustness
    """EasyOCR primary; Tesseract fallback if too little text."""
    # Primary: EasyOCR with timeout
    def ocr_easy():
        reader = easyocr.Reader(['en'], gpu=False)
        result = reader.readtext(temp_path, detail=1, paragraph=False)  # Higher detail, no paragraphs for cards
        return ' '.join([det[1] for det in result if det[2] > 0.5])  # Filter low-confidence

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(ocr_easy)
        try:
            easy_text = future.result(timeout=timeout)
            return easy_text
        except concurrent.futures.TimeoutError:
            logger.warning("EasyOCR timed out—falling back to Tesseract")
            # Fallback: Tesseract
            try:
                tesseract_text = pytesseract.image_to_string(Image.open(temp_path), config='--psm 6')
                return tesseract_text.strip()
            except Exception as e:
                logger.error(f"Tesseract fallback failed: {e}")
                return ""

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle business card photo: OCR → LLM extract → append to sheet."""
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("👋 Please /start to sign in with Google first!")
        return

    # Send processing message
    processing_msg = await update.message.reply_text("🔍 Extracting contact details... This may take a moment.")

    # Download photo to temp file
    photo_file = update.message.photo[-1]
    file = await context.bot.get_file(photo_file.file_id)
    temp_path = tempfile.mktemp(suffix='.jpg')
    await file.download_to_drive(temp_path)

    try:
        # OCR extraction
        logger.info("Using EasyOCR (primary)")
        ocr_text = run_ocr_with_timeout(temp_path, timeout=60)
        logger.info(f"OCR extracted: {ocr_text}")

        if not ocr_text.strip():
            raise ValueError("No text detected in image—try a clearer photo.")

        # LLM extraction with raw_text for fallbacks
        data = extract_with_llm(ocr_text, raw_text=ocr_text)

        name = data.get('name', 'N/A')
        company = data.get('company', 'N/A')
        description = data.get('description', 'N/A')
        phone = data.get('phone', 'N/A')
        email = data.get('email', 'N/A')

        # Validate/create sheet
        sheet_id = validate_and_get_sheet_id(
            user['sheet_id'], user['access_token'], user.get('refresh_token'), update.effective_user.id
        )
        if not sheet_id:
            raise Exception("Sheet access failed—please /start to re-authenticate.")

        # Prepare and append row
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [timestamp, name, company, description, phone, email]
        success = append_row(sheet_id, user['access_token'], row, user.get('refresh_token'), update.effective_user.id)

        if success:
            # Success message (edit processing_msg)
            success_msg = f"✅ Added to your contacts!\n\n👤 {name}\n🏢 {company}\n📞 {phone}\n📧 {email}"
            if description != 'N/A':
                success_msg += f"\n💼 {description}"
            success_msg += f"\n\nView sheet: {get_sheet_url(sheet_id)}"
            await processing_msg.edit_text(success_msg, parse_mode='HTML', disable_web_page_preview=True)
        else:
            raise Exception("Failed to save contact to sheet.")

    except Exception as e:
        logger.error(f"Photo handler error: {e}")
        error_msg = "❌ Oops! Couldn't extract details. Tips:\n• Ensure the card is clear and well-lit\n• Try a different angle\n• Check if text is readable"
        # Use reply_text instead of edit_text for robustness (avoids edit failures on network issues)
        await update.message.reply_text(error_msg)
        # Safely delete processing message
        try:
            await processing_msg.delete()
        except Exception as delete_e:
            logger.warning(f"Failed to delete processing message: {delete_e}")

    finally:
        # Cleanup temp file
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception as cleanup_e:
                logger.warning(f"Temp file cleanup failed: {cleanup_e}")

async def list_entries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List contacts with only Edit and Delete buttons (no per-row buttons)."""
    user_id = (update.callback_query.from_user.id if update.callback_query else update.message.from_user.id)
    user = get_user(user_id)
    if not user or not user.get('sheet_id'):
        await send_or_edit(update, context, "❌ Not authenticated or no sheet found. Use /start.")
        return

    rows = get_rows(user['sheet_id'], user['access_token'], num_rows=20, refresh_token=user.get('refresh_token'), telegram_id=user_id)
    if not rows:
        await send_or_edit(update, context, "📋 Your contacts sheet is empty.\n\nSend a business card photo to add one!")
        return

    # Reverse to show newest first
    rows = rows[::-1]
    text = f"📋 Your Recent Contacts ({len(rows)} shown, newest first):\n\n"
    for idx, row in enumerate(rows, 1):
        timestamp = row[0] if len(row) > 0 else ""
        name = row[1] if len(row) > 1 else "N/A"
        company = row[2] if len(row) > 2 else ""
        phone = row[4] if len(row) > 4 else ""
        email = row[5] if len(row) > 5 else ""
        date_part = timestamp.split(" ")[0] if " " in timestamp else timestamp
        text += f"{idx}. 👤 {name}"
        if company != "N/A":
            text += f" | 🏢 {company}"
        if phone != "N/A":
            text += f" | 📞 {phone}"
        if email != "N/A":
            text += f" | 📧 {email}"
        text += f" ({date_part})\n"

    keyboard = [
        [InlineKeyboardButton("✏️ Edit Contact", callback_data="start_edit")],
        [InlineKeyboardButton("🗑️ Delete Contact(s)", callback_data="start_delete")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="drive_link")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_or_edit(update, context, text + "\nChoose an action:", reply_markup=reply_markup)



async def handle_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt for edit value (text-based), showing current value."""
    query = update.callback_query
    await query.answer()
    try:
        parts = query.data.split('_')
        field = parts[1]  # e.g., name, company, description, phone, email
        index = int(parts[2])  # 0-based
        user = get_user(query.from_user.id)
        rows = get_rows(user['sheet_id'], user['access_token'], 10, user.get('refresh_token'), query.from_user.id)
        if index >= len(rows):
            await query.edit_message_text("❌ Invalid entry—use /list to refresh.")
            return
        row = rows[index]
        current_value = ''
        field_map = {'name': (1, 'Name'), 'company': (2, 'Company'), 'description': (3, 'Description'), 'phone': (4, 'Phone'), 'email': (5, 'Email')}
        if field in field_map:
            col_idx, display_field = field_map[field]
            current_value = row[col_idx] if len(row) > col_idx else 'N/A'
        else:
            await query.edit_message_text("❌ Invalid field—use /list.")
            return
        await query.edit_message_text(
            f"📝 Edit {display_field} (Entry #{index+1}):\n\n"
            f"Current: {current_value}\n\n"
            f"Enter new value:\n(Reply below. Use /cancel to abort.)"
        )
        # Store context for next message
        context.user_data['editing'] = {'index': index, 'field': field}
    except Exception as e:
        logger.error(f"Handle edit field error: {e}")
        await query.edit_message_text("❌ Error starting edit—use /list.")

async def handle_text_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for editing."""
    editing = context.user_data.get('editing')
    if not editing:
        # Fallback to general text handler
        await handle_text(update, context)
        return
    try:
        user = get_user(update.effective_user.id)
        if not user:
            await update.message.reply_text("Please /start first.")
            return
        index = editing['index']
        field = editing['field']
        value = update.message.text.strip()
        # Map field for update_row (desc -> description, but now using 'description' in callback)
        if field == 'desc':
            field = 'description'
        sheet_id = validate_and_get_sheet_id(user['sheet_id'], user['access_token'], user.get('refresh_token'), update.effective_user.id)
        # Update row with timestamp note if needed (for "name the file when edited" – adding edit log)
        edited_value = f"{value} (edited {datetime.now().strftime('%Y-%m-%d %H:%M')})"
        success = update_row(sheet_id, user['access_token'], index, field, edited_value, user.get('refresh_token'), update.effective_user.id)
        if success:
            await update.message.reply_text(f"✅ {field.capitalize()} updated for entry #{index+1}!")
            # Show updated list below (no edit, just reply to keep prompt visible)
            await list_entries(update, context)
        else:
            await update.message.reply_text("❌ Update failed—try again. Check logs for details.")
        # Clear context
        context.user_data.pop('editing', None)
    except Exception as e:
        logger.error(f"Text edit error: {e}")
        await update.message.reply_text("❌ Edit error—use /list.")
        context.user_data.pop('editing', None)

async def start_edit_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # CRITICAL: Fully clear any previous editing state
    context.user_data.pop('editing', None)
    context.user_data.pop('editing_row', None)
    context.user_data.pop('editing_field', None)
    
    context.user_data['mode'] = 'await_edit_row'
    await query.edit_message_text(
        "✏️ Edit Mode\n\n"
        "Please reply with the number of the contact you want to edit (from the recent list).\n\n"
        "Example: `5`",
        parse_mode='Markdown'
    )

async def start_delete_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Clear any conflicting state
    context.user_data.pop('editing', None)
    context.user_data.pop('editing_row', None)
    context.user_data.pop('editing_field', None)
    
    context.user_data['mode'] = 'await_delete_rows'
    await query.edit_message_text(
        "🗑️ Delete Mode\n\n"
        "Reply with the number(s) of the contact(s) to delete:\n\n"
        "• Single: `4`\n"
        "• Sequence: `3-5`\n"
        "• Multiple: `2,5,7`\n"
        "• Mixed: `1-3,5,7-9`\n\n"
        "Example: `2, 4-6, 8`",
        parse_mode='Markdown'
    )

async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Perform delete."""
    query = update.callback_query
    await query.answer()
    try:
        parts = query.data.split('_')
        index = int(parts[2])
        user = get_user(query.from_user.id)
        if not user:
            await query.edit_message_text("Please /start first.")
            return
        sheet_id = validate_and_get_sheet_id(user['sheet_id'], user['access_token'], user.get('refresh_token'), query.from_user.id)
        success = delete_row(sheet_id, user['access_token'], index, user.get('refresh_token'), query.from_user.id)
        if success:
            await query.edit_message_text(f"✅ Entry {index+1} deleted successfully!\n\nUse /list to see updates.")
        else:
            await query.edit_message_text("❌ Delete failed—try again.")
    except Exception as e:
        logger.error(f"Confirm delete error: {e}")
        await query.edit_message_text("❌ Delete error—use /list.")

async def handle_edit_row_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Please send a valid number from the list.")
        return
    row_num = int(text)
    user_id = update.effective_user.id
    user = get_user(user_id)
    rows = get_rows(user['sheet_id'], user['access_token'], num_rows=50, refresh_token=user.get('refresh_token'), telegram_id=user_id)
    if not rows or row_num < 1 or row_num > len(rows):
        await update.message.reply_text(f"❌ Invalid number. Must be between 1 and {len(rows)}.")
        return

    # Map displayed (newest first) to actual sheet row index
    displayed_index = row_num - 1
    actual_row_index = len(rows) - 1 - displayed_index  # Correct mapping

    context.user_data['editing_row'] = actual_row_index
    context.user_data['editing'] = True
    context.user_data.pop('mode', None)  # Clear mode after successful input

    row = rows[::-1][displayed_index]  # Get the displayed row data
    name = row[1] if len(row) > 1 else "N/A"
    company = row[2] if len(row) > 2 else "N/A"
    desc = row[3] if len(row) > 3 else "N/A"
    phone = row[4] if len(row) > 4 else "N/A"
    email = row[5] if len(row) > 5 else "N/A"

    keyboard = [
        [InlineKeyboardButton(f"👤 Name: {name}", callback_data=f"edit_name_{actual_row_index}")],
        [InlineKeyboardButton(f"🏢 Company: {company}", callback_data=f"edit_company_{actual_row_index}")],
        [InlineKeyboardButton(f"💼 Role: {desc}", callback_data=f"edit_desc_{actual_row_index}")],
        [InlineKeyboardButton(f"📞 Phone: {phone}", callback_data=f"edit_phone_{actual_row_index}")],
        [InlineKeyboardButton(f"📧 Email: {email}", callback_data=f"edit_email_{actual_row_index}")],
        [InlineKeyboardButton("🔙 Back to List", callback_data="list_contacts")]
    ]
    await update.message.reply_text(
        f"✏️ Editing contact #{row_num}:\n"
        f"👤 {name}\n"
        f"🏢 {company} | 💼 {desc}\n"
        f"📞 {phone} | 📧 {email}\n\n"
        "Choose a field to edit:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_delete_rows_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    user = get_user(user_id)
    rows = get_rows(user['sheet_id'], user['access_token'], num_rows=100, refresh_token=user.get('refresh_token'), telegram_id=user_id)
    total = len(rows)

    if not rows:
        await update.message.reply_text("📋 No contacts to delete.")
        context.user_data.pop('mode', None)
        return

    # Parse input: support 4, 3-5, 2,4,6, 1-3,5
    raw_numbers = re.split(r'[,\s]+', text.replace('-', ','))  # Normalize
    indices = set()
    valid = True

    for part in re.split(r',', text):
        part = part.strip()
        if not part:
            continue
        if '-' in part and part.count('-') == 1:
            try:
                start, end = map(int, part.split('-'))
                if 1 <= start <= end <= total:
                    indices.update(range(start, end + 1))
                else:
                    valid = False
            except:
                valid = False
        elif part.isdigit():
            num = int(part)
            if 1 <= num <= total:
                indices.add(num)
            else:
                valid = False
        else:
            valid = False

    if not valid or not indices:
        await update.message.reply_text(
            f"❌ Invalid input.\n\n"
            f"Examples:\n"
            f"• Single: `4`\n"
            f"• Range: `3-6`\n"
            f"• Multiple: `2,5,8`\n"
            f"• Mixed: `1-3,5,7-9`\n\n"
            f"Numbers must be between 1 and {total}."
        )
        return

    # Convert displayed indices (1-based newest first) → actual sheet row indices
    displayed_to_actual = {i + 1: len(rows) - i for i in range(len(rows))}
    actual_indices = sorted([displayed_to_actual[idx] for idx in indices], reverse=True)  # Delete from bottom

    success_count = delete_row(user['sheet_id'], user['access_token'], actual_indices, user.get('refresh_token'), user_id)

    context.user_data.pop('mode', None)
    context.user_data.pop('editing', None)  # Extra safety
    await update.message.reply_text(
        f"✅ Successfully deleted {success_count} contact(s)!\n\n"
        "Use /list to see updated contacts."
    )

# === UPDATE handle_text FUNCTION (CRITICAL FIX: Check order + state cleanup) ===
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle non-command text messages (e.g., 'Hii') or edits/deletes."""
    mode = context.user_data.get('mode')

    # NEW: Prioritize new modes first
    if mode == 'await_edit_row':
        await handle_edit_row_input(update, context)
        return
    elif mode == 'await_delete_rows':
        await handle_delete_rows_input(update, context)
        return

    # OLD: Only check editing if no mode is active
    if context.user_data.get('editing'):
        await handle_text_edit(update, context)
        return

    # Existing non-mode text handling
    try:
        user = get_user(update.effective_user.id)
        if not user:
            await update.message.reply_text(
                f"Hi there! 😊 I see you're chatting—great! To start capturing contacts from business cards, please use /start to sign in with Google first.\n\n"
                "Once signed in, send me a photo, and I'll handle the rest! 🤝"
            )
        else:
            await update.message.reply_text(
                f"Hey {user['display_name']}! 👋 Ready to add a new connection? Send a business card photo, and I'll extract the name, phone, and email.\n\n"
                "Or say /start for your contacts sheet."
            )
    except Exception as e:
        logger.error(f"Text handler error: {e}")
        await update.message.reply_text("❌ Message error—try again!")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries (buttons)."""
    query = update.callback_query
    await query.answer()
    data = query.data
    try:
        if data == "drive_link":
            await drive_link(update, context)
        elif data == "list_contacts":
            await list_entries(update, context)
        elif data == "sign_out":
            await signout(update, context)
        elif data == "start_edit":
            await start_edit_flow(update, context)
        elif data == "start_delete":
            await start_delete_flow(update, context)
        elif data.startswith("edit_") and len(data.split('_')) == 3:  # e.g., "edit_name_0"
            await handle_edit_field(update, context)
        else:
            await query.edit_message_text("❌ Unknown action—use /start.")
    except Exception as e:
        logger.error(f"Callback handler error: {e}")
        await query.edit_message_text("❌ Button error—try /start again.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled: {context.error}", exc_info=context.error)
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("❌ Unexpected error—try again!")
        except:
            pass  # Ignore if can't reply

def main():
    global bot
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set")
    
    # Custom request with longer timeouts for flaky networks
    from telegram.request import HTTPXRequest
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=60.0,
        pool_timeout=30.0
    )
    
    try:
        application = Application.builder().token(BOT_TOKEN).request(request).concurrent_updates(True).build()
        bot = application.bot
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("signin", signin))  # New: /signin alias
        application.add_handler(CommandHandler("signout", signout))
        application.add_handler(CommandHandler("drivelink", drive_link))  # New: /drivelink
        application.add_handler(CommandHandler("list", list_entries))
        application.add_handler(CallbackQueryHandler(handle_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        application.add_error_handler(error_handler)
        
        logger.info("All We Met Bot starting...")
        
        # Resilient polling with retry on network errors
        max_retries = 10  # Max attempts before giving up
        retry_delay = 5  # Initial delay in seconds (exponential backoff)
        connected = False
        attempt = 0
        while not connected and attempt < max_retries:
            try:
                application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
                connected = True  # If we reach here, polling started successfully
            except (telegram.error.NetworkError, httpx.ConnectError) as e:
                attempt += 1
                logger.warning(f"Network connection failed (attempt {attempt}/{max_retries}): {e}. Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)  # Exponential backoff, max 60s
            except KeyboardInterrupt:
                logger.info("Bot stopped by user")
                break
            except Exception as e:
                logger.error(f"Fatal non-network error: {e}")
                raise
        if not connected:
            logger.error("Max retries exceeded. Bot could not connect. Exiting.")
            print("\n🚨 Max connection retries exceeded. Check your internet and restart the bot.")
            return
        
    except KeyboardInterrupt:
        logger.info("Bot stopped")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        raise
    finally:
        if 'application' in locals() and hasattr(application, 'running') and application.running:
            import asyncio
            asyncio.create_task(application.shutdown())

if __name__ == "__main__":
    main()