import json
import streamlit as st
import pytz
from pathlib import Path
from urllib.parse import unquote

# --- Configuration & Init ---
st.set_page_config(
    page_title="Social Scheduler",
    page_icon="⚙",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# Load custom CSS
try:
    with open("assets/style.css", "r") as f:
        st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
except Exception as e:
    pass

from src.auth_utils import (
    finish_google_auth,
    get_google_auth_url,
    get_google_client_config,
    has_google_client_config,
    save_google_client_config,
    verify_youtube_credentials,
    youtube_connected,
)
from src.database import (
    clear_platform_status,
    delete_from_queue,
    get_config,
    get_queue,
    get_uploaded_count,
    get_uploaded_items,
    init_db,
    reschedule_queue_item,
    set_config,
    set_account_state,
    export_config,
    import_config,
    cleanup_uploaded
)
from src.logging_utils import get_log_file_path, init_logging, tail_log, log_once
from src.notifier import send_telegram_message, telegram_enabled
from src.platform_registry import all_platform_statuses, get_platforms
from src.platforms import tiktok as tiktok_platform
from src.platforms import instagram as instagram_platform
from src.scheduling import get_schedule, human_readable_schedule, next_slots, next_daily_slots, save_schedule
from src import ui_logic
from src.log_display import parse_log_data

# Initialize
init_db()
logger = init_logging("ui")
log_once(logger, "ui_started", "Streamlit UI started.")

# Constants
DATA_DIR = Path("data")
UPLOAD_DIR = Path("data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
YOUTUBE_KEY = "youtube_credentials"
FORCE_KEY = "queue_force_run"
FORCE_PLATFORM_KEY = "queue_force_platform"


# --- UI Components ---

def render_header():
    """Render minimal header."""
    st.markdown("""
    <div style="text-align: center; padding: 0.5rem 0 0 0;">
        <h1 style="margin: 0; font-size: 1.25rem;">Social Scheduler</h1>
    </div>
    """, unsafe_allow_html=True)


def render_platform_status():
    """Render platform connections - minimal row."""
    st.markdown("### **Platforms**")
    
    statuses = all_platform_statuses()
    registry = get_platforms()
    
    # Simple row of badges - uniform styled containers
    cols = st.columns(3)
    
    for idx, (key, cfg) in enumerate(registry.items()):
        with cols[idx]:
            state = statuses.get(key, {})
            live_connected = cfg["connected"]()
            state_connected = bool(state.get("connected"))
            connected = live_connected and state_connected
            
            # Use uniform HTML containers
            if connected:
                st.markdown(f'''
                <div class="platform-status connected">
                    <div class="platform-name">{cfg['label']}</div>
                    <div class="platform-state">Connected</div>
                </div>
                ''', unsafe_allow_html=True)
            else:
                st.markdown(f'''
                <div class="platform-status disconnected">
                    <div class="platform-name">{cfg['label']}</div>
                    <div class="platform-state">Not Connected</div>
                </div>
                ''', unsafe_allow_html=True)
    
    # Refresh below
    st.markdown('<div class="refresh-btn">', unsafe_allow_html=True)
    if st.button("Refresh Status", key="refresh_status", type="secondary"):
        with st.spinner("Checking..."):
            refresh_platform_statuses()
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)


def refresh_platform_statuses():
    """Check platform connectivity."""
    try:
        verify_youtube_credentials(probe_api=True)
    except:
        pass
    try:
        instagram_platform.verify_login()
    except:
        pass
    try:
        tiktok_platform.verify_session(force=True)
    except:
        pass


def render_dashboard_tab(queue_rows, uploaded_count: int):
    """Render minimal dashboard."""
    # Metrics - Simple row
    pending = sum(1 for row in queue_rows if row["status"] in ("pending", "retry"))
    processing = sum(1 for row in queue_rows if row["status"] == "processing")
    paused = bool(int(get_config("queue_paused", 0) or 0))
    
    # Simple metric row
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Pending", pending)
    with c2:
        st.metric("Processing", processing)
    with c3:
        st.metric("Done", uploaded_count)
    with c4:
        st.metric("Status", "Paused" if paused else "Running")
    
    # Pause toggle
    pause_toggle = st.toggle("Pause Uploads", value=paused, key="pause_toggle")
    if pause_toggle != paused:
        set_config("queue_paused", int(pause_toggle))
        st.rerun()
    
    # Schedule - Simple
    st.markdown("### **Schedule**")
    st.info(human_readable_schedule())
    
    if paused:
        st.warning("Queue paused")
    else:
        next_up = next(
            (row for row in queue_rows if row.get("scheduled_for") and row["status"] in ("pending", "retry")),
            None,
        )
        if next_up:
            st.success(f"Next: #{next_up['id']} at {ui_logic.format_datetime_for_ui(next_up['scheduled_for'])}")
        else:
            upcoming = next_slots(1)
            if upcoming:
                st.info(f"Next slot: {upcoming[0].strftime('%b %d %H:%M')}")
            else:
                st.error("No schedule slots!")
    
    # Storage - Simple bar
    st.markdown("### **Storage**")
    used_gb, free_gb, total_gb, percent = ui_logic.get_storage_summary(DATA_DIR)
    if total_gb is not None:
        st.progress(percent / 100.0)
        st.caption(f"Used {used_gb:.1f}GB / {total_gb:.1f}GB")
        if st.button("Clean Old Uploads", key="clean_uploaded_btn"):
            deleted, freed = cleanup_uploaded(20)
            if deleted:
                st.success(f"Deleted {deleted}, freed {freed/ (1024**2):.1f} MB")
                st.rerun()


def _parse_platform_logs(log_value) -> dict:
    """Parse platform_logs from string or dict."""
    if not log_value:
        return {}
    if isinstance(log_value, dict):
        return log_value
    if isinstance(log_value, str):
        try:
            return json.loads(log_value)
        except json.JSONDecodeError:
            return {}
    return {}


def render_platform_status_row(row_id: int, platform_key: str, label: str, log_value, file_path: str):
    """Render status and force upload button for a single platform."""
    logs = _parse_platform_logs(log_value)
    status_text = logs.get(platform_key, "")
    
    # Determine status
    is_success = "success" in str(status_text).lower() or "uploaded" in str(status_text).lower() or "id:" in str(status_text).lower()
    is_failed = status_text and not is_success
    
    col_status, col_action = st.columns([2, 1])
    
    with col_status:
        if is_success:
            st.success(f"✓ {label}: Success")
        elif is_failed:
            st.error(f"✗ {label}: {status_text[:40]}...")
        else:
            st.info(f"○ {label}: Pending")
    
    with col_action:
        # Only show force button if not already successful
        if not is_success:
            if st.button(f"Force {label}", key=f"force_{row_id}_{platform_key}"):
                # Clear the platform status to allow retry
                cleared = clear_platform_status(row_id, platform_key)
                if cleared:
                    # Set force flag for this platform
                    set_config(FORCE_KEY, 1)
                    set_config(FORCE_PLATFORM_KEY, platform_key)
                    st.success(f"Force {label} queued!")
                    st.rerun()
                else:
                    st.error("Failed to clear platform status")


def render_queue_tab(queue_rows, uploaded_rows):
    """Render upload queue."""
    
    # Quick actions
    has_queue_items = any(row["status"] in ("pending", "retry") for row in queue_rows)
    
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Shuffle Queue", key="shuffle_queue_btn", disabled=not has_queue_items):
            shuffled, _ = ui_logic.shuffle_queue(queue_rows)
            st.success(f"Shuffled {shuffled} items!")
            st.rerun()
    with c2:
        if st.button("Delete Next", key="delete_next_btn", type="secondary", disabled=not bool(queue_rows)):
            next_item = next((row for row in queue_rows if row.get("status") in ("pending", "retry", "failed")), None)
            if next_item:
                delete_from_queue(next_item["id"])
                fp = Path(next_item["file_path"])
                if fp.exists():
                    fp.unlink(missing_ok=True)
                st.success(f"Removed #{next_item['id']}")
                st.rerun()
    
    # Upload section
    st.markdown("### **Upload**")
    uploaded_files = st.file_uploader(
        "Drop videos (mp4, mov)",
        type=["mp4", "mov", "m4v"],
        accept_multiple_files=True
    )
    
    if uploaded_files:
        start_dt = ui_logic.get_schedule_start_time(queue_rows)
        occupied = ui_logic.occupied_schedule_dates(queue_rows)
        slots = next_daily_slots(len(uploaded_files), start=start_dt, occupied_dates=occupied)
        
        if len(slots) < len(uploaded_files):
            st.error(f"Not enough slots")
        else:
            st.markdown(f"**{len(uploaded_files)} videos** scheduled")
            
            # Preview first 2
            for uploaded in uploaded_files[:2]:
                st.video(uploaded)
            
            # View schedule
            with st.expander("Schedule"):
                st.write("\n".join(s.l.strftime("%b %d %H:%M") for s in slots))
            
            if st.button(f"Queue {len(uploaded_files)} Videos", key="queue_videos_btn", type="primary"):
                sig = tuple((f.name, getattr(f, "size", None)) for f in uploaded_files)
                if st.session_state.get("queued_sig") != sig:
                    count = ui_logic.save_files_to_queue(uploaded_files, slots, UPLOAD_DIR, shuffle_order=False)
                    if count > 0:
                        st.session_state["queued_sig"] = sig
                        st.success(f"Queued {count} videos!")
                        st.rerun()
    else:
        st.session_state.pop("queued_sig", None)
    
    # Queue list
    st.markdown("### **Queue**")
    
    if queue_rows:
        platforms = get_platforms()
        
        for row in queue_rows:
            status_icons = {"pending": "Pending", "processing": "Processing", "uploaded": "Done", "failed": "Failed", "retry": "Retry"}
            icon = status_icons.get(row['status'], row['status'].title())
            
            with st.expander(f"{icon} #{row['id']} - {Path(row['file_path']).name[:25]}"):
                col_info, col_vid = st.columns([1, 1])
                
                with col_info:
                    st.write(f"**{ui_logic.format_datetime_for_ui(row.get('scheduled_for'))}**")
                    st.write(f"Status: {row['status']}")
                    if row.get("last_error"):
                        st.error(row['last_error'][:50])
                    
                    # Platform status section
                    st.markdown("**Platforms:**")
                    for pkey, pcfg in platforms.items():
                        render_platform_status_row(
                            row["id"], 
                            pkey, 
                            pcfg["label"], 
                            row.get("platform_logs"),
                            row["file_path"]
                        )
                    
                    st.markdown("---")
                    ac1, ac2 = st.columns(2)
                    if ac1.button("Delete", key=f"del_{row['id']}"):
                        delete_from_queue(row["id"])
                        fp = Path(row["file_path"])
                        if fp.exists():
                            fp.unlink(missing_ok=True)
                        st.rerun()
                    if ac2.button("Reschedule", key=f"rsc_{row['id']}"):
                        anchor = ui_logic.parse_iso(row.get("scheduled_for")) or ui_logic.get_schedule_start_time(queue_rows)
                        occupied = ui_logic.occupied_schedule_dates(queue_rows)
                        curr_dt = ui_logic.parse_iso(row.get("scheduled_for"))
                        if curr_dt:
                            occupied.discard(curr_dt.date().isoformat())
                        future = next_daily_slots(1, start=anchor, occupied_dates=occupied)
                        if future:
                            reschedule_queue_item(row["id"], future[0].isoformat())
                            st.success("Rescheduled!")
                            st.rerun()
                
                with col_vid:
                    if Path(row["file_path"]).exists():
                        st.video(str(row["file_path"]))
                    else:
                        st.warning("File missing")
    else:
        st.info("No videos in queue")


def render_accounts_tab():
    """Render platform accounts."""
    
    # YouTube
    st.markdown("### **YouTube**")
    google_config_present = has_google_client_config()
    
    with st.expander("OAuth client JSON (Desktop app)", expanded=not google_config_present):
        st.caption(
            "1. Google Cloud Console -> Create OAuth Client ID (Desktop app).\n"
            "2. Download JSON and paste below."
        )
        google_json = st.text_area(
            "Google client JSON",
            value=get_google_client_config(pretty=True) or "",
            height=200,
            key="google_json_input",
        )
        if st.button("Save Google OAuth JSON"):
            if not google_json.strip():
                st.warning("Paste the JSON first.")
            else:
                ok, msg = save_google_client_config(google_json.strip())
                if ok:
                    logger.info("Google OAuth JSON saved (%s bytes).", len(google_json.strip()))
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
                    logger.warning("Failed to save Google OAuth JSON: %s", msg)
        
        yt_connected = youtube_connected()
        if google_config_present:
            if not yt_connected:
                auth_url, err = get_google_auth_url()
                if not err:
                    st.link_button("Connect YouTube", auth_url)
                    yt_code = st.text_input("Auth code", key="yt_code")
                    if st.button("Finish", key="yt_finish_btn", type="primary"):
                        ok, message = finish_google_auth(yt_code.strip())
                        if ok:
                            st.success("Connected!")
                            st.rerun()
                        else:
                            st.error(message)
            else:
                st.success("YouTube connected!")
                c1, c2 = st.columns(2)
                if c1.button("Verify", key="yt_verify_btn"):
                    ok, msg = verify_youtube_credentials(probe_api=False)
                    st.success(msg) if ok else st.error(msg)
                if c2.button("Disconnect", key="yt_disconnect_btn"):
                    set_config(YOUTUBE_KEY, "")
                    set_account_state("youtube", False, "")
                    st.rerun()
    
    # Instagram
    st.markdown("### **Instagram**")
    
    with st.expander("Instagram"):
        st.caption("Paste sessionid cookie")
        ig_session = st.text_area("Session", height=60, key="ig_session")
        c1, c2 = st.columns(2)
        if c1.button("Save", key="ig_save_btn"):
            ok, msg = instagram_platform.save_sessionid(ig_session)
            st.success(msg) if ok else st.error(msg)
            st.rerun()
        if c2.button("Verify", key="ig_verify_btn"):
            ok, msg = instagram_platform.verify_login()
            st.success(msg) if ok else st.error(msg)
    
    # TikTok
    st.markdown("### **TikTok**")
    
    tt_status = tiktok_platform.session_status()
    
    if tt_status["valid"]:
        st.success(f"@{tt_status.get('account_name', 'user')}")
    elif tt_status["sessionid"]:
        st.error(tt_status.get('message', 'Invalid'))
    else:
        st.warning("No session")
    
    with st.form("tiktok_form"):
        st.caption("Paste sessionid")
        tt_input = st.text_area("Session", height=60, key="tt_session")
        if st.form_submit_button("Save", key="tt_save_btn", type="primary"):
            raw_input = tt_input.strip()
            if "%" in raw_input:
                try:
                    raw_input = unquote(raw_input)
                except:
                    pass
            clean_session = ui_logic.extract_tiktok_session(raw_input)
            if clean_session:
                tiktok_platform.save_session(clean_session)
                tiktok_platform.verify_session(force=True)
                st.success("Saved!")
                st.rerun()
            else:
                st.warning("No sessionid found")
    
    c1, c2 = st.columns(2)
    if c1.button("Verify", key="tt_verify_btn"):
        ok, msg = tiktok_platform.verify_session(force=True)
        st.success(msg) if ok else st.error(msg)
        st.rerun()
    if c2.button("Clear", key="tt_clear_btn"):
        tiktok_platform.save_session("")
        st.rerun()


def render_settings_tab():
    """Render settings."""
    
    # Schedule
    st.markdown("### **Schedule**")
    schedule = get_schedule()
    days_map = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
    
    with st.form("schedule_form"):
        selected_days = st.multiselect(
            "Days",
            options=list(days_map.keys()),
            default=[k for k, v in days_map.items() if v in schedule["days"]],
        )
        times_input = st.text_input("Times (HH:MM)", value=", ".join(schedule["times"]))
        
        tz_options = list(pytz.common_timezones)
        curr_tz = schedule["timezone"]
        if curr_tz not in tz_options:
            tz_options.insert(0, curr_tz)
        timezone = st.selectbox("Timezone", tz_options, index=tz_options.index(curr_tz))
        
        if st.form_submit_button("Save", key="schedule_save_btn", type="primary"):
            p_times = [t.strip() for t in times_input.split(",") if t.strip()]
            p_days = [days_map[d] for d in selected_days]
            save_schedule(p_days, p_times, timezone)
            st.success("Saved!")
            st.rerun()
    
    # Metadata
    st.markdown("### **Defaults**")
    with st.form("meta_form"):
        title = st.text_input("Title", value=get_config("global_title", "Daily Short #{num}"))
        desc = st.text_area("Caption/Hashtags", value=get_config("global_desc", "#shorts #viral"), height=60)
        if st.form_submit_button("Save", key="meta_save_btn"):
            set_config("global_title", title)
            set_config("global_desc", desc)
            st.success("Saved!")
    
    # Telegram
    st.markdown("### **Telegram**")
    with st.form("telegram_form"):
        bot_token = st.text_input("Bot Token", type="password")
        chat_id = st.text_input("Chat ID")
        if st.form_submit_button("Save", key="telegram_save_btn"):
            set_config("telegram_bot_token", bot_token)
            set_config("telegram_chat_id", chat_id)
            st.success("Saved!")
    
    if telegram_enabled() and st.button("Test", key="telegram_test_btn"):
        send_telegram_message("Test!")
        st.success("Sent!")
    
    # Backup
    st.markdown("### **Backup**")
    backup_payload = json.dumps(export_config(), indent=2)
    st.download_button("Download", key="backup_download_btn", data=backup_payload, file_name="backup.json", mime="application/json")
    
    with st.expander("Restore"):
        raw = st.text_area("Paste backup", height=80)
        if st.button("Restore", key="restore_backup_btn"):
            try:
                payload = json.loads(raw)
                s, a = import_config(payload)
                st.success(f"Restored {s} settings, {a} accounts")
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")


def render_logs_tab():
    """Render logs with professional display."""
    st.markdown("### **Logs**")
    
    log_path = get_log_file_path()
    
    # Line count selector
    line_count = st.selectbox(
        "Lines",
        options=[20, 50, 100],
        index=0,
        key="log_lines_select"
    )
    
    # Filter input on separate row
    filter_text = st.text_input("Filter", key="log_filter", placeholder="Search...")
    
    # Action buttons on separate row
    c_refresh, c_clear = st.columns(2)
    with c_refresh:
        if st.button("⟳ Refresh", key="logs_refresh_btn"):
            st.rerun()
    with c_clear:
        if st.button("✕ Clear", key="logs_clear_btn"):
            if log_path.exists():
                log_path.write_text("")
                st.success("Cleared!")
                st.rerun()
    
    # Load logs
    raw_log = tail_log(line_count) if line_count else (log_path.read_text(encoding="utf-8", errors="ignore") if log_path.exists() else "")
    
    # Apply filter
    if filter_text.strip():
        lines = [l for l in raw_log.splitlines() if filter_text.lower() in l.lower()]
        display_lines = lines
    else:
        display_lines = raw_log.splitlines()
    
    # Parse log lines for display
    log_data = parse_log_data(display_lines)
    
    # Professional log container
    status_class = 'live' if log_path.exists() and log_path.stat().st_size > 0 else 'empty'
    
    st.markdown(f"""
    <div style="background:var(--bg-input);border:1px solid var(--border);border-radius:var(--radius-sm);margin-bottom:1rem;overflow:hidden;">
        <div style="background:var(--bg-card);padding:0.6rem 0.75rem;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;font-size:0.75rem;font-weight:600;color:var(--text-secondary);text-transform:uppercase;letter-spacing:0.5px;">
            <span>Log Viewer</span>
            <span style="font-size:0.75rem;color:var(--text-secondary);">{len(display_lines)} lines</span>
        </div>
        <div style="max-height:400px;overflow-y:auto;">
            {''.join(log_data)}
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    # Download button
    if log_path.exists():
        st.download_button(
            "Download Logs", 
            key="logs_download_btn", 
            data=log_path.read_text(), 
            file_name="logs.txt", 
            mime="text/plain"
        )



# --- Main ---

render_header()

# Load data
queue_data = get_queue()
uploaded_count = get_uploaded_count()
uploaded_rows = get_uploaded_items(200)

# Platform status
render_platform_status()

# Tabs
tabs = st.tabs(["Dashboard", "Queue", "Accounts", "Settings", "Logs"])

with tabs[0]:
    render_dashboard_tab(queue_data, uploaded_count)

with tabs[1]:
    render_queue_tab(queue_data, uploaded_rows)

with tabs[2]:
    render_accounts_tab()

with tabs[3]:
    render_settings_tab()

with tabs[4]:
    render_logs_tab()

# Footer
st.caption("Social Scheduler  |  Raspberry Pi 5")
