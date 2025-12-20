## Social Scheduler (Pi-ready)

Upload 7–10 shorts once and auto-post them to YouTube Shorts, Instagram Reels, and TikTok on a shared schedule. Built for Raspberry Pi 8GB, mobile-friendly UI (via Twingate), and local persistence.

### Quick start (Docker)
```powershell
docker build -t social-scheduler .
docker run -d --name scheduler ^
  -p 8501:8501 ^
  -e TZ="UTC" ^
  --shm-size=1g ^
  -v %cd%/data:/app/data ^
  social-scheduler
```

```bash
docker build -t social-scheduler .
docker run -d --name scheduler -p 8501:8501 -e TZ="CET" --shm-size=1g -v $(pwd)/data:/app/data social-scheduler
```

### Local (no Docker)
```bash
python -m venv .venv
. .venv/Scripts/activate  # Windows
pip install -r requirements.txt
streamlit run main.py  # UI
python run_worker.py   # worker (separate shell)
```

### Daily flow
- Drop multiple mp4/mov files in **Upload & Queue**. One title/description/time applies to all. Videos are shuffled and per-platform order is randomized to avoid simultaneous posting.
- Preview clips inline; “Push to next slot” reshuffles without re-uploading.
- Dashboard shows remaining storage; “Clean oldest uploads” frees space by deleting already-posted files first.
- Logs are visible/downloadable from the **Logs** tab.

### Accounts (tokens/cookies)
- **YouTube**: In **Accounts → Google API Setup**, paste your Desktop OAuth client JSON. Open OAuth, paste the code back, and link. Tokens auto-refresh.
- **Instagram**: Enter username/password; stored locally.
- **TikTok**: Paste `sessionid` — any of:
  - Raw value: `123...`
  - Cookie header snippet: `sessionid=123...; path=/;`
  - DevTools JSON export containing `cookies` or `sessionid`
  The UI verifies immediately, warns after ~25 days, and the worker blocks uploads if invalid.
- **Telegram** (optional): Add bot token + chat ID in Settings for alerts on failures/auth gaps.

### Tips
- Keep `data/` backed up when you rebuild the Pi; it contains the DB, schedule, credentials, and upload history.
- If TikTok/YouTube stop uploading, re-paste the cookie/auth code; verification buttons are in **Accounts**.
