## Social Scheduler

Automated short-form video publisher inspired by Buffer. Upload a week's worth of clips once and let the Raspberry Pi worker drip them to YouTube Shorts, Instagram Reels, and TikTok on a shared schedule. Telegram notifications keep you updated when accounts are missing or uploads fail.

### Features
- Multi-platform queue with one global title/caption.
- Configurable weekday/time schedule + timezone awareness.
- OAuth-based YouTube link, stored Instagram credentials/session cache, TikTok session cookie.
- Telegram alerts for authentication gaps, upload failures, and manual test pings.
- Headless worker (`python run_worker.py`) that watches due items and retries up to three times, automatically rescheduling failed attempts.
- Centralized rotating log file (`data/logs/scheduler.log`) surfaced inside the Streamlit UI for quick troubleshooting and downloadable archives.

### Requirements
- Python 3.9+ on your Raspberry Pi or workstation.
- Chrome/Chromium + matching chromedriver for TikTok headless uploads.
- `client_secret.json` from Google Cloud (YouTube Data API v3) placed in the project root.
- Telegram bot token and chat ID (optional, but recommended).

Install dependencies:

```bash
python -m venv .venv
. .venv/Scripts/activate  # Windows
pip install -r requirements.txt
```

### Running
1. Start the Streamlit UI for configuration/uploads:
   ```bash
   streamlit run main.py
   ```
2. In a separate terminal (or systemd service on the Pi), run the worker:
   ```bash
   python run_worker.py
   ```

Keep both processes running. The UI writes to `data/` (SQLite + uploaded files) that the worker reads.

### Platform Setup
- **Google OAuth**: in *Accounts → Google API Setup* paste the OAuth client JSON you downloaded from Google Cloud (Desktop application). No file uploads required.
- **YouTube**: once the client JSON is saved, click *Open Google OAuth* in the UI, grant access, paste the auth code back, and verify connection.
- **Instagram**: enter username/password; the app stores and refreshes the session token automatically after the first login.
- **TikTok**: paste either the raw `sessionid` value, a cookie header snippet, or the exported JSON from your browser—the Accounts tab parses it, stores the cookie, and lets you re-verify / clear it without touching the server.

### Token Health
- The app keeps track of when TikTok session cookies were stored and last verified. Hit **Verify session now** anytime to confirm it is still live; the worker also re-validates every few hours before uploads.
- When a TikTok session is 25+ days old or fails verification, you will see warnings in the UI and receive Telegram alerts before uploads start failing.
- YouTube tokens auto-refresh using the stored OAuth client; Instagram sessions refresh automatically after each successful login through Instagrapi.

### Logs
- Both the Streamlit UI and the worker log to `data/logs/scheduler.log`. The new **Logs** tab in the UI tails the file, lets you refresh on demand, and offers a download button for sharing diagnostics.
- **Instagram**: supply username/password; Instagrapi refreshes the session automatically.
- **TikTok**: copy the `sessionid` cookie from a logged-in browser session (Developer Tools → Application → Cookies).

### Telegram Notifications
Add your bot token and chat ID in *Settings → Telegram alerts*. Use the *Send test alert* button to confirm connectivity. The worker will now push messages when:
- An account is missing credentials during a scheduled upload.
- A platform upload throw exceptions.
- A video finishes uploading everywhere.

### Tips
- Use the *Push to next slot* action to shuffle queue items without re-uploading.
- Rotate ChromeDriver updates alongside Chromium upgrades for TikTok stability.
- Back up `data/scheduler.db` if you rebuild the Pi—this includes schedule config, credentials, and queue history.
