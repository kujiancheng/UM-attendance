"""
UM Auto-Attend: Interactive Setup & Authentication Script
=========================================================
Configures config.json automatically using your SPeCTRUM session.
Generates long-lived Mobile API tokens and auto-discovers your User ID.
"""

import base64
import json
import os
import re
import sys
import requests

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
SITE_URL = "https://spectrum.um.edu.my"


def load_existing_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def main():
    print("=" * 65)
    print("   Universiti Malaya SPeCTRUM Auto-Attend: Setup Wizard")
    print("=" * 65)
    print()

    existing = load_existing_config()
    print("To allow the attendance script to run on your behalf, we need")
    print("a valid SPeCTRUM session to generate your long-lived API token.\n")
    print("📌 How to get your MoodleSession cookie (Takes 10 seconds):")
    print("   1. Open Google Chrome and log in to: https://spectrum.um.edu.my")
    print("   2. Press F12 (or right-click anywhere -> Inspect) -> click 'Console'")
    print("   3. Paste and press Enter:")
    print("      copy(document.cookie.match(/MoodleSession=([^;]+)/)?.[1])")
    print("   4. The cookie is now copied to your clipboard! Paste it below.\n")

    current_cookie = existing.get("moodle_session_cookie", "")
    prompt = "🔑 Paste your MoodleSession cookie"
    if current_cookie:
        prompt += f" [leave blank to reuse current: {current_cookie[:8]}...]"
    prompt += ": "

    cookie = input(prompt).strip().strip('"').strip("'")
    if not cookie and current_cookie:
        cookie = current_cookie

    if not cookie:
        print("\n❌ Cookie cannot be empty. Please follow the instructions above.")
        sys.exit(1)

    print("\n🔄 Validating cookie and connecting to SPeCTRUM...")
    session = requests.Session()
    session.cookies.set("MoodleSession", cookie, domain="spectrum.um.edu.my")
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })

    try:
        resp = session.get(f"{SITE_URL}/my/", timeout=15, allow_redirects=False)
        if resp.status_code != 200 or "login" in resp.url or "microsoft" in resp.headers.get("Location", "").lower():
            print("❌ SPeCTRUM rejected this cookie! The cookie is expired or invalid.")
            print("   Please log into https://spectrum.um.edu.my in Chrome and copy a fresh cookie.")
            sys.exit(1)
    except Exception as e:
        print(f"❌ Connection error: {e}")
        sys.exit(1)

    print("✅ Cookie verified! Generating SPeCTRUM Mobile API token...")

    # Query Moodle Mobile launch endpoint
    launch_url = f"{SITE_URL}/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=12345&urlscheme=moodlemobile"
    token = ""
    private_token = ""

    try:
        launch_resp = session.get(launch_url, allow_redirects=False, timeout=15)
        loc = launch_resp.headers.get("Location", "")
        m = re.search(r'token=([A-Za-z0-9+/=]+)', loc)
        if m:
            decoded = base64.b64decode(m.group(1)).decode("utf-8")
            parts = decoded.split(":::")
            if len(parts) >= 2:
                token = parts[1]
                print("✅ Successfully generated long-lived Moodle Mobile API token!")
            if len(parts) >= 3:
                private_token = parts[2]
                print("✅ Retrieved private token for automatic cookie renewal!")
        else:
            print("⚠️ Notice: Mobile launch token redirect did not return a token.")
            print("   The script will use the verified cookie directly.")
    except Exception as e:
        print(f"⚠️ Mobile launch note: {e}")

    # Fallback to existing token if new one wasn't returned
    if not token and existing.get("token") and existing["token"] != "YOUR_MOODLE_TOKEN_HERE_OR_LEAVE_EMPTY":
        token = existing["token"]

    user_id = existing.get("user_id", 0)
    fullname = existing.get("fullname", "")
    username = existing.get("username", "")

    # Retrieve user information via Moodle web service API
    if token:
        try:
            info_resp = requests.get(
                f"{SITE_URL}/webservice/rest/server.php",
                params={
                    "wstoken": token,
                    "wsfunction": "core_webservice_get_site_info",
                    "moodlewsrestformat": "json"
                },
                timeout=15
            ).json()

            if isinstance(info_resp, dict) and "userid" in info_resp:
                user_id = info_resp["userid"]
                fullname = info_resp.get("fullname", "")
                username = info_resp.get("username", "")
                print(f"👤 Connected as: {fullname} ({username}) [UserID: {user_id}]")
        except Exception as e:
            print(f"⚠️ Could not fetch site info: {e}")

    # Telegram notification settings
    print("\n" + "-" * 50)
    print("📱 Telegram Notifications (Optional but recommended)")
    print("-" * 50)
    print("Receive instant alerts whenever attendance is marked or if auth expires.")

    current_tg_token = existing.get("telegram_bot_token", "")
    current_tg_chat = existing.get("telegram_chat_id", "")

    prompt_bot = "🤖 Telegram Bot Token"
    if current_tg_token:
        prompt_bot += f" [{current_tg_token[:10]}...]"
    prompt_bot += " (Enter to keep/skip): "
    tg_token = input(prompt_bot).strip()
    if not tg_token and current_tg_token:
        tg_token = current_tg_token

    prompt_chat = "💬 Telegram Chat ID"
    if current_tg_chat:
        prompt_chat += f" [{current_tg_chat}]"
    prompt_chat += " (Enter to keep/skip): "
    tg_chat = input(prompt_chat).strip()
    if not tg_chat and current_tg_chat:
        tg_chat = current_tg_chat

    # Test Telegram connection
    if tg_token and tg_chat:
        try:
            test_resp = requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={
                    "chat_id": tg_chat,
                    "text": "🎉 <b>SPeCTRUM Auto-Attend Connected!</b>\nYour Telegram bot is successfully linked and will receive attendance notifications.",
                    "parse_mode": "HTML"
                },
                timeout=10
            )
            if test_resp.status_code == 200:
                print("✅ Telegram test message sent successfully!")
            else:
                print(f"⚠️ Telegram API returned status {test_resp.status_code}: {test_resp.text}")
        except Exception as e:
            print(f"⚠️ Could not send Telegram test message: {e}")

    # Build config dictionary
    config = {
        "site_url": SITE_URL,
        "token": token,
        "private_token": private_token,
        "moodle_session_cookie": cookie,
        "user_id": user_id,
        "username": username,
        "fullname": fullname,
        "telegram_bot_token": tg_token,
        "telegram_chat_id": tg_chat,
        "check_interval_minutes": existing.get("check_interval_minutes", 60)
    }

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 65)
    print("🎉 Setup Complete! config.json has been generated successfully.")
    print("=" * 65)
    print("\nNext steps:")
    print("  • Run a single test check:  python auto_attend.py --test")
    print("  • Run continuously locally: python auto_attend.py")
    print("  • Or push to GitHub to run via GitHub Actions automatically!")
    print()


if __name__ == "__main__":
    main()
