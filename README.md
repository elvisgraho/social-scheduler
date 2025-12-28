## Social Scheduler (Pi-ready)

Upload short videos once and auto-post them to YouTube Shorts, Instagram Reels, and TikTok on a shared schedule. Built for Raspberry Pi 8GB (Docker/Linux) with robust auto-recovery and a mobile-friendly UI.

### What you need
- **System:** Raspberry Pi (or Linux/Mac) with Docker.
- **Dependencies:** Chromium, Chromedriver, and **FFmpeg** (included in Dockerfile).
- **Accounts:**
  - **YouTube:** Google Cloud Project with "YouTube Data API v3" and a **Desktop** OAuth Client.
  - **Instagram:** Session ID (preferred) + Username/Password (for auto-recovery).
  - **TikTok:** Valid `sessionid` cookie from a logged-in browser.

### Quick start (Docker)
```bash
# Build
docker build -t social-scheduler .

# Run (Linux/Mac)
docker run -d --name scheduler \
  -p 8501:8501 \
  -v $(pwd)/data:/app/data \
  --restart unless-stopped \
  social-scheduler

# Run (Windows PowerShell)
docker run -d --name scheduler `
  -p 8501:8501 `
  -v ${PWD}/data:/app/data `
  --restart unless-stopped `
  social-scheduler
```
Access UI at `http://<device-ip>:8501`.

### Account Configuration
**1. YouTube (Critical Step)**
1. Create a Google Cloud Project -> Enable **YouTube Data API v3**.
2. Create Credentials -> **OAuth Client ID** -> **Desktop App**. Download JSON.
3. In Scheduler UI -> Accounts: Paste JSON -> Save.
4. Click **"Open Google OAuth"**. Sign in.
5. **IMPORTANT:** You MUST check the box: **"Manage your YouTube videos"**.
6. Copy the code (from the generic localhost page), paste it into the UI, and finish.

**2. Instagram**
*   **Session:** Paste your `sessionid` cookie for stability.
*   **Credentials:** Enter Username/Password in the fallback form. The system uses these to **auto-relogin** and retry uploads if your session cookie expires.

**3. TikTok**
*   Paste your `sessionid` cookie (from browser DevTools Application tab).
*   Supports URL-encoded values. Use the "Verify" button to ensure it works.

### Features
*   **Smart Queue:** Upload multiple files at once. The scheduler assigns slots based on your settings.
*   **Resilience:** Failed uploads auto-retry. If the container crashes, stuck tasks reset automatically on restart.
*   **Management:** Pause/Resume queue, "Force Upload Now", or reschedule individual items.
*   **Maintenance:** "Clean oldest uploads" frees up disk space by deleting source files of completed posts.

### Troubleshooting
*   **YouTube 403 Error:** You didn't check the "Manage your YouTube videos" box during auth. Disconnect and re-link.
*   **Instagram Login Required:** Your session expired. Ensure Username/Password are saved so the worker can auto-refresh the session.
*   **Logs:** Check `data/logs/scheduler.log` in the UI Logs tab for details.