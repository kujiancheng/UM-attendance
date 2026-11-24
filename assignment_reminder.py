"""
Module: Assignment Due Reminder for UM SPeCTRUM
=================================================
Monitors SPeCTRUM for new assignments and due dates.
Sends instant Telegram alerts when:
  1. A NEW assignment is posted by a lecturer.
  2. An upcoming assignment reaches milestones: 7 days, 3 days, 24h, 12h, 3h, 1h before deadline.
"""

import json
import os
import sys
import time
from datetime import datetime
import requests

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REMINDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders_sent.json")


def load_reminders_state() -> dict:
    if os.path.exists(REMINDERS_FILE):
        try:
            with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_reminders_state(state: dict):
    try:
        with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving reminders state: {e}")


def format_time_remaining(seconds: float) -> str:
    if seconds <= 0:
        return "Overdue"
    days = seconds / 86400.0
    if days >= 1.0:
        return f"{round(days, 1)} days"
    hours = seconds / 3600.0
    if hours >= 1.0:
        return f"{round(hours, 1)} hours"
    minutes = int(seconds // 60)
    return f"{minutes} minutes"


def check_submission_status(site_url: str, token: str, assign_id: int):
    """
    Check if the user (or their group) has already submitted the assignment.
    Returns:
      True  -> Confirmed submitted (individual or team submission, or graded)
      False -> Confirmed not submitted
      None  -> API/network error (caller must NOT send a false 'not submitted' alert)
    """
    url = f"{site_url.rstrip('/')}/webservice/rest/server.php"
    data = {
        "wstoken": token,
        "wsfunction": "mod_assign_get_submission_status",
        "assignid": assign_id,
        "moodlewsrestformat": "json",
    }
    for attempt in range(1, 4):
        try:
            sub_r = requests.post(url, data=data, timeout=15)
            sub_r.raise_for_status()
            sub_data = sub_r.json()
            if isinstance(sub_data, dict) and "exception" in sub_data:
                return None

            last_attempt = sub_data.get("lastattempt") or {}
            if last_attempt.get("gradingstatus") == "graded":
                return True

            # Check both individual submission and group/team submission
            for key in ("submission", "teamsubmission"):
                sub = last_attempt.get(key) or {}
                if sub.get("status") == "submitted":
                    return True
                # Also check if files or online text were uploaded in draft/team plugins
                for plugin in sub.get("plugins", []):
                    for fb in plugin.get("fileareas", []):
                        if fb.get("files"):
                            return True
                    for ef in plugin.get("editorfields", []):
                        if ef.get("text", "").strip():
                            return True
            return False
        except Exception:
            if attempt < 3:
                time.sleep(2 * attempt)
                continue
            return None
    return None


def send_tg_message(tg_token: str, tg_chat: str, msg: str):
    """Safely post a Telegram notification."""
    if not tg_token or not tg_chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            json={"chat_id": tg_chat, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")


def check_assignment_deadlines(site_url: str, token: str, tg_token: str = None, tg_chat: str = None) -> list:
    """
    Check all enrolled courses for new assignments and approaching deadlines.
    Milestones: 7 days, 3 days, 24 hours, 12 hours, 3 hours, 1 hour.
    """
    now = time.time()
    state = load_reminders_state()
    alerts_sent = []

    if not token or token == "YOUR_MOODLE_TOKEN_HERE_OR_LEAVE_EMPTY":
        return []

    # 1. Fetch assignments across all enrolled courses (with POST + retry)
    courses = []
    for attempt in range(1, 4):
        try:
            r = requests.post(
                f"{site_url.rstrip('/')}/webservice/rest/server.php",
                data={
                    "wstoken": token,
                    "wsfunction": "mod_assign_get_assignments",
                    "moodlewsrestformat": "json",
                },
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            courses = data.get("courses", [])
            break
        except Exception as e:
            if attempt < 3:
                time.sleep(2 * attempt)
                continue
            print(f"Failed to fetch assignments: {e}")
            return []

    for c in courses:
        cname = c.get("shortname", c.get("fullname", ""))
        for a in c.get("assignments", []):
            assign_id = str(a["id"])
            assign_name = a.get("name", "Untitled Assignment")
            duedate = a.get("duedate", 0)

            # Silently archive past assignments from previous semesters/weeks
            if duedate and duedate <= now:
                if assign_id not in state:
                    state[assign_id] = ["archived"]
                continue

            # If already confirmed submitted (or group-leader submitted), skip immediately
            if state.get(assign_id) == ["submitted"]:
                continue

            time_left_sec = (duedate - now) if duedate else None
            time_left_hours = (time_left_sec / 3600.0) if time_left_sec is not None else None
            due_str = datetime.fromtimestamp(duedate).strftime("%d %b %Y, %I:%M %p") if duedate else "No due date specified"
            time_desc = format_time_remaining(time_left_sec) if time_left_sec is not None else "N/A"

            # Check if this assignment is already submitted
            is_submitted = check_submission_status(site_url, token, a["id"])
            if is_submitted is True:
                state[assign_id] = ["submitted"]
                continue
            if is_submitted is None:
                # Transient network/API error: do not send false 'not submitted' alert
                continue

            # -------------------------------------------------------------
            # FEATURE 1: New Assignment Detection
            # -------------------------------------------------------------
            if assign_id not in state:
                # Brand new assignment detected!
                new_msg = (
                    f"📢 <b>NEW ASSIGNMENT POSTED!</b>\n\n"
                    f"📚 <b>Course:</b> {cname}\n"
                    f"📝 <b>Task:</b> {assign_name}\n"
                    f"📅 <b>Due Date:</b> {due_str}\n"
                    f"⏳ <b>Time Left:</b> ~{time_desc}\n\n"
                    f"⚠️ <i>Status: Not submitted yet!</i>"
                )
                print(f"Sending NEW assignment alert: [{cname}] {assign_name}")
                send_tg_message(tg_token, tg_chat, new_msg)

                # Initialize state for this assignment
                sent_for_this = ["new_alert"]

                # If due within closer milestones, mark earlier ones as acknowledged
                # so we don't spam duplicate alerts in the exact same run
                if time_left_hours is not None:
                    if time_left_hours <= 168.0:
                        sent_for_this.append("7d")
                    if time_left_hours <= 72.0:
                        sent_for_this.append("3d")
                    if time_left_hours <= 24.0:
                        sent_for_this.append("24h")
                    if time_left_hours <= 12.0:
                        sent_for_this.append("12h")
                    if time_left_hours <= 3.0:
                        sent_for_this.append("3h")
                    if time_left_hours <= 1.0:
                        sent_for_this.append("1h")

                state[assign_id] = sent_for_this
                alerts_sent.append(f"NEW: {cname} - {assign_name}")
                continue

            # -------------------------------------------------------------
            # FEATURE 2: Approaching Due Date Reminders (7d, 3d, 24h, 12h, 3h, 1h)
            # -------------------------------------------------------------
            sent_for_this = state.get(assign_id, [])
            if not isinstance(sent_for_this, list):
                sent_for_this = []

            if time_left_hours is None or time_left_hours > 168.5:
                # Due date is more than 7 days away; no milestone reminder needed yet
                continue

            threshold = None
            emoji = "⏰"
            title = ""

            # Check milestones in descending order of urgency: 1h -> 3h -> 12h -> 24h -> 3d -> 7d
            if time_left_hours <= 1.0:
                if "1h" not in sent_for_this:
                    threshold = "1h"
                    emoji = "🚨"
                    title = "URGENT: DUE IN 1 HOUR!"
                for m in ["7d", "3d", "24h", "12h", "3h", "1h"]:
                    if m not in sent_for_this:
                        sent_for_this.append(m)

            elif time_left_hours <= 3.0:
                if "3h" not in sent_for_this:
                    threshold = "3h"
                    emoji = "⚠️"
                    title = "ASSIGNMENT DUE IN 3 HOURS"
                for m in ["7d", "3d", "24h", "12h", "3h"]:
                    if m not in sent_for_this:
                        sent_for_this.append(m)

            elif time_left_hours <= 12.0:
                if "12h" not in sent_for_this:
                    threshold = "12h"
                    emoji = "⏳"
                    title = "ASSIGNMENT DUE IN 12 HOURS"
                for m in ["7d", "3d", "24h", "12h"]:
                    if m not in sent_for_this:
                        sent_for_this.append(m)

            elif time_left_hours <= 24.0:
                if "24h" not in sent_for_this:
                    threshold = "24h"
                    emoji = "⏰"
                    title = "ASSIGNMENT DUE IN 24 HOURS (TOMORROW)"
                for m in ["7d", "3d", "24h"]:
                    if m not in sent_for_this:
                        sent_for_this.append(m)

            elif time_left_hours <= 72.0:
                if "3d" not in sent_for_this:
                    threshold = "3d"
                    emoji = "⏳"
                    title = "ASSIGNMENT DUE IN 3 DAYS"
                for m in ["7d", "3d"]:
                    if m not in sent_for_this:
                        sent_for_this.append(m)

            elif time_left_hours <= 168.0:
                if "7d" not in sent_for_this:
                    threshold = "7d"
                    emoji = "🗓️"
                    title = "ASSIGNMENT DUE IN 7 DAYS"
                for m in ["7d"]:
                    if m not in sent_for_this:
                        sent_for_this.append(m)

            if threshold:
                msg = (
                    f"{emoji} <b>{title}</b>\n\n"
                    f"📚 <b>Course:</b> {cname}\n"
                    f"📝 <b>Task:</b> {assign_name}\n"
                    f"⏳ <b>Time Left:</b> ~{time_desc}\n"
                    f"📅 <b>Due Date:</b> {due_str}\n\n"
                    f"⚠️ <i>Status: Not submitted yet!</i>"
                )
                print(f"Sending deadline reminder: [{cname}] {assign_name} ({threshold})")
                send_tg_message(tg_token, tg_chat, msg)

                state[assign_id] = sent_for_this
                alerts_sent.append(f"{threshold}: {cname} - {assign_name}")

    save_reminders_state(state)
    return alerts_sent


def get_upcoming_assignments_summary(site_url: str, token: str) -> str:
    """Return a formatted string of all upcoming assignments and submission status."""
    now = time.time()
    state = load_reminders_state()
    try:
        r = requests.post(
            f"{site_url.rstrip('/')}/webservice/rest/server.php",
            data={
                "wstoken": token,
                "wsfunction": "mod_assign_get_assignments",
                "moodlewsrestformat": "json"
            },
            timeout=20
        )
        data = r.json()
        courses = data.get("courses", [])
    except Exception as e:
        return f"❌ Failed to fetch assignments: {e}"

    upcoming = []
    for c in courses:
        cname = c.get("shortname", c.get("fullname", ""))
        for a in c.get("assignments", []):
            due = a.get("duedate", 0)
            if due and due > now:
                aid = str(a["id"])
                is_submitted = (state.get(aid) == ["submitted"]) or (check_submission_status(site_url, token, a["id"]) is True)
                status_text = "✅ Submitted" if is_submitted else "⚠️ Not submitted"

                time_left_days = (due - now) / 86400.0
                if time_left_days < 1:
                    time_left_str = f"{round((due - now) / 3600.0, 1)} hours"
                else:
                    time_left_str = f"{round(time_left_days, 1)} days"

                due_str = datetime.fromtimestamp(due).strftime("%d %b %Y, %I:%M %p")
                upcoming.append((
                    due,
                    f"📚 <b>[{cname}]</b> {a.get('name')}\n"
                    f"   📅 <b>Due:</b> {due_str} (~{time_left_str} left)\n"
                    f"   <b>Status:</b> {status_text}\n"
                ))

    if not upcoming:
        return "🎉 No upcoming assignments found! All clear!"

    upcoming.sort(key=lambda x: x[0])
    lines = ["📝 <b>Upcoming Assignments:</b>\n"]
    for idx, (_, item) in enumerate(upcoming, 1):
        lines.append(f"{idx}. {item}")

    return "\n".join(lines)


if __name__ == "__main__":
    CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    print("Checking assignments...")
    alerts = check_assignment_deadlines(
        cfg["site_url"], cfg["token"],
        cfg.get("telegram_bot_token"), cfg.get("telegram_chat_id")
    )
    print(f"Alerts triggered: {alerts}")
    print()
    print(get_upcoming_assignments_summary(cfg["site_url"], cfg["token"]))
