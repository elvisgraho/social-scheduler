## Social Scheduler (Pi-ready)

Upload 7–10 shorts once and auto-post them to YouTube Shorts, Instagram Reels, and TikTok on a shared schedule. Built for Raspberry Pi 8GB, mobile-friendly UI, and local persistence.

### What you need
- Raspberry Pi (or any Linux host) with Docker or Python 3.11.
- Chromium + chromedriver (bundled in the Dockerfile).
- Google Cloud project with YouTube Data API v3 enabled and one Desktop OAuth client JSON.
- Instagram session cookie (`sessionid`) exported from a trusted device (preferred) or instagrapi settings JSON.
- TikTok `sessionid` cookie from a logged-in browser.
- (Optional) Telegram bot token + chat ID for alerts.

### Quick start (Docker)
```bash
docker build -t social-scheduler .
sudo docker run -d --name scheduler \
  -p 8501:8501 \
  -e TZ="UTC" \
  -e OAUTH_REDIRECT_URI="http://localhost:8080" \
  --shm-size=1g \
  -v $(pwd)/data:/app/data \
  social-scheduler
```
Windows PowerShell:
```powershell
docker run -d --name scheduler `
  -p 8501:8501 `
  -e TZ="UTC" `
  -e OAUTH_REDIRECT_URI="http://localhost:8080" `
  --shm-size=1g `
  -v ${PWD}/data:/app/data `
  social-scheduler
```
UI: `http://<pi-ip>:8501`. The `data/` volume holds DB, uploads, cookies/tokens, and logs. OAuth uses the loopback redirect `http://localhost:8080`; when Google redirects there, copy the code from the browser and paste it in the app.

### Local (no Docker)
```bash
python -m venv .venv
. .venv/Scripts/activate  # Windows
pip install -r requirements.txt
streamlit run main.py  # UI
python run_worker.py   # worker (separate shell)
```

### Configure accounts (step-by-step)
- **YouTube (required)**
  1) In Google Cloud Console, create a project and enable **YouTube Data API v3**.
  2) OAuth consent screen: set up External, add your Google account under Test Users.
  3) Credentials: create **OAuth client ID → Desktop app**; download the JSON.
  4) In the app UI (Accounts → Google API Setup), paste the JSON and save.
  5) Click “Open Google OAuth screen”, sign in, copy the code Google shows (loopback redirect `http://localhost:8080`), paste it back, and finish. Tokens auto-refresh; reuse this same JSON everywhere.
- **Instagram (recommended: session-based)**
  - Best: grab `sessionid` from a trusted device (browser DevTools Cookies for instagram.com) or paste instagrapi settings JSON. Save it in Accounts → Instagram → session form, then Verify.
  - Fallback: username/password (expand the fallback form). May trigger challenges; use only if a session isn’t available.
- **TikTok**
  - Paste `sessionid` in Accounts → TikTok. Accepted formats: raw value (`123...`), cookie header (`sessionid=123...; path=/;`), or DevTools JSON export containing `cookies`/`sessionid`. Verify to confirm; the worker blocks uploads if invalid.
- **Telegram (optional)**
  - Add bot token + chat ID in Settings. Alerts fire on failures/auth gaps and when the queue auto-pauses.

### Daily flow
- Upload & Queue: drop multiple mp4/mov files. One title/description/time applies to all; videos and platform order are randomized to avoid simultaneous posting. Preview clips inline.
- Queue controls: Pause/Resume uploads, “Upload next now” to force the next item, “Push to next slot” per item, auto-pause on failures with Telegram alert.
- Storage: dashboard shows free/used space; “Clean oldest uploads” deletes already-posted files first.
- Logs: view/download `data/logs/scheduler.log` from the Logs tab.

### Tips
- Keep `data/` backed up when you rebuild the Pi; it contains the DB, schedule, credentials, and upload history.
- If TikTok/YouTube stop uploading, re-paste the cookie/auth code and use the Verify buttons in Accounts.
