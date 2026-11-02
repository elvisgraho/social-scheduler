## Social Scheduler (Pi-ready)

Schedule short videos for YouTube Shorts, Instagram Reels, and TikTok. Runs locally or in Docker on a 64-bit Raspberry Pi.

### What you need

- **Python:** 3.12 for local development, matching the Docker image.
- **System:** Raspberry Pi (or Linux/Mac) with Docker.
- Docker installs the browser and FFmpeg automatically. No Chrome or ChromeDriver setup.
- **Raspberry Pi:** Use a 64-bit OS and ARM64 Docker image.
- **Accounts:**
  - **YouTube:** Google Cloud Project with "YouTube Data API v3" and a **Desktop** OAuth Client.
  - **Instagram:** Session ID (preferred) + Username/Password (for auto-recovery).
  - **TikTok:** Valid `sessionid` cookie from a logged-in browser.

### Quick start (Docker)

```bash
# Build
docker build -t social-scheduler .

# Run (Linux/Mac)
docker run -d --init --name scheduler \
  -p 8501:8501 \
  -v $(pwd)/data:/app/data \
  --shm-size=2g \
  --restart unless-stopped \
  social-scheduler
```

Windows PowerShell:

```powershell
docker run -d --init --name scheduler `
  -p 8501:8501 `
  -v ${PWD}/data:/app/data `
  --shm-size=2g `
  --restart unless-stopped `
  social-scheduler
```
Access UI at `http://<device-ip>:8501`.

### Local setup (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD '.playwright-browsers'
.\.venv\Scripts\python -m playwright install --only-shell chromium
.\.venv\Scripts\python -m streamlit run main.py
```
Run `.\.venv\Scripts\python run_worker.py` in a second terminal with the same
`PLAYWRIGHT_BROWSERS_PATH` setting.

To update Docker, rebuild the image and recreate the container using the existing
data volume. Stop the old container before starting the replacement.

### Accounts

**YouTube**

1. Create a Google Cloud Project -> Enable **YouTube Data API v3**.
2. Create Credentials -> **OAuth Client ID** -> **Desktop App**. Download JSON.
3. In Scheduler UI -> Accounts: Paste JSON -> Save.
4. Click **"Open Google OAuth"**. Sign in.
5. **IMPORTANT:** You MUST check the box: **"Manage your YouTube videos"**.
6. Copy the code (from the generic localhost page), paste it into the UI, and finish.

**Instagram**

- **Session:** Paste your `sessionid` cookie for stability.
- **Credentials:** Enter Username/Password in the fallback form. The system uses these to **auto-relogin** and retry uploads if your session cookie expires.

**TikTok**

- Paste your `sessionid` cookie (from browser DevTools Application tab).
- Supports URL-encoded values. Use the "Verify" button to ensure it works.

### Troubleshooting

- **Needs review:** Check whether the post appeared before using Force to retry.
- **YouTube 403 Error:** You didn't check the "Manage your YouTube videos" box during auth. Disconnect and re-link.
- **Instagram Login Required:** Your session expired. Ensure Username/Password are saved so the worker can auto-refresh the session.
- **Logs:** Check `data/logs/scheduler.log` in the UI Logs tab for details.
