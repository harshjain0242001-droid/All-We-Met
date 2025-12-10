import json
import re
import time
from groq import Groq
from config import GROQ_API_KEY
import pytesseract  # NEW: Tesseract fallback
from PIL import Image  # For Tesseract
import easyocr  # FIX: Import for EasyOCR
import logging 
import concurrent.futures  # FIX: Import for ThreadPoolExecutor
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

EXTRACTION_PROMPT = """
Parse noisy OCR from business card. Fix common errors: typos ('Alendeil' → 'Alendei'), merged ('PvtLtd' → 'Pvt. Ltd.'), gapped numbers ('+91 99090 48406' → recognize as full phone), layout (name top, company bottom).

Extract (keep short, accurate):
- name: Person's full name ONLY (e.g., 'Partha Das'—exclude titles like 'Vice President')
- company: Company name (full, clean; infer from URL/domain near email, e.g., 'Simpel.ai' from 'simpel.ai' or 'https://simpel.ai')
- description: Role/title only (brief, e.g., 'Vice President Sales')
- phone: Full number (preserve +91; format +91 XX XXXX XXXX; detect country)
- email: Email

Respond ONLY with valid JSON object—no extra text, explanations, or markdown: {{"name": "value", "company": "value", "description": "value", "phone": "value", "email": "value"}}
Use "N/A" if unclear. Prioritize: Name (bold/large, top), title (under name), company (bottom/URL), phone (+digits), email (@).

Text: {text}
"""

PHONE_REGEX = re.compile(r'(?:\+|\b)\d+(?:[\s.-]\d+)*')
EMAIL_REGEX = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')

def tesseract_fallback(img_path, lang='eng'):
    """Tesseract OCR fallback (faster on printed text)."""
    try:
        text = pytesseract.image_to_string(Image.open(img_path), lang=lang, config='--psm 6')  # PSM 6 for uniform block
        return text.strip()
    except Exception as e:
        logger.warning(f"Tesseract fallback failed: {e}")
        return ""

def run_ocr_with_timeout(temp_path, timeout=15):
    """Run EasyOCR with timeout; fallback to Tesseract if times out."""
    def ocr_easy():
        reader = easyocr.Reader(['en'], gpu=False)
        result = reader.readtext(temp_path)
        return ' '.join([det[1] for det in result])
    
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(ocr_easy)
        try:
            text = future.result(timeout=timeout)
            return text
        except concurrent.futures.TimeoutError:
            logger.warning("EasyOCR timed out—trying Tesseract fallback")
            return tesseract_fallback(temp_path)

# NEW: Helper to strip common Markdown (bold/italic) from LLM outputs
def clean_markdown(text):
    """Remove Markdown bold (**text**) and italic (*text*) wrappers."""
    if not text or text == 'N/A':
        return text
    # Strip bold: **text** → text
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    # Strip italic: *text* → text (non-greedy)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    return text.strip()

# Validation functions (enhanced for accuracy)
def validate_phone(phone, raw_text):
    # Clean parens from phone
    phone = re.sub(r'[()]', '', phone)
    match = PHONE_REGEX.search(phone)
    if match:
        matched_phone = match.group(0)
        digits = re.sub(r'[^\d]', '', matched_phone)
        if len(digits) < 7:
            return 'N/A'
        # Determine country
        if digits.startswith('91'):
            country = '91'
        elif digits.startswith('1'):
            country = '1'
        else:
            # Assume first 1-3 digits as country if no clear match
            country_len = 1 if digits[0] != '1' and digits[0] != '9' else 2 if digits.startswith('91') else 3 if len(digits) > 10 else 1
            country = digits[:country_len]
        rest = digits[len(country):]
        if len(rest) != 10:
            # If not standard length, return cleaned with spaces preserved or simple format
            return f"+{country} {rest}"
        if country == '91':
            # Format as +91 XX XXXX XXXX (2+4+4)
            return f"+91 {rest[0:2]} {rest[2:6]} {rest[6:]}"
        elif country == '1':
            # Format as +1-XXX-XXX-XXXX
            return f"+1-{rest[0:3]}-{rest[3:6]}-{rest[6:]}"
        else:
            return f"+{country} {rest[0:3]} {rest[3:6]} {rest[6:]}"
    # Fallback search in raw_text
    raw_clean = re.sub(r'[()]', '', raw_text)
    candidates = [m.group(0) for m in PHONE_REGEX.finditer(raw_clean)]
    valid = [c for c in candidates if len(re.sub(r'[^\d]', '', c)) >= 7]
    if valid:
        # Prefer ones starting with +
        with_plus = [c for c in valid if c.startswith('+')]
        if with_plus:
            phone_str = max(with_plus, key=lambda x: len(re.sub(r'[^\d]', '', x)))
        else:
            phone_str = max(valid, key=lambda x: len(re.sub(r'[^\d]', '', x)))
        # Recursive call to format the best candidate
        return validate_phone(phone_str, raw_text)
    return 'N/A'

# Similar enhancements for other fields (name, company, desc, email—fuzzy fixes)
def validate_name(name, raw_text):
    if name == 'N/A':
        candidates = re.findall(r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+){1,2}\b', raw_text)  # 2-3 word names
        return candidates[0] if candidates else 'N/A'
    return name.title().strip()

def validate_company(company, raw_text):
    if company == 'N/A':
        # NEW: Fallback to email domain (e.g., 'partha@simpel.ai' → 'Simpel Ai')
        email_match = EMAIL_REGEX.search(raw_text)
        if email_match:
            domain = email_match.group(0).split('@')[1]
            company = domain.replace('.', ' ').title()  # 'simpel.ai' → 'Simpel Ai'
            return company
        # Existing regex (broadened slightly for plain names)
        candidates = re.findall(r'\b[A-Z][a-zA-Z\s&.-]+(?:Pvt\.?\s*Ltd\.?|Inc\.?|LLC)?\b', raw_text)
        return candidates[0] if candidates else 'N/A'
    return re.sub(r'\s+', ' ', company).title()

def validate_description(desc, raw_text):
    if desc == 'N/A':
        # UPDATED: Broader regex for titles (2-4 capitalized words, e.g., 'Vice President Sales')
        pattern = r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+){1,3}\b'
        candidates = re.findall(pattern, raw_text)
        # NEW: Select second candidate (assumes first is name)
        return candidates[1] if len(candidates) >= 2 else 'N/A'
    return desc.title().strip()

def validate_email(email, raw_text):
    if EMAIL_REGEX.match(email):
        return email.lower().strip()
    match = EMAIL_REGEX.search(raw_text)
    return match.group(0).lower() if match else 'N/A'

def extract_with_llm(text, raw_text=None, max_retries=3):
    if not text or not client:
        return {"name": "N/A", "company": "N/A", "description": "N/A", "phone": "N/A", "email": "N/A"}
    
    prompt = EXTRACTION_PROMPT.format(text=text)
    for attempt in range(max_retries):
        try:
            chat = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.05,  # FIXED: Lower for consistency
                max_tokens=150  # Shorter for speed
            )
            response = chat.choices[0].message.content.strip()
            if response.startswith('{') and response.endswith('}'):
                data = json.loads(response)
                # NEW: Clean Markdown from all fields before validation
                for key in data:
                    data[key] = clean_markdown(data.get(key, 'N/A'))
                # Apply validations
                data['name'] = validate_name(data.get('name', 'N/A'), raw_text or text)
                data['company'] = validate_company(data.get('company', 'N/A'), raw_text or text)
                data['description'] = validate_description(data.get('description', 'N/A'), raw_text or text)
                data['phone'] = validate_phone(data.get('phone', 'N/A'), raw_text or text)
                data['email'] = validate_email(data.get('email', 'N/A'), raw_text or text)
                return data
        except json.JSONDecodeError:
            logger.warning(f"LLM retry {attempt+1}: Invalid JSON")
        except Exception as e:
            logger.error(f"LLM error: {e}")
        
        time.sleep(0.5)  # Faster backoff
    
    # Regex fallback
    return {"name": validate_name('N/A', raw_text or text), "company": validate_company('N/A', raw_text or text), "description": validate_description('N/A', raw_text or text), "phone": validate_phone('N/A', raw_text or text), "email": validate_email('N/A', raw_text or text)}