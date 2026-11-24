"""
UM Auto-Attend: Automatic Attendance Checker & Submitter
=========================================================
Monitors SPeCTRUM (Moodle) for open attendance sessions
and automatically marks you as Present.

Usage:
  python auto_attend.py --test     # Test mode: check once and show results
  python auto_attend.py            # Run continuously on schedule
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
REMINDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders_sent.json")



def load_config() -> dict:
    """Load configuration from config.json or environment variables."""
    config = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"  ⚠️ Warning loading config.json: {e}")
            config = {}

    # Defaults
    config.setdefault("site_url", "https://spectrum.um.edu.my")
    config.setdefault("token", "")
    config.setdefault("private_token", "")
    config.setdefault("moodle_session_cookie", "")
    config.setdefault("user_id", 0)
    config.setdefault("check_interval_minutes", 60)

    # Environment variables override config.json if present (ideal for GitHub Secrets)
    if os.environ.get("MOODLE_TOKEN"):
        config["token"] = os.environ.get("MOODLE_TOKEN")
    if os.environ.get("MOODLE_PRIVATE_TOKEN"):
        config["private_token"] = os.environ.get("MOODLE_PRIVATE_TOKEN")
    if os.environ.get("MOODLE_COOKIE"):
        config["moodle_session_cookie"] = os.environ.get("MOODLE_COOKIE")
    if os.environ.get("USER_ID"):
        try:
            config["user_id"] = int(os.environ.get("USER_ID"))
        except ValueError:
            pass
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        config["telegram_bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN")
    if os.environ.get("TELEGRAM_CHAT_ID"):
        config["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID")
    if os.environ.get("CHECK_INTERVAL"):
        try:
            config["check_interval_minutes"] = int(os.environ.get("CHECK_INTERVAL"))
        except ValueError:
            pass

    return config


def save_config(config: dict):
    """Safely save current configuration to config.json."""
    if os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log(f"⚠️ Could not save updated config.json: {e}")


# ---------------------------------------------------------------------------
# Moodle API Client
# ---------------------------------------------------------------------------

class MoodleError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"Moodle Error [{code}]: {message}")


class MoodleAPI:
    """Simple Moodle REST API wrapper."""

    def __init__(self, site_url: str, token: str):
        self.site_url = site_url.rstrip("/")
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "MoodleMobile 4.4.0"})

    def call(self, function: str, **params) -> dict:
        """Call a Moodle web service function with retries."""
        url = f"{self.site_url}/webservice/rest/server.php"
        data = {
            "wstoken": self.token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
        }
        data.update(params)

        last_err = None
        for attempt in range(1, 4):
            try:
                resp = self.session.post(url, data=data, timeout=30)
                resp.raise_for_status()
                res_json = resp.json()

                # Check for Moodle errors
                if isinstance(res_json, dict) and "exception" in res_json:
                    raise MoodleError(
                        res_json.get("errorcode", "unknown"),
                        res_json.get("message", "Unknown error"),
                    )
                return res_json
            except MoodleError:
                raise
            except Exception as e:
                last_err = e
                if attempt < 3:
                    time.sleep(2 * attempt)
                    continue
                raise MoodleError("connection_error", f"REST API failed after 3 attempts: {last_err}")

    def get_site_info(self) -> dict:
        """Get site info and verify token."""
        return self.call("core_webservice_get_site_info")

    def get_courses(self, user_id: int) -> list:
        """Get enrolled courses for a user."""
        return self.call("core_enrol_get_users_courses", userid=user_id)

    def get_course_contents(self, course_id: int) -> list:
        """Get course modules/activities."""
        return self.call("core_course_get_contents", courseid=course_id)

    def get_autologin_key(self, private_token: str) -> dict:
        """Get a one-time autologin key for web access."""
        return self.call("tool_mobile_get_autologin_key", privatetoken=private_token)

    def get_mobile_content(self, component: str, method: str, args: dict) -> dict:
        """Call tool_mobile_get_content using permanent wstoken (no web cookie required)."""
        params = {
            "component": component,
            "method": method,
        }
        for idx, (k, v) in enumerate(args.items()):
            params[f"args[{idx}][name]"] = str(k)
            params[f"args[{idx}][value]"] = str(v)
        return self.call("tool_mobile_get_content", **params)


# ---------------------------------------------------------------------------
# Web-based Attendance Handler
# ---------------------------------------------------------------------------

class AttendanceChecker:
    """
    Checks for open attendance sessions and submits them.
    Uses web scraping approach since mod_attendance REST API may not be exposed.
    """

    def __init__(self, api: MoodleAPI, config: dict):
        self.api = api
        self.config = config
        self.web_session = requests.Session()
        self.web_session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            )
        })
        self._logged_in = False
        self._login_failure_reason = ""

    def _ensure_web_login(self) -> bool:
        """Get a web session using cookie or mobile autologin."""
        if self._logged_in:
            return True
        if self._login_failure_reason == "expired":
            return False

        site_url = self.api.site_url

        # Method 1: Check existing MoodleSession cookie
        cookie = self.config.get("moodle_session_cookie", "")
        if cookie:
            self.web_session.cookies.set("MoodleSession", cookie, domain="spectrum.um.edu.my")
            for attempt in range(1, 4):
                try:
                    resp = self.web_session.get(f"{site_url}/my/", timeout=15, allow_redirects=False)
                    if resp.status_code == 200 and "login" not in resp.url:
                        self._logged_in = True
                        self._login_failure_reason = ""
                        updated_cookie = self.web_session.cookies.get("MoodleSession", domain="spectrum.um.edu.my")
                        if updated_cookie and updated_cookie != cookie:
                            self.config["moodle_session_cookie"] = updated_cookie
                            save_config(self.config)
                        log("✅ Web session established & keep-alive pinged via saved cookie.")
                        # Clear alert timestamp if present
                        if os.path.exists(REMINDERS_FILE):
                            try:
                                with open(REMINDERS_FILE, "r", encoding="utf-8") as rf:
                                    rem_state = json.load(rf)
                                if "cookie_expired_last_alert_ts" in rem_state:
                                    rem_state.pop("cookie_expired_last_alert_ts", None)
                                    with open(REMINDERS_FILE, "w", encoding="utf-8") as wf:
                                        json.dump(rem_state, wf, indent=2, ensure_ascii=False)
                            except Exception:
                                pass
                        return True

                    # 303/302 Redirect to login/SSO means cookie expired
                    loc = resp.headers.get("Location", "")
                    if resp.status_code in (301, 302, 303, 307, 308) and (
                        "login" in loc.lower() or "microsoft" in loc.lower()
                    ):
                        log(f"⚠️ Saved cookie expired (redirect to {loc[:60]}...).")
                        break

                    if resp.status_code >= 500:
                        log(f"⚠️ SPeCTRUM server returned {resp.status_code} (attempt {attempt}/3). Retrying...")
                        time.sleep(3)
                        continue
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as net_err:
                    log(f"⚠️ Network error (attempt {attempt}/3): {net_err}")
                    time.sleep(3)
                    continue
                except Exception as e:
                    log(f"⚠️ Error testing cookie: {e}")
                    break

        # Method 2: Auto-login using private_token (never expires!)
        private_token = self.config.get("private_token", "")
        if private_token and self.api.token:
            log("🔄 Attempting automatic web login via Moodle autologin key...")
            try:
                autologin_data = self.api.get_autologin_key(private_token)
                autologin_url = autologin_data.get("autologinurl")
                if autologin_url:
                    resp = self.web_session.get(autologin_url, allow_redirects=True, timeout=20)
                    new_cookie = self.web_session.cookies.get("MoodleSession", domain="spectrum.um.edu.my")
                    if new_cookie:
                        self.config["moodle_session_cookie"] = new_cookie
                        save_config(self.config)
                        self._logged_in = True
                        self._login_failure_reason = ""
                        log("✅ Web session established via autologin key! (Cookie auto-refreshed)")
                        return True
            except Exception as e:
                log(f"⚠️ Autologin key generation failed: {e}")

        # If both methods failed:
        self._login_failure_reason = "expired"
        log("⚠️ SPeCTRUM web session cookie is currently expired.")
        return False

    def check_all_courses(self) -> list:
        """
        Check all enrolled courses for open attendance sessions.
        Returns list of results.
        """
        results = []
        user_id = self.config.get("user_id", 0)

        # Scan courses using Moodle REST API token (which remains valid long-term)
        # Web session login will be engaged dynamically if open attendance is found

        # Get enrolled courses
        try:
            courses = self.api.get_courses(user_id)
        except MoodleError as e:
            log(f"❌ Failed to get courses: {e}")
            return results

        log(f"📚 Found {len(courses)} enrolled courses")

        for course in courses:
            course_id = course["id"]
            course_name = course.get("shortname", course.get("fullname", f"Course {course_id}"))

            try:
                result = self._check_course_attendance(course_id, course_name)
                if result:
                    results.extend(result)
            except Exception as e:
                log(f"  ⚠️ Error checking {course_name}: {e}")

        return results

    def _check_course_attendance(self, course_id: int, course_name: str) -> list:
        """Check a single course for attendance activities."""
        results = []

        try:
            contents = self.api.get_course_contents(course_id)
        except MoodleError:
            return results

        # Find attendance modules accessible to this student
        for section in contents:
            for module in section.get("modules", []):
                if module.get("modname") == "attendance":
                    if module.get("uservisible") is False:
                        continue
                    mod_id = module["id"]
                    mod_name = module.get("name", "Attendance")
                    log(f"  📋 Checking attendance in {course_name}: {mod_name}")

                    result = self._check_attendance_module(
                        course_id, course_name, mod_id, mod_name,
                        module.get("url", "")
                    )
                    if result:
                        results.append(result)

        return results

    def _check_attendance_module(self, course_id, course_name, mod_id, mod_name, mod_url):
        """Check a specific attendance module for open sessions."""
        attendance_funcs = self.config.get("attendance_functions", [])

        if "mod_attendance_get_sessions" in attendance_funcs:
            return self._check_via_api(course_id, course_name, mod_id, mod_name)

        # Primary method: Moodle Mobile API (tool_mobile_get_content)
        # Uses permanent wstoken (just like assignment_reminder.py) and NEVER needs a browser cookie!
        mobile_ok, mobile_res = self._check_via_mobile_api(course_id, course_name, mod_id, mod_name)
        if mobile_ok:
            return mobile_res

        # Fallback: Web scraping if Mobile API is unavailable and cookie is active
        return self._check_via_web(course_id, course_name, mod_id, mod_name, mod_url)

    def _check_via_mobile_api(self, course_id, course_name, mod_id, mod_name):
        """
        Check and submit attendance via Moodle Mobile API (tool_mobile_get_content).
        Returns (handled_ok: bool, result: dict | None).
        """
        try:
            view_res = self.api.get_mobile_content(
                component="mod_attendance",
                method="mobile_view_activity",
                args={"cmid": str(mod_id), "courseid": str(course_id)},
            )
        except MoodleError as e:
            if e.code == "requireloginerror":
                # Restricted to another occurrence/group
                return True, None
            log(f"  ⚠️ Mobile API check failed for {course_name}: {e}")
            return False, None
        except Exception as e:
            log(f"  ⚠️ Mobile API check failed for {course_name}: {e}")
            return False, None

        html = "".join(t.get("html", "") for t in view_res.get("templates", []))
        if not html:
            return True, None

        # Extract open session IDs from ion-button [args]="{cmid: ..., courseid: ..., sessid: 12345, ...}"
        open_sessids = list(dict.fromkeys(re.findall(r"sessid:\s*(\d+)", html)))
        if not open_sessids:
            return True, None

        log(f"  🟢 OPEN session found via Mobile API in {course_name} ({mod_name})! sessid={open_sessids}")
        for sessid in open_sessids:
            try:
                form_res = self.api.get_mobile_content(
                    component="mod_attendance",
                    method="mobile_user_form",
                    args={
                        "cmid": str(mod_id),
                        "courseid": str(course_id),
                        "sessid": str(sessid),
                        "appversioncode": "44000",
                    },
                )
                form_html = "".join(t.get("html", "") for t in form_res.get("templates", []))

                if "subnetwrong" in form_html:
                    log(f"  ⚠️ {course_name}: Campus Wi-Fi / subnet required for sessid={sessid}.")
                    return True, {
                        "course": f"{course_name} ({mod_name})",
                        "status": "error",
                        "message": "Campus Wi-Fi (subnet restriction) required for this session.",
                        "sessid": sessid,
                    }

                # Check if password is required
                needs_pass = 'name="studentpass"' in form_html or "enterpassword" in form_html

                # Parse status options (<ion-radio value="stid">)
                soup = BeautifulSoup(form_html, "html.parser")
                present_id = None
                radios = soup.find_all("ion-radio")
                for radio in radios:
                    stid = radio.get("value")
                    item = radio.find_parent("ion-item")
                    label_text = (item.get_text(" ", strip=True) if item else radio.get_text(" ", strip=True)).lower()
                    if re.search(r"\b(present|hadir)\b", label_text) or label_text.strip() == "p":
                        present_id = stid
                        break

                if not present_id and radios:
                    present_id = radios[0].get("value")

                if needs_pass and not present_id:
                    log(f"  🔒 {course_name}: Session requires PASSWORD.")
                    return True, {
                        "course": f"{course_name} ({mod_name})",
                        "status": "needs_password",
                        "message": "Password required for this attendance session.",
                        "sessid": sessid,
                    }

                if not present_id:
                    log(f"  ⚠️ {course_name}: Could not find Present status option in mobile form.")
                    continue

                # Submit attendance via mobile_view_activity with sessid & status
                sub_res = self.api.get_mobile_content(
                    component="mod_attendance",
                    method="mobile_view_activity",
                    args={
                        "cmid": str(mod_id),
                        "courseid": str(course_id),
                        "sessid": str(sessid),
                        "status": str(present_id),
                        "studentpass": "",
                    },
                )
                sub_html = "".join(t.get("html", "") for t in sub_res.get("templates", []))

                if "incorrectpasswordshort" in sub_html:
                    log(f"  🔒 {course_name}: Session requires PASSWORD.")
                    return True, {
                        "course": f"{course_name} ({mod_name})",
                        "status": "needs_password",
                        "message": "Password required for this attendance session.",
                        "sessid": sessid,
                    }

                if "invalidstatus" in sub_html or "attendance_no_status" in sub_html:
                    log(f"  ⚠️ {course_name}: Status unavailable or session closed.")
                    continue

                # When submission succeeds, mobile_view_activity unsets sessid ($html['sessid'] = null)
                still_open = re.findall(rf"sessid:\s*{re.escape(str(sessid))}\b", sub_html)
                if not still_open:
                    log(f"  ✅ {course_name} ({mod_name}): MARKED PRESENT via Mobile API! 🎉")
                    return True, {
                        "course": f"{course_name} ({mod_name})",
                        "status": "success",
                        "message": "Marked as Present! 🎉 (via Mobile API)",
                        "sessid": sessid,
                    }
            except Exception as e:
                log(f"  ❌ Error submitting mobile attendance for {course_name} (sessid={sessid}): {e}")

        return True, None

    def _check_via_api(self, course_id, course_name, mod_id, mod_name):
        """Check attendance via Moodle API (if mod_attendance functions are available)."""
        try:
            sessions = self.api.call("mod_attendance_get_sessions", attendanceid=mod_id)
            for session in sessions:
                if self._is_session_open(session):
                    log(f"  🟢 Open session found in {course_name}!")
                    return self._submit_via_api(session, course_name)
        except MoodleError as e:
            log(f"  ⚠️ API attendance check failed: {e}")
            return None

    def _check_via_web(self, course_id, course_name, mod_id, mod_name, mod_url):
        """Check attendance via web scraping."""
        if not self._ensure_web_login():
            log("  ❌ Cannot establish web session")
            return None

        site_url = self.api.site_url
        view_url = f"{site_url}/mod/attendance/view.php?id={mod_id}"
        try:
            resp = self.web_session.get(view_url, timeout=15)
            html = resp.text
        except Exception as e:
            log(f"  ❌ Failed to fetch attendance page: {e}")
            return None

        # Parse links using BeautifulSoup for maximum accuracy
        soup = BeautifulSoup(html, "html.parser")
        target_links = []

        # Find all anchor tags that submit attendance
        for a in soup.find_all("a", href=True):
            href = a["href"]
            link_text = a.get_text(strip=True).lower()
            if "attendance.php" in href or "submit attendance" in link_text or "hantar kehadiran" in link_text:
                target_links.append((href, link_text))

        # Also fallback to regex in case links are embedded in JavaScript
        if not target_links:
            # Match attendance.php with sessid and sesskey (handles & and &amp;)
            raw_matches = re.findall(
                r'attendance\.php\?[^"\'\s>]+',
                html
            )
            for m in raw_matches:
                clean_url = m.replace("&amp;", "&")
                target_links.append((clean_url, "regex_found"))

        if not target_links:
            return None

        log(f"  🟢 OPEN session candidate found in {course_name}!")
        for href, text in target_links:
            # Normalize URL
            if href.startswith("/"):
                full_url = site_url + href
            elif not href.startswith("http"):
                full_url = f"{site_url}/mod/attendance/{href.lstrip('/')}"
            else:
                full_url = href
            full_url = full_url.replace("&amp;", "&")

            # Extract sessid if present
            parsed = urlparse(full_url)
            qs = parse_qs(parsed.query)
            sessid = qs.get("sessid", [None])[0]

            log(f"  ➡️ Navigating to submit attendance: {full_url}")
            try:
                sub_resp = self.web_session.get(full_url, timeout=15)
                res = self._parse_and_submit_form(sub_resp.text, course_name, sessid)
                if res:
                    return res
            except Exception as e:
                log(f"  ❌ Error submitting via link: {e}")

        return None

    def _parse_and_submit_form(self, html: str, course_name: str, sessid=None):
        """Parse the attendance form and submit 'Present' accurately."""
        soup = BeautifulSoup(html, "html.parser")

        # Find the attendance submission form
        form = soup.find("form", action=lambda x: x and "attendance.php" in x)
        if not form:
            form = soup.find("form")

        if not form:
            # Check if attendance is already recorded
            if any(w in html.lower() for w in ["self-recorded", "recorded", "hadir", "present"]):
                log(f"  ℹ️ {course_name}: Attendance already recorded.")
                return {"course": course_name, "status": "already_marked", "message": "Already marked present."}
            log(f"  ❌ {course_name}: Could not find attendance form.")
            return None

        # Check for password requirement
        if "password" in html.lower() and soup.find("input", {"type": "password"}):
            log(f"  🔒 {course_name}: Session requires PASSWORD.")
            return {"course": course_name, "status": "needs_password", "message": "Password required for this session"}

        # Collect all hidden inputs dynamically
        form_data = {}
        for hidden in form.find_all("input", type="hidden"):
            name = hidden.get("name")
            val = hidden.get("value", "")
            if name:
                form_data[name] = val

        # Locate Present radio option accurately
        # MUST use regex word boundaries so words like 'points' or 'participant' do not trigger!
        present_id = None
        for radio in form.find_all("input", {"name": "status"}):
            parent = radio.find_parent("label") or radio.find_parent("div")
            label_text = parent.get_text(strip=True).lower() if parent else ""

            # Check for words 'present' or 'hadir' or standalone 'p'
            if re.search(r'\b(present|hadir)\b', label_text) or label_text.strip() == "p":
                present_id = radio.get("value")
                break

        # Safe fallback: if labels couldn't be parsed, use the first radio button
        if not present_id:
            first_radio = form.find("input", {"name": "status"})
            if first_radio:
                present_id = first_radio.get("value")

        if not present_id:
            log(f"  ⚠️ {course_name}: Status options not found.")
            return None

        form_data["status"] = present_id
        form_data["submitbutton"] = "Save changes"

        submit_url = form.get("action", f"{self.api.site_url}/mod/attendance/attendance.php")
        if not submit_url.startswith("http"):
            submit_url = self.api.site_url + "/" + submit_url.lstrip("/")

        try:
            resp = self.web_session.post(submit_url, data=form_data, timeout=15)
            resp_lower = resp.text.lower()
            if resp.status_code == 200 and any(w in resp_lower for w in ["recorded", "present", "hadir", "self-recorded"]):
                log(f"  ✅ {course_name}: MARKED PRESENT! 🎉")
                return {
                    "course": course_name,
                    "status": "success",
                    "message": "Marked as Present! 🎉",
                    "sessid": sessid,
                }
            else:
                log(f"  ✅ {course_name}: Form submitted (HTTP {resp.status_code})")
                return {
                    "course": course_name,
                    "status": "success",
                    "message": "Submitted successfully.",
                    "sessid": sessid,
                }
        except Exception as e:
            log(f"  ❌ {course_name}: Submission failed: {e}")
            return {"course": course_name, "status": "error", "message": str(e)}

    def _is_session_open(self, session):
        """Check if a session is currently open for self-recording."""
        now = time.time()
        start = session.get("sessdate", 0)
        duration = session.get("duration", 3600)
        end = start + duration
        return start <= now <= end


# ---------------------------------------------------------------------------
# Telegram Notifications
# ---------------------------------------------------------------------------

def send_telegram(bot_token: str, chat_id: str, message: str):
    """Send a Telegram notification."""
    if not bot_token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception as e:
        log(f"  ⚠️ Telegram notification failed: {e}")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(message: str):
    """Print a timestamped log message safely on any OS console."""
    now = datetime.now().strftime("%H:%M:%S")
    try:
        print(f"  [{now}] {message}")
    except UnicodeEncodeError:
        safe = message.encode("ascii", errors="replace").decode("ascii")
        print(f"  [{now}] {safe}")


# ---------------------------------------------------------------------------
# Main Logic
# ---------------------------------------------------------------------------

def refresh_token_from_cookie(config: dict) -> str:
    """Auto-refresh wstoken via launch.php using MoodleSession cookie."""
    cookie = config.get("moodle_session_cookie")
    if not cookie:
        return None
    try:
        s = requests.Session()
        s.cookies.set("MoodleSession", cookie, domain="spectrum.um.edu.my")
        url = f"{config['site_url']}/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=12345&urlscheme=moodlemobile"
        r = s.get(url, allow_redirects=False, timeout=10)
        loc = r.headers.get("Location", "")
        m = re.search(r'token=([A-Za-z0-9+/=]+)', loc)
        if m:
            decoded = base64.b64decode(m.group(1)).decode("utf-8")
            parts = decoded.split(":::")
            if len(parts) >= 2:
                new_token = parts[1]
                config["token"] = new_token
                if len(parts) >= 3 and parts[2]:
                    config["private_token"] = parts[2]
                save_config(config)
                return new_token
    except Exception as e:
        log(f"⚠️ Failed to auto-refresh token: {e}")
    return None


def run_check(config: dict, single_run: bool = False):
    """Run a single attendance check cycle."""
    log("🔄 Starting attendance check...")
    ping_keep_alive(config)

    api = MoodleAPI(config["site_url"], config["token"])
    site_info = None

    # Step 1: Verify token or refresh if expired
    try:
        if config.get("token") and config["token"] != "YOUR_MOODLE_TOKEN_HERE_OR_LEAVE_EMPTY":
            site_info = api.get_site_info()
        else:
            raise MoodleError("notoken", "No valid token configured")
    except MoodleError as e:
        log(f"🔄 Token check failed ({e.message}). Auto-refreshing via cookie...")
        new_token = refresh_token_from_cookie(config)
        if new_token:
            api = MoodleAPI(config["site_url"], new_token)
            try:
                site_info = api.get_site_info()
            except MoodleError as e2:
                log(f"❌ Token refresh verification failed: {e2}")
        else:
            log("⚠️ Credentials currently inactive. Silent wait mode (no alerts sent).")
            return []

    # Step 2: Auto-populate user_id and profile from site_info if missing
    if site_info:
        user_id = site_info.get("userid", 0)
        fullname = site_info.get("fullname", "")
        username = site_info.get("username", "")
        if os.environ.get("GITHUB_ACTIONS"):
            log(f"👤 Authenticated with SPeCTRUM Mobile API [OK]")
        else:
            log(f"👤 Logged in as: {fullname} ({username}) [UserID: {user_id}]")

        needs_save = False
        if config.get("user_id", 0) != user_id and user_id != 0:
            config["user_id"] = user_id
            needs_save = True
        if not config.get("fullname") and fullname:
            config["fullname"] = fullname
            needs_save = True
        if not config.get("username") and username:
            config["username"] = username
            needs_save = True
        if needs_save:
            save_config(config)

    # Step 3: Check attendance
    checker = AttendanceChecker(api, config)
    results = checker.check_all_courses()

    # Step 4: Report results
    if not results:
        log("📭 No open attendance sessions found.")
    else:
        for r in results:
            status = r.get("status", "unknown")
            course = r.get("course", "Unknown")
            message = r.get("message", "")

            if status in ("success", "already_marked"):
                emoji = "✅"
            elif status in ("needs_password", "needs_qr"):
                emoji = "⚠️"
            else:
                emoji = "❌"

            log(f"{emoji} {course}: {message}")

            # Send Telegram notification for actions (deduplicated per session ID)
            if status != "auth_error":
                sessid = r.get("sessid")
                dedup_key = f"sess_{sessid}_{status}" if sessid else None
                already_notified = False
                rem_state = {}
                if dedup_key and os.path.exists(REMINDERS_FILE):
                    try:
                        with open(REMINDERS_FILE, "r", encoding="utf-8") as rf:
                            rem_state = json.load(rf)
                        if dedup_key in rem_state.get("notified_sessids", []):
                            already_notified = True
                    except Exception:
                        pass

                if not already_notified:
                    tg_token = config.get("telegram_bot_token", "")
                    tg_chat = config.get("telegram_chat_id", "")
                    if tg_token and tg_chat:
                        tg_msg = f"{emoji} <b>{course}</b>\n{message}"
                        send_telegram(tg_token, tg_chat, tg_msg)
                    if dedup_key:
                        try:
                            notified_list = rem_state.get("notified_sessids", [])
                            notified_list.append(dedup_key)
                            rem_state["notified_sessids"] = notified_list[-200:]
                            with open(REMINDERS_FILE, "w", encoding="utf-8") as wf:
                                json.dump(rem_state, wf, indent=2, ensure_ascii=False)
                        except Exception:
                            pass

            if status == "auth_error":
                log("⚠️ Web session unavailable. Skipping submission.")

    # Step 5: Check upcoming assignment deadlines
    try:
        from assignment_reminder import check_assignment_deadlines
        tg_token = config.get("telegram_bot_token", "")
        tg_chat = config.get("telegram_chat_id", "")
        alerts = check_assignment_deadlines(config["site_url"], config["token"], tg_token, tg_chat)
        if alerts:
            log(f"⏰ Sent {len(alerts)} assignment deadline alerts: {', '.join(alerts)}")
    except Exception as e:
        log(f"⚠️ Assignment check note: {e}")

    log("✔️ Check complete.")
    return results


def ping_keep_alive(config: dict):
    """Silent ping to keep optional MoodleSession cookie active if present."""
    cookie = config.get("moodle_session_cookie")
    if not cookie:
        return
    try:
        s = requests.Session()
        s.cookies.set("MoodleSession", cookie, domain="spectrum.um.edu.my")
        s.headers.update({"User-Agent": "Mozilla/5.0 Chrome/128.0.0.0"})
        r = s.get(f"{config['site_url']}/my/", timeout=10, allow_redirects=False)
        if r.status_code == 200 and "login" not in r.url:
            log("💓 Web session keep-alive active")
    except Exception:
        pass


def run_scheduled(config: dict):
    """Run attendance checks on a schedule."""
    import schedule as sched

    interval = config.get("check_interval_minutes", 60)

    print()
    print("  " + "=" * 55)
    print("   UM Auto-Attend: Running")
    print(f"   Checking every {interval} minutes")
    print("   Keep-alive heartbeat: every 20 minutes")
    print("   Press Ctrl+C to stop")
    print("  " + "=" * 55)
    print()

    # Run immediately on start
    run_check(config)

    # Schedule periodic checks
    sched.every(interval).minutes.do(run_check, config)

    # Schedule keep-alive every 20 minutes to prevent idle expiration
    sched.every(20).minutes.do(ping_keep_alive, config)

    try:
        while True:
            sched.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n\n  👋 Stopped. Goodbye!")


def sync_reminders_git():
    """Commit and push reminders_sent.json if running inside GitHub Actions."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        import subprocess
        subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", "reminders_sent.json"], check=False)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "chore: update reminder state [skip ci]"], check=False)
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
            subprocess.run(["git", "push", "origin", "main"], check=False)
    except Exception as e:
        log(f"⚠️ Git sync note: {e}")


def run_cloud_loop(config: dict, duration_minutes: int):
    """Run checks in a loop inside GitHub Actions for duration_minutes (24/7 continuous)."""
    interval = max(1, config.get("check_interval_minutes", 5))
    end_time = time.time() + (duration_minutes * 60)
    cycle = 0

    print()
    print("  " + "=" * 55)
    print(f"   UM Auto-Attend: Cloud Loop ({duration_minutes} mins, every {interval}m)")
    print("  " + "=" * 55)
    print()

    while True:
        cycle += 1
        log(f"☁️ Cloud check cycle #{cycle}...")
        try:
            run_check(config, single_run=True)
            sync_reminders_git()
        except Exception as e:
            log(f"⚠️ Cloud cycle error: {e}")

        remaining = end_time - time.time()
        if remaining <= interval * 60:
            log("⏹️ Cloud loop window finished. Handing off to next run...")
            break
        sleep_secs = min(interval * 60, max(10, int(remaining)))
        time.sleep(sleep_secs)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="UM Auto-Attend")
    parser.add_argument(
        "--test", action="store_true",
        help="Run once and exit (test mode)"
    )
    parser.add_argument(
        "--interval", type=int, default=None,
        help="Check interval in minutes (default: 60)"
    )
    parser.add_argument(
        "--cloud-loop", type=int, default=0,
        help="Run in cloud loop mode for N minutes before exiting"
    )
    args = parser.parse_args()

    config = load_config()

    if args.interval:
        config["check_interval_minutes"] = args.interval

    if args.cloud_loop and args.cloud_loop > 0:
        run_cloud_loop(config, args.cloud_loop)
    elif args.test:
        print()
        print("  " + "=" * 55)
        print("   UM Auto-Attend: Test Mode")
        print("  " + "=" * 55)
        print()
        run_check(config, single_run=True)
        print()
    else:
        run_scheduled(config)


if __name__ == "__main__":
    main()
