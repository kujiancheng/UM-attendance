"""
UM Auto-Attend: Telegram QR Relay Bot
=======================================
A Telegram bot that receives QR code photos from friends and
instantly submits attendance on SPeCTRUM using your saved token.

Usage:
  python qr_relay_bot.py

How it works:
  1. Friend in class photographs the QR code on the projector
  2. Friend sends the photo to this Telegram bot
  3. Bot decodes the QR code (extracts sessid + qrpass)
  4. Bot immediately submits attendance using your saved auth
  5. You get a confirmation message ?

Setup:
  1. Run setup_token.py first (to get your SPeCTRUM token)
  2. Run setup_telegram.py (to configure your bot token)
  3. Run this script: python qr_relay_bot.py
"""

import asyncio
import json
import logging
import os
import re
import sys
import io
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import requests
from PIL import Image

# Try to import pyzbar for QR decoding
try:
    from pyzbar.pyzbar import decode as pyzbar_decode
    HAS_PYZBAR = True
except ImportError:
    HAS_PYZBAR = False

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
ALLOWED_USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "allowed_users.json")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        print("ERROR: config.json not found! Run setup_token.py first.")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_allowed_users():
    """Load list of Telegram user IDs allowed to send QR codes."""
    if os.path.exists(ALLOWED_USERS_FILE):
        with open(ALLOWED_USERS_FILE) as f:
            return json.load(f)
    return {"owner": "", "allowed_senders": []}


def save_allowed_users(data):
    with open(ALLOWED_USERS_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# QR Code Decoding
# ---------------------------------------------------------------------------

def decode_qr_from_image(image_bytes: bytes) -> list[str]:
    """Decode QR codes from an image. Returns list of decoded strings."""
    results = []

    if HAS_PYZBAR:
        try:
            img = Image.open(io.BytesIO(image_bytes))
            decoded = pyzbar_decode(img)
            for obj in decoded:
                data = obj.data.decode("utf-8", errors="replace")
                results.append(data)
        except Exception as e:
            logger.error(f"pyzbar decode failed: {e}")

    if not results:
        # Fallback: try with opencv if available
        try:
            import cv2
            import numpy as np
            nparr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            detector = cv2.QRCodeDetector()
            data, _, _ = detector.detectAndDecode(img)
            if data:
                results.append(data)
        except ImportError:
            pass
        except Exception as e:
            logger.error(f"OpenCV decode failed: {e}")

    return results


def parse_attendance_url(qr_data: str) -> dict | None:
    """
    Parse QR code data from either SPeCTRUM (Moodle) or UMtouch.

    SPeCTRUM formats:
      - https://spectrum.um.edu.my/mod/attendance/attendance.php?sessid=123&qrpass=abc

    UMtouch formats:
      - Random alphanumeric string (e.g., "aB3xK9mP2z")
      - Could be a token, UUID, or encoded session ID
    """
    qr_data = qr_data.strip()

    # --- SPeCTRUM (Moodle) QR ---
    if "attendance" in qr_data.lower() and "sessid" in qr_data.lower():
        try:
            if not qr_data.startswith("http"):
                qr_data = "https://" + qr_data

            parsed = urlparse(qr_data)
            params = parse_qs(parsed.query)

            sessid = params.get("sessid", [None])[0]
            qrpass = params.get("qrpass", [None])[0]

            if sessid:
                return {
                    "system": "spectrum",
                    "type": "attendance_url",
                    "sessid": sessid,
                    "qrpass": qrpass,
                    "full_url": qr_data,
                }
        except Exception as e:
            logger.error(f"URL parse failed: {e}")

    # --- SPeCTRUM fallback: look for sessid/qrpass anywhere ---
    sessid_match = re.search(r'sessid[=:]?\s*(\d+)', qr_data, re.IGNORECASE)
    qrpass_match = re.search(r'qrpass[=:]?\s*([a-zA-Z0-9]+)', qr_data, re.IGNORECASE)

    if sessid_match:
        return {
            "system": "spectrum",
            "type": "parsed",
            "sessid": sessid_match.group(1),
            "qrpass": qrpass_match.group(1) if qrpass_match else None,
            "full_url": qr_data,
        }

    # --- SPeCTRUM URL with password ---
    if "spectrum.um.edu.my" in qr_data.lower():
        return {
            "system": "spectrum",
            "type": "spectrum_url",
            "raw": qr_data,
        }

    # --- ELEARN_ format (UMtouch QR that's actually from SPeCTRUM) ---
    # Format: ELEARN_{base64_session}_{checksum}_{base64_qrpass}
    # This is generated by SPeCTRUM's Moodle attendance and read by UMtouch app
    if qr_data.startswith("ELEARN_"):
        parts = qr_data.split("_")
        # ELEARN + 3 more parts = 4 total
        if len(parts) >= 4:
            return {
                "system": "spectrum_elearn",
                "type": "elearn_qr",
                "full_token": qr_data,
                "session_hash": parts[1],
                "checksum": parts[2],
                "qrpass_hash": parts[3],
            }
        else:
            return {
                "system": "spectrum_elearn",
                "type": "elearn_qr",
                "full_token": qr_data,
            }

    # --- UMtouch QR: random string / token ---
    # UMtouch QR codes are typically short random alphanumeric strings
    # that the UMtouch app sends to its own server
    if re.match(r'^[a-zA-Z0-9\-_]{4,}$', qr_data) and "http" not in qr_data.lower():
        return {
            "system": "umtouch",
            "type": "token",
            "token": qr_data,
        }

    # --- Any other URL from UM ---
    if "um.edu.my" in qr_data.lower():
        return {
            "system": "unknown_um",
            "type": "url",
            "raw": qr_data,
        }

    # --- Fallback: treat any short non-URL string as potential UMtouch token ---
    if len(qr_data) < 200 and not qr_data.startswith("http"):
        return {
            "system": "umtouch",
            "type": "token",
            "token": qr_data,
        }

    return None


# ---------------------------------------------------------------------------
# Attendance Submission
# ---------------------------------------------------------------------------

def submit_attendance(config: dict, sessid: str, qrpass: str = None) -> dict:
    """
    Submit attendance for a session using the saved token.
    Uses autologin to get a web session, then POSTs the attendance form.
    """
    site_url = config["site_url"]
    token = config["token"]
    private_token = config.get("private_token", "")

    # Step 1: Get autologin key for web session
    try:
        key_resp = requests.get(
            f"{site_url}/webservice/rest/server.php",
            params={
                "wstoken": token,
                "wsfunction": "tool_mobile_get_autologin_key",
                "privatetoken": private_token,
                "moodlewsrestformat": "json",
            },
            timeout=15,
        )
        key_data = key_resp.json()

        if "key" not in key_data:
            return {"success": False, "error": f"Autologin failed: {key_data}"}

    except Exception as e:
        return {"success": False, "error": f"Autologin request failed: {e}"}

def submit_attendance(config: dict, sessid: str, qrpass: str = None, password: str = None) -> dict:
    """
    Submit attendance for a SPeCTRUM session (supports normal, QR, and password).
    """
    site_url = config["site_url"]
    token = config["token"]
    private_token = config.get("private_token", "")

    # Step 1: Establish web session (use cookie first, autologin as fallback)
    web = requests.Session()
    web.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 16; SM-S906B) "
            "AppleWebKit/537.36 Chrome/128.0.0.0 Mobile Safari/537.36"
        )
    })
    cookie = config.get("moodle_session_cookie", "")
    if cookie:
        web.cookies.set("MoodleSession", cookie, domain="spectrum.um.edu.my")

    # Step 2: Fetch the attendance form page
    form_url = f"{site_url}/mod/attendance/attendance.php?sessid={sessid}"
    if qrpass:
        form_url += f"&qrpass={qrpass}"

    try:
        resp = web.get(form_url, timeout=15)
        html = resp.text
    except Exception as e:
        return {"success": False, "error": f"Failed to fetch form: {e}"}

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    form = soup.find("form", action=lambda x: x and "attendance.php" in x) or soup.find("form")
    if not form:
        if "already" in html.lower() and ("taken" in html.lower() or "recorded" in html.lower()):
            return {"success": True, "message": "Attendance was already recorded!"}
        return {"success": False, "error": "Could not find attendance form on page"}

    # Dynamic hidden inputs
    form_data = {}
    for hidden in form.find_all("input", type="hidden"):
        name = hidden.get("name")
        val = hidden.get("value", "")
        if name:
            form_data[name] = val

    # Find Present status ID
    present_id = None
    for radio in form.find_all("input", {"name": "status"}):
        parent = radio.find_parent("label") or radio.find_parent("div")
        label_text = parent.get_text(strip=True).lower() if parent else ""
        if any(p in label_text for p in ["present", "hadir", "p"]):
            present_id = radio.get("value")
            break

    if not present_id:
        first_radio = form.find("input", {"name": "status"})
        if first_radio:
            present_id = first_radio.get("value")

    if not present_id:
        if not password and ("studentpassword" in html.lower() or "password" in html.lower()):
            return {"success": False, "error": "Session requires a PASSWORD. Use /pw <password>"}
        return {"success": False, "error": "Could not find 'Present' status option in form"}

    form_data["status"] = present_id
    form_data["submitbutton"] = "Save changes"
    if qrpass:
        form_data["qrpass"] = qrpass
    if password:
        form_data["studentpassword"] = password

    submit_url = form.get("action", f"{site_url}/mod/attendance/attendance.php")
    if not submit_url.startswith("http"):
        submit_url = site_url + "/" + submit_url.lstrip("/")

    try:
        resp = web.post(submit_url, data=form_data, timeout=15)
        if resp.status_code == 200:
            if "error" in resp.text.lower() and "qr" in resp.text.lower():
                return {"success": False, "error": "QR code expired! Ask friend for a new one."}
            if "invalid" in resp.text.lower() and "password" in resp.text.lower():
                return {"success": False, "error": "Incorrect password! Try again."}
            return {"success": True, "message": "MARKED PRESENT!"}
        else:
            return {"success": False, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"success": False, "error": f"Submit failed: {e}"}


def submit_elearn_attendance(config: dict, elearn_token: str) -> dict:
    """
    Submit attendance using an ELEARN_ format QR token.

    Format: ELEARN_{base64_session}_{checksum}_{base64_qrpass}

    This is generated by SPeCTRUM (Moodle) and read by the UMtouch app.
    We try multiple approaches to submit this token.
    """
    site_url = config["site_url"]
    token = config["token"]
    private_token = config.get("private_token", "")

    # Get web session via autologin
    try:
        key_resp = requests.get(
            f"{site_url}/webservice/rest/server.php",
            params={
                "wstoken": token,
                "wsfunction": "tool_mobile_get_autologin_key",
                "privatetoken": private_token,
                "moodlewsrestformat": "json",
            },
            timeout=15,
        )
        key_data = key_resp.json()
        if "key" not in key_data:
            return {"success": False, "error": "Could not get web session"}
    except Exception as e:
        return {"success": False, "error": f"Auth failed: {e}"}

    web = requests.Session()
    web.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 16; SM-S906B) "
            "AppleWebKit/537.36 Chrome/128.0.0.0 Mobile Safari/537.36"
        )
    })

    autologin_url = (
        f"{site_url}/admin/tool/mobile/autologin.php"
        f"?userid={key_data['userid']}"
        f"&key={key_data['key']}"
        f"&urltogo=/my/"
    )
    web.get(autologin_url, allow_redirects=True, timeout=15)

    # Approach 1: Try the full ELEARN token as a qrpass on the attendance page
    # Some Moodle attendance plugins accept the full token string
    try:
        # Try submitting to attendance page with the token as qrpass
        resp = web.get(
            f"{site_url}/mod/attendance/attendance.php",
            params={"qrpass": elearn_token},
            timeout=15,
        )
        if "successfully" in resp.text.lower() or "recorded" in resp.text.lower():
            return {"success": True, "message": "Marked via ELEARN token (approach 1)"}
    except Exception as e:
        logger.error(f"Approach 1 failed: {e}")

    # Approach 2: Try known UMtouch/ELEARN endpoints
    umtouch_urls = [
        f"{site_url}/local/umtouch/attend.php",
        f"{site_url}/local/attendance/qr.php",
        f"{site_url}/mod/attendance/processqr.php",
        f"{site_url}/local/elearn/attendance.php",
    ]

    for url in umtouch_urls:
        try:
            # Try POST with the token
            resp = web.post(url, data={"qr": elearn_token, "token": elearn_token}, timeout=10)
            if resp.status_code == 200 and "error" not in resp.text.lower():
                if "success" in resp.text.lower() or "present" in resp.text.lower():
                    return {"success": True, "message": f"Marked via {url}"}
        except Exception:
            continue

        try:
            # Try GET with the token
            resp = web.get(url, params={"qr": elearn_token}, timeout=10)
            if resp.status_code == 200 and "error" not in resp.text.lower():
                if "success" in resp.text.lower() or "present" in resp.text.lower():
                    return {"success": True, "message": f"Marked via {url}"}
        except Exception:
            continue

    # Approach 3: Scan all courses for open attendance sessions
    # and try submitting the ELEARN token as a password
    try:
        user_id = config.get("user_id", 0)
        courses_resp = requests.get(
            f"{site_url}/webservice/rest/server.php",
            params={
                "wstoken": token,
                "wsfunction": "core_enrol_get_users_courses",
                "userid": user_id,
                "moodlewsrestformat": "json",
            },
            timeout=15,
        )
        courses = courses_resp.json()

        for course in courses:
            contents_resp = requests.get(
                f"{site_url}/webservice/rest/server.php",
                params={
                    "wstoken": token,
                    "wsfunction": "core_course_get_contents",
                    "courseid": course["id"],
                    "moodlewsrestformat": "json",
                },
                timeout=15,
            )
            contents = contents_resp.json()

            for section in contents:
                for module in section.get("modules", []):
                    if module.get("modname") == "attendance":
                        mod_id = module["id"]
                        att_url = f"{site_url}/mod/attendance/view.php?id={mod_id}"
                        resp = web.get(att_url, timeout=15)
                        html = resp.text

                        # Find open sessions
                        sess_matches = re.findall(
                            r'attendance\.php\?sessid=(\d+)', html
                        )

                        for sessid in sess_matches:
                            # Try with full ELEARN token as qrpass
                            result = submit_attendance(config, sessid, elearn_token)
                            if result.get("success"):
                                return result

                            # Also try with just the qrpass_hash part
                            parts = elearn_token.split("_")
                            if len(parts) >= 4:
                                result = submit_attendance(config, sessid, parts[3])
                                if result.get("success"):
                                    return result

    except Exception as e:
        logger.error(f"ELEARN course scan failed: {e}")

    return {
        "success": False,
        "error": (
            "Could not auto-submit ELEARN token.\n"
            "The UMtouch QR format needs the exact submission endpoint.\n"
            "Please try scanning it in the UMtouch app and share what URL it opens."
        ),
    }


def submit_umtouch_attendance(config: dict, qr_token: str) -> dict:
    """
    Submit attendance via UMtouch system using the QR token.

    UMtouch is a separate system from SPeCTRUM. The QR code contains
    a random token string. We try known UMtouch API patterns.

    If auto-submit fails, we return the token for manual entry.
    """
    site_url = config["site_url"]
    token = config["token"]
    private_token = config.get("private_token", "")

    # First, try to get a web session via autologin
    try:
        key_resp = requests.get(
            f"{site_url}/webservice/rest/server.php",
            params={
                "wstoken": token,
                "wsfunction": "tool_mobile_get_autologin_key",
                "privatetoken": private_token,
                "moodlewsrestformat": "json",
            },
            timeout=15,
        )
        key_data = key_resp.json()
        if "key" not in key_data:
            return {"success": False, "error": "Could not get web session"}
    except Exception as e:
        return {"success": False, "error": f"Auth failed: {e}"}

    web = requests.Session()
    web.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 16; SM-S906B) "
            "AppleWebKit/537.36 Chrome/128.0.0.0 Mobile Safari/537.36"
        )
    })

    # Login to get session
    autologin_url = (
        f"{site_url}/admin/tool/mobile/autologin.php"
        f"?userid={key_data['userid']}"
        f"&key={key_data['key']}"
        f"&urltogo=/my/"
    )
    web.get(autologin_url, allow_redirects=True, timeout=15)

    # Try several known UMtouch-related endpoints
    umtouch_endpoints = [
        # SPeCTRUM might have an attendance block that accepts tokens
        f"{site_url}/mod/attendance/attendance.php?qrpass={qr_token}",
        # Some Moodle setups use the QR token as a student password
        f"{site_url}/mod/attendance/attendance.php",
    ]

    # Try approach 1: Search for sessions that accept this QR token
    # The token might be a student password for a static-password session
    # Get all courses and check attendance modules
    try:
        user_id = config.get("user_id", 0)
        courses_resp = requests.get(
            f"{site_url}/webservice/rest/server.php",
            params={
                "wstoken": token,
                "wsfunction": "core_enrol_get_users_courses",
                "userid": user_id,
                "moodlewsrestformat": "json",
            },
            timeout=15,
        )
        courses = courses_resp.json()

        for course in courses:
            # Get course contents
            contents_resp = requests.get(
                f"{site_url}/webservice/rest/server.php",
                params={
                    "wstoken": token,
                    "wsfunction": "core_course_get_contents",
                    "courseid": course["id"],
                    "moodlewsrestformat": "json",
                },
                timeout=15,
            )
            contents = contents_resp.json()

            for section in contents:
                for module in section.get("modules", []):
                    if module.get("modname") == "attendance":
                        mod_id = module["id"]
                        # Fetch attendance page and try submitting with this token
                        att_url = f"{site_url}/mod/attendance/view.php?id={mod_id}"
                        resp = web.get(att_url, timeout=15)
                        html = resp.text

                        # Look for open sessions that accept passwords
                        import re
                        submit_links = re.findall(
                            r'attendance\.php\?sessid=(\d+)',
                            html
                        )

                        for sessid in submit_links:
                            # Try submitting with the QR token as the password
                            result = submit_attendance(config, sessid, qr_token)
                            if result.get("success"):
                                return result

    except Exception as e:
        logger.error(f"UMtouch course scan failed: {e}")

    # If nothing worked, return failure with the token
    return {
        "success": False,
        "error": (
            "Could not auto-submit UMtouch token.\n"
            "The token has been decoded  -  try entering it manually in the UMtouch app."
        ),
    }


def submit_password_attendance(config: dict, password: str) -> dict:
    """Scan all courses for an open password-protected attendance session and submit."""
    site_url = config["site_url"]
    token = config["token"]
    user_id = config.get("user_id", 0)

    web = requests.Session()
    web.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 16; SM-S906B) "
            "AppleWebKit/537.36 Chrome/128.0.0.0 Mobile Safari/537.36"
        )
    })
    cookie = config.get("moodle_session_cookie", "")
    if cookie:
        web.cookies.set("MoodleSession", cookie, domain="spectrum.um.edu.my")

    try:
        courses_resp = requests.get(
            f"{site_url}/webservice/rest/server.php",
            params={
                "wstoken": token,
                "wsfunction": "core_enrol_get_users_courses",
                "userid": user_id,
                "moodlewsrestformat": "json",
            },
            timeout=15,
        )
        courses = courses_resp.json()

        for course in courses:
            cname = course.get("shortname", course.get("fullname", ""))
            contents_resp = requests.get(
                f"{site_url}/webservice/rest/server.php",
                params={
                    "wstoken": token,
                    "wsfunction": "core_course_get_contents",
                    "courseid": course["id"],
                    "moodlewsrestformat": "json",
                },
                timeout=15,
            )
            contents = contents_resp.json()

            for section in contents:
                for module in section.get("modules", []):
                    if module.get("modname") == "attendance":
                        mod_id = module["id"]
                        att_url = f"{site_url}/mod/attendance/view.php?id={mod_id}"
                        resp = web.get(att_url, timeout=15)
                        html = resp.text

                        # Look for open sessions
                        sess_matches = re.findall(r'attendance\.php\?sessid=(\d+)', html)
                        for sessid in sess_matches:
                            res = submit_attendance(config, sessid, password=password)
                            if res.get("success"):
                                res["course"] = cname
                                return res

    except Exception as e:
        logger.error(f"Password scan failed: {e}")
        return {"success": False, "error": str(e)}

    return {"success": False, "error": "No open password session found across your courses."}


# ---------------------------------------------------------------------------
# Telegram Bot Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    allowed = load_allowed_users()

    # First user becomes owner
    if not allowed.get("owner"):
        allowed["owner"] = str(user.id)
        save_allowed_users(allowed)

    await update.message.reply_text(
        f"Hi {user.first_name}! I'm the UM Auto-Attend Relay Bot.\n\n"
        f"How to use:\n"
        f"1. ?? Send a PHOTO of a QR code\n"
        f"2. ?? Send /pw <password> for password-protected classes\n"
        f"3. ?? Paste an attendance URL directly\n\n"
        f"Commands:\n"
        f"/start - Show this help\n"
        f"/pw <password> - Submit with class password\n"
        f"/due - View upcoming assignments & deadlines\n"
        f"/status - Check if bot is connected\n"
        f"/allow <user_id> - Allow a friend to send QR/passwords\n"
        f"/myid - Show your Telegram user ID\n\n"
        f"Your ID: {user.id}"
    )


async def cmd_pw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Submit attendance using a class password."""
    user = update.effective_user
    config = load_config()
    allowed = load_allowed_users()

    owner = allowed.get("owner", config.get("telegram_chat_id", ""))
    allowed_users = allowed.get("allowed_senders", [])

    if str(user.id) != owner and str(user.id) not in allowed_users:
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /pw <password>\nExample: /pw lecture123")
        return

    password = context.args[0]
    await update.message.reply_text(f"? Searching courses and submitting with password '{password}'...")

    result = submit_password_attendance(config, password)
    if result.get("success"):
        cname = result.get("course", "Course")
        await update.message.reply_text(f"? MARKED PRESENT for {cname}!\nPassword '{password}' accepted! ??")
    else:
        await update.message.reply_text(f"? Could not mark attendance:\n{result.get('error')}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check if the bot and SPeCTRUM connection is working."""
    config = load_config()

    # Test API connection
    try:
        resp = requests.get(
            f"{config['site_url']}/webservice/rest/server.php",
            params={
                "wstoken": config["token"],
                "wsfunction": "core_webservice_get_site_info",
                "moodlewsrestformat": "json",
            },
            timeout=10,
        )
        info = resp.json()
        if "fullname" in info:
            await update.message.reply_text(
                f"Bot Status: ONLINE\n"
                f"SPeCTRUM: Connected\n"
                f"User: {info['fullname']}\n"
                f"QR Decoder: {'pyzbar' if HAS_PYZBAR else 'OpenCV fallback'}\n\n"
                f"Ready to receive QR codes!"
            )
        else:
            await update.message.reply_text(f"SPeCTRUM connection error: {info}")
    except Exception as e:
        await update.message.reply_text(f"Connection error: {e}")


async def cmd_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show upcoming assignment deadlines and submission status."""
    config = load_config()
    await update.message.reply_text("? Checking your upcoming assignments on SPeCTRUM...")
    try:
        from assignment_reminder import get_upcoming_assignments_summary
        summary = get_upcoming_assignments_summary(config["site_url"], config["token"])
        await update.message.reply_text(summary, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"? Error fetching assignments: {e}")


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's Telegram ID."""
    await update.message.reply_text(f"Your Telegram ID: {update.effective_user.id}")


async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow a friend to send QR codes."""
    allowed = load_allowed_users()
    owner = allowed.get("owner", "")

    if str(update.effective_user.id) != owner:
        await update.message.reply_text("Only the bot owner can add allowed users.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /allow <telegram_user_id>\n"
            "Your friend can get their ID by sending /myid to this bot."
        )
        return

    new_id = context.args[0]
    if new_id not in allowed.get("allowed_senders", []):
        allowed.setdefault("allowed_senders", []).append(new_id)
        save_allowed_users(allowed)

    await update.message.reply_text(f"User {new_id} can now send QR codes to this bot!")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming photos  -  decode QR and submit attendance."""
    user = update.effective_user
    config = load_config()
    allowed = load_allowed_users()

    # Check if user is allowed
    owner = allowed.get("owner", config.get("telegram_chat_id", ""))
    allowed_users = allowed.get("allowed_senders", [])

    if str(user.id) != owner and str(user.id) not in allowed_users:
        await update.message.reply_text(
            f"You're not authorized to use this bot.\n"
            f"Ask the owner to run: /allow {user.id}"
        )
        return

    # Get the photo (highest resolution)
    photo = update.message.photo[-1]
    await update.message.reply_text("Decoding QR code...")

    try:
        file = await photo.get_file()
        image_bytes = await file.download_as_bytearray()
    except Exception as e:
        await update.message.reply_text(f"Failed to download photo: {e}")
        return

    # Decode QR
    qr_results = decode_qr_from_image(bytes(image_bytes))

    if not qr_results:
        await update.message.reply_text(
            "Could not decode any QR code from this image.\n\n"
            "Tips:\n"
            "- Make sure the QR code is clearly visible\n"
            "- Try taking a closer/clearer photo\n"
            "- Avoid blur and glare on the projector screen"
        )
        return

    # Process the first QR code found
    qr_data = qr_results[0]
    logger.info(f"QR decoded: {qr_data[:100]}")

    # Parse the QR data
    parsed = parse_attendance_url(qr_data)

    if not parsed:
        await update.message.reply_text(
            f"QR decoded but couldn't identify the system.\n\n"
            f"QR content: {qr_data[:200]}\n\n"
            f"Send a screenshot to the developer for analysis."
        )
        return

    system = parsed.get("system", "unknown")

    # === SPeCTRUM (Moodle) QR ===
    if system == "spectrum":
        sessid = parsed.get("sessid")
        qrpass = parsed.get("qrpass")

        status_msg = f"?? SPeCTRUM QR Detected!\n"
        status_msg += f"Session ID: {sessid}\n"
        if qrpass:
            status_msg += f"QR Pass: {qrpass}\n"
        status_msg += f"\n? Submitting attendance NOW..."

        await update.message.reply_text(status_msg)

        result = submit_attendance(config, sessid, qrpass)

        if result.get("success"):
            msg = result.get("message", "Done!")
            await update.message.reply_text(f"? MARKED PRESENT!\n\n{msg}")
        else:
            error = result.get("error", "Unknown error")
            await update.message.reply_text(
                f"? Submission failed!\n\n"
                f"Error: {error}\n\n"
                f"If the QR expired, ask your friend to send a new photo quickly!"
            )

    # === SPeCTRUM ELEARN QR (from UMtouch) ===
    elif system == "spectrum_elearn":
        full_token = parsed.get("full_token", qr_data)

        status_msg = (
            f"?? SPeCTRUM ELEARN QR Detected!\n"
            f"(This is a UMtouch-format QR from SPeCTRUM)\n\n"
            f"? Submitting attendance..."
        )
        await update.message.reply_text(status_msg)

        result = submit_elearn_attendance(config, full_token)

        if result.get("success"):
            msg = result.get("message", "Done!")
            await update.message.reply_text(f"? MARKED PRESENT!\n\n{msg}")
        else:
            error = result.get("error", "Unknown error")
            await update.message.reply_text(
                f"? Auto-submit failed: {error}\n\n"
                f"?? Full token:\n`{full_token}`\n\n"
                f"Try scanning this in the UMtouch app manually.",
                parse_mode="Markdown",
            )

    # === UMtouch QR ===
    elif system == "umtouch":
        token = parsed.get("token", qr_data)

        status_msg = (
            f"?? UMtouch QR Detected!\n"
            f"Token: {token}\n\n"
            f"? Attempting to submit via UMtouch..."
        )
        await update.message.reply_text(status_msg)

        result = submit_umtouch_attendance(config, token)

        if result.get("success"):
            msg = result.get("message", "Done!")
            await update.message.reply_text(f"? MARKED PRESENT!\n\n{msg}")
        else:
            error = result.get("error", "Unknown error")
            await update.message.reply_text(
                f"?? UMtouch auto-submit: {error}\n\n"
                f"?? QR Token: `{token}`\n\n"
                f"You can try entering this token manually in the UMtouch app.",
                parse_mode="Markdown",
            )

    # === Unknown UM system ===
    else:
        await update.message.reply_text(
            f"QR decoded from a UM system but not sure which one.\n\n"
            f"Content: {qr_data[:300]}\n\n"
            f"Try entering this manually."
        )

    # Notify owner if a friend submitted
    if str(user.id) != owner and owner:
        try:
            await context.bot.send_message(
                chat_id=owner,
                text=(
                    f"?? QR Relay used!\n"
                    f"By: {user.first_name} ({user.id})\n"
                    f"System: {system}\n"
                    f"QR: {qr_data[:100]}"
                ),
            )
        except Exception:
            pass


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages  -  check if it's a pasted URL."""
    text = update.message.text.strip()

    if "attendance" in text.lower() and "sessid" in text.lower():
        # User pasted an attendance URL directly
        parsed = parse_attendance_url(text)
        if parsed:
            config = load_config()
            sessid = parsed.get("sessid")
            qrpass = parsed.get("qrpass")

            await update.message.reply_text(f"URL detected! Submitting attendance...\nSession: {sessid}")

            result = submit_attendance(config, sessid, qrpass)

            if result.get("success"):
                await update.message.reply_text(f"MARKED PRESENT!\n{result.get('message', '')}")
            else:
                await update.message.reply_text(f"Failed: {result.get('error', 'Unknown')}")
            return

    await update.message.reply_text(
        f"?? Received: '{text}'\n\n"
        f"� If this is a password for class: Send /pw {text}\n"
        f"� If you have a QR code: Send the PHOTO directly\n"
        f"� Or paste the attendance link directly."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_config()

    bot_token = config.get("telegram_bot_token", "")
    if not bot_token:
        print("ERROR: No Telegram bot token in config.json!")
        print("Run setup_telegram.py first.")
        sys.exit(1)

    print()
    print("=" * 55)
    print("  UM Auto-Attend: QR Relay Bot")
    print("=" * 55)
    print()
    print(f"  Bot token: {bot_token[:15]}...")
    print(f"  SPeCTRUM user: {config.get('fullname', 'Unknown')}")
    print(f"  QR Decoder: {'pyzbar' if HAS_PYZBAR else 'OpenCV fallback'}")
    print()
    print("  Bot is running! Send a QR photo to your bot on Telegram.")
    print("  Press Ctrl+C to stop.")
    print()

    # Initialize allowed users with owner
    allowed = load_allowed_users()
    if not allowed.get("owner"):
        chat_id = config.get("telegram_chat_id", "")
        if chat_id:
            allowed["owner"] = str(chat_id)
            save_allowed_users(allowed)

    # Build and run the bot
    app = Application.builder().token(bot_token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("pw", cmd_pw))
    app.add_handler(CommandHandler(["due", "assignments"], cmd_due))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("allow", cmd_allow))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()


