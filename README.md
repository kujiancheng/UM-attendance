# 🎓 UM Auto-Attend & Keep-Alive Suite

An automated attendance tracker, keep-alive heartbeat, and deadline notifier for **Universiti Malaya SPeCTRUM (Moodle)**.

---

## ⚡ What Was Fixed & Improved

1. **Persistent Authentication & Token Management**:
   - Fixed expired cookie handling by integrating **SPeCTRUM Mobile App API Tokens** (`wstoken`) and **Moodle Autologin keys** (`tool_mobile_get_autologin_key`).
   - Automatically generates and renews web session cookies so you don't need to manually update cookies constantly.
2. **Automatic User ID Discovery**:
   - Resolved the issue where `user_id` was hardcoded to `0`, preventing course scans from completing.
   - User ID, Full Name, and Student Email are now auto-discovered from SPeCTRUM's `core_webservice_get_site_info`.
3. **Accurate Status Option Selection**:
   - Fixed regex status selection so words like `"points"` in `"Absent (0 points)"` or `"Excused (0 points)"` will **never** accidentally select Absent.
   - Accurately targets **"Present"** / **"Hadir"**.
4. **Resilient URL Parsing**:
   - Upgraded attendance link parsing with `BeautifulSoup` to handle HTML entity encodings (`&amp;`) and relative URLs seamlessly.
5. **Scheduled Automation on GitHub Actions**:
   - Added automatic cron schedule (`schedule: - cron: '*/30 0-14 * * 1-5'`) to run every 30 minutes during class hours (8:00 AM – 10:00 PM MYT, Mon–Fri).
   - Retained `workflow_dispatch` for manual 1-click execution and external webhook triggers.
   - Added automatic persistence for refreshed tokens.
6. **Telegram Failure Notifications**:
   - Sends immediate Telegram alerts if your session genuinely expires so you never miss an attendance window without knowing.
7. **Smart Assignment Tracker & Notifications**:
   - **Instant New Assignment Alert**: Whenever a lecturer posts a new assignment on SPeCTRUM, an alert pops up immediately on Telegram.
   - **Deadline Reminders**: Sends countdown reminders at **7 days**, **3 days**, **24 hours**, **12 hours**, **3 hours**, and **1 hour** before submission deadlines if unsubmitted.

---

## 🚀 Quick Setup (1 Minute)

### 1. Configure Credentials
Run the interactive setup wizard:
```bash
python setup_token.py
```

It will guide you to copy your `MoodleSession` cookie from Chrome:
1. Open [SPeCTRUM](https://spectrum.um.edu.my) in Chrome and log in.
2. Press **F12** -> click the **Console** tab.
3. Paste this command and hit Enter:
   ```javascript
   copy(document.cookie.match(/MoodleSession=([^;]+)/)?.[1])
   ```
4. Paste the copied string into the setup prompt.

The wizard will automatically:
- Verify your login on SPeCTRUM.
- Generate your long-lived Mobile API token.
- Fetch your Student ID and enrolled courses.
- Test your Telegram Bot (optional).
- Save everything into `config.json`.

---

## 🧪 Testing & Running

### Single Test Check
Run once to verify course scanning and submission:
```bash
python auto_attend.py --test
```

### Run Locally (Scheduled)
Runs in the background, checking every 60 minutes with a keep-alive ping every 20 minutes:
```bash
python auto_attend.py
```

### Run 24/7 Cloud Suite (Auto-Attend + Telegram QR Bot)
```bash
python run.py
```

---

## ☁️ Running on GitHub Actions (Zero Local Resources)

This repository includes a pre-configured GitHub Actions workflow in `.github/workflows/main.yml`.

1. Commit and push your changes to your private repository:
   ```bash
   git add .
   git commit -m "fix: auto attendance configuration and workflow"
   git push origin main
   ```
2. Navigate to your repository on GitHub -> **Actions** -> **UM Auto-Attend & Keep-Alive**.
3. Click **Run workflow** to test it anytime.
4. The workflow will automatically run every 30 minutes on weekdays during Malaysian class hours (8:00 AM – 10:00 PM MYT).

---

## 📁 Repository Structure

- `auto_attend.py` — Core attendance engine & keep-alive monitor.
- `setup_token.py` — 1-minute interactive setup wizard.
- `assignment_reminder.py` — Deadline watcher for upcoming assignments (24h, 12h, 3h, 1h).
- `qr_relay_bot.py` — Telegram bot to submit attendance from QR code photos sent by classmates.
- `run.py` — 24/7 runner with built-in health-check server for cloud hosting.
- `config.json` — Local configuration & session tokens (keep private!).
- `config.example.json` — Clean configuration template.
- `.github/workflows/main.yml` — Automated GitHub Actions workflow.
