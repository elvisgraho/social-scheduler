import json
import streamlit as st
import pytz
from pathlib import Path
from urllib.parse import unquote  # Fix for encoded cookies

# --- Configuration & Init ---
st.set_page_config(page_title="Social Scheduler", page_icon="SS", layout="centered")

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

# Import the logic module
from src import ui_logic

# Initialize
init_db()
logger = init_logging("ui")
log_once(logger, "ui_started", "Streamlit UI started.")

# Constants
DATA_DIR = Path("data")
UPLOAD_DIR = Path("data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
YOUTUBE_KEY = "youtube_credentials"

logger.debug("Uploads directory ready at %s", UPLOAD_DIR)


# --- UI Components ---

def refresh_platform_statuses():
    """
    Actively re-check platform connectivity so the badge row reflects current reality.
    """
    results = []
    try:
        yt_ok, yt_msg = verify_youtube_credentials(probe_api=True)
        results.append(("YouTube", yt_ok, yt_msg))
    except Exception as e:
        results.append(("YouTube", False, str(e)))

    try:
        ig_ok, ig_msg = instagram_platform.verify_login()
        results.append(("Instagram", ig_ok, ig_msg))
    except Exception as e:
        results.append(("Instagram", False, str(e)))

    try:
        tt_ok, tt_msg = tiktok_platform.verify_session(force=True)
        results.append(("TikTok", tt_ok, tt_msg))
    except Exception as e:
        results.append(("TikTok", False, str(e)))

    return results


def render_platform_status_badge():
    """Renders the top status row for platforms."""
    st.caption("Connections")
    refresh_col, _ = st.columns([1, 3])
    if refresh_col.button("Refresh statuses"):
        with st.spinner("Checking connections..."):
            st.session_state["status_results"] = refresh_platform_statuses()
        st.rerun()

    statuses = all_platform_statuses()
    registry = get_platforms()
    cols = st.columns(len(registry))
    
    for col, (key, cfg) in zip(cols, registry.items()):
        state = statuses.get(key, {})
        # Consider both the live check and the persisted account_state flag to avoid stale "Connected" labels.
        live_connected = cfg["connected"]()
        state_connected = bool(state.get("connected"))
        connected = live_connected and state_connected
        label = cfg["label"]
        
        if connected:
            col.success(f"{label}\nConnected")
        else:
            col.warning(f"{label}\nNot linked")
            
        error = state.get("last_error")
        if error:
            col.caption(f"Error: {error}")

    # If we just ran a refresh, surface the results inline
    recent = st.session_state.pop("status_results", None)
    if recent:
        for name, ok, message in recent:
            if ok:
                st.success(f"{name}: OK")
            else:
                st.warning(f"{name}: {message}")

def render_dashboard_tab(queue_rows, uploaded_count: int):
    st.subheader("Status Overview")
    
    try:
        pending = sum(1 for row in queue_rows if row["status"] in ("pending", "retry"))
        processing = sum(1 for row in queue_rows if row["status"] == "processing")
        uploaded = uploaded_count
    except Exception:
        pending, processing, uploaded = 0, 0, uploaded_count
    
    # Metrics
    metrics = st.columns(4)
    metrics[0].metric("Pending", pending)
    metrics[1].metric("Processing", processing)
    metrics[2].metric("Done (recent)", uploaded)
    
    paused = bool(int(get_config("queue_paused", 0) or 0))
    metrics[3].metric("Queue", "Paused" if paused else "Live")

    st.write(f"**Schedule:** {human_readable_schedule()}")

    # Storage
    used_gb, free_gb, total_gb, percent = ui_logic.get_storage_summary(DATA_DIR)
    if total_gb is not None:
        st.progress(percent / 100.0, text=f"Storage used: {used_gb:.1f} / {total_gb:.1f} GB ({percent:.1f}%)")
        storage_cols = st.columns(2)
        storage_cols[0].caption(f"Free: {free_gb:.1f} GB")
        with storage_cols[1]:
            if st.button("Clean oldest uploads", key="clean_uploaded"):
                deleted, freed = cleanup_uploaded(20)
                logger.info("Cleanup triggered via UI. Removed %s items, freed %s bytes.", deleted, freed)
                if deleted:
                    st.success(f"Deleted {deleted} uploaded items, freed {freed/ (1024**2):.1f} MB.")
                    st.rerun()
                else:
                    st.info("No uploaded items eligible for cleanup.")

    # Next Up Info
    if paused:
        st.info("Queue is paused. Resume uploads to continue scheduling.")
    else:
        next_up = next(
            (row for row in queue_rows if row.get("scheduled_for") and row["status"] in ("pending", "retry")),
            None,
        )
        if next_up:
            st.info(f"Next upload #{next_up['id']} at {ui_logic.format_datetime_for_ui(next_up['scheduled_for'])}")
        else:
            upcoming = next_slots(1)
            if upcoming:
                st.info(f"No videos waiting. Next available slot: {upcoming[0].strftime('%b %d %H:%M %Z')}")
            else:
                st.warning("No valid schedule slots configured. Please check Settings.")

    failed_rows = [row for row in queue_rows if row.get("status") == "failed"]
    if failed_rows:
        st.markdown("### Recent failures")
        for row in failed_rows[:5]:
            msg = row.get("last_error") or "Unknown error"
            st.warning(f"#{row['id']} on {ui_logic.format_datetime_for_ui(row.get('scheduled_for') or '')}: {msg}")

def render_queue_tab(queue_rows, uploaded_rows):
    st.subheader("Upload & Queue")

    # Display Notice (persisted across reruns)
    notice = st.session_state.pop("queue_notice", None)
    if isinstance(notice, dict):
        level = notice.get("level", "info")
        text = notice.get("text", "")
        if text:
            if level == "success":
                st.success(text)
            elif level == "warning":
                st.warning(text)
            else:
                st.info(text)
    
    paused = bool(int(get_config("queue_paused", 0) or 0))
    has_queue_items = any(row["status"] in ("pending", "retry") for row in queue_rows)
    
    if not has_queue_items:
        st.session_state["force_now_confirmed"] = False

    # --- Worker Controls ---
    st.markdown("#### Worker controls")
    worker_cols = st.columns([1, 1, 1])

    # Pause Toggle
    pause_toggle = worker_cols[0].toggle(
        "Pause uploads",
        value=paused,
        help="When on, the worker will not post until you turn it off.",
        key="pause_toggle",
    )
    if pause_toggle != paused:
        set_config("queue_paused", int(pause_toggle))
        if pause_toggle:
            logger.warning("Queue paused via UI.")
            st.session_state["queue_notice"] = {"level": "warning", "text": "Uploads paused."}
        else:
            rescheduled, first_slot = ui_logic.reschedule_pending_items(queue_rows)
            logger.info("Queue resumed via UI.")
            message = "Uploads resumed."
            if rescheduled:
                formatted = first_slot.strftime("%b %d %H:%M %Z") if first_slot else "next available slot"
                message = f"Uploads resumed. Rescheduled {rescheduled} item(s), next at {formatted}."
            st.session_state["queue_notice"] = {"level": "success", "text": message}
        st.rerun()

    # Shuffle queue
    if worker_cols[1].button(
        "Shuffle queue order",
        help="Randomize scheduled dates for pending/retry items starting from the next available slot."
    ):
        shuffled, first_slot = ui_logic.shuffle_queue(queue_rows)
        if shuffled:
            formatted = ui_logic.format_datetime_for_ui(first_slot.isoformat()) if first_slot else "next slot"
            st.session_state["queue_notice"] = {"level": "success", "text": f"Shuffled {shuffled} items. Next slot: {formatted}."}
        else:
            st.session_state["queue_notice"] = {"level": "info", "text": "No pending items to shuffle."}
        st.rerun()

    # Delete-next as a quick cleanup action
    delete_next = worker_cols[2].button(
        "Delete next in queue",
        help="Remove the next queued item and its file.",
        type="secondary",
        disabled=not bool(queue_rows),
        key="delete_next_btn",
        use_container_width=True,
    )

    if delete_next:
        next_item = next(
            (row for row in queue_rows if row.get("status") in ("pending", "retry", "failed")),
            None,
        )
        if not next_item:
            st.warning("No queued videos to delete.")
        else:
            delete_from_queue(next_item["id"])
            fp = Path(next_item["file_path"])
            if fp.exists():
                fp.unlink(missing_ok=True)
            logger.info("Deleted next queue item %s (%s).", next_item["id"], next_item["file_path"])
            st.session_state["queue_notice"] = {"level": "success", "text": f"Removed #{next_item['id']} from queue."}
            st.session_state["force_now_confirmed"] = False
            st.rerun()

    # --- Immediate actions ---
    st.markdown("#### Immediate actions")
    action_col1, action_col2 = st.columns([1.2, 1])

    force_now = action_col1.button(
        "Upload next now",
        help="Process the next queued item immediately.",
        type="primary",
        disabled=not has_queue_items,
        key="force_next_btn",
        use_container_width=True,
    )

    # Force-to-platform is separated for clarity
    platforms = get_platforms()
    platform_keys = list(platforms.keys())
    platform_labels = {k: cfg["label"] for k, cfg in platforms.items()}

    with action_col2:
        if platform_keys:
            selected_platform = st.selectbox(
                "Force to platform",
                options=platform_keys,
                format_func=lambda k: platform_labels.get(k, k.title()),
                key="force_platform_select",
                help="Immediately push the next queued item to one platform only.",
            )
        else:
            selected_platform = None
            st.warning("No platforms available.")

        force_platform_btn = st.button(
            "Send next to platform",
            key="force_platform_btn",
            help="Trigger an immediate run of the next queued item to the selected platform only.",
            disabled=not (has_queue_items and selected_platform),
            use_container_width=True,
        )

    if force_now:
        if not has_queue_items:
            st.warning("No queued videos to upload.")
        elif st.session_state.get("force_now_confirmed"):
            set_config("queue_force_platform", "")
            set_config("queue_force_run", 1)
            logger.info("Force upload requested via UI.")
            st.session_state["queue_notice"] = {"level": "success", "text": "Next item will be processed immediately."}
            st.session_state["force_now_confirmed"] = False
            st.rerun()
        else:
            st.warning("Click again to confirm immediate upload.")
            st.session_state["force_now_confirmed"] = True

    if force_platform_btn and selected_platform:
        if not has_queue_items:
            st.warning("No queued videos to upload.")
        elif not platforms[selected_platform]["connected"]():
            st.warning(f"{platform_labels[selected_platform]} is not connected.")
        else:
            set_config("queue_force_platform", selected_platform)
            set_config("queue_force_run", 1)
            logger.info("Force upload requested for %s only.", selected_platform)
            st.session_state["queue_notice"] = {
                "level": "success",
                "text": f"Forcing the next queued item to {platform_labels[selected_platform]} now.",
            }
            st.session_state["force_now_confirmed"] = False
            st.rerun()

    # --- Add videos ---
    st.markdown("#### Add videos to schedule")
    uploaded_files = st.file_uploader(
        "Drop multiple shorts (mp4/mov)", type=["mp4", "mov", "m4v"], accept_multiple_files=True
    )
    st.caption("Tip: Upload 7-10 at once with one shared title/description.")

    def _uploads_signature(files) -> tuple:
        return tuple((f.name, getattr(f, "size", None)) for f in files)

    # -- Upload Logic --
    if uploaded_files:
        start_dt = ui_logic.get_schedule_start_time(queue_rows)
        occupied = ui_logic.occupied_schedule_dates(queue_rows)
        slots = next_daily_slots(len(uploaded_files), start=start_dt, occupied_dates=occupied)
        
        if len(slots) < len(uploaded_files):
            st.error(f"Not enough schedule slots available in the next 90 days. Needed {len(uploaded_files)}, found {len(slots)}.")
        else:
            readable_slots = "\n".join(slot.strftime("%b %d %H:%M %Z") for slot in slots)
            st.write("Videos will be scheduled for:")
            st.code(readable_slots)
            
            preview_cols = st.columns(min(3, len(uploaded_files)))
            for idx, uploaded in enumerate(uploaded_files[:3]):
                preview_cols[idx].video(uploaded)

            sig = _uploads_signature(uploaded_files)
            # If signature changed, process new files
            if st.session_state.get("queued_sig") != sig:
                logger.info("Queuing %s new videos.", len(uploaded_files))
                count = ui_logic.save_files_to_queue(uploaded_files, slots, UPLOAD_DIR, shuffle_order=False)
                
                if count > 0:
                    st.session_state["queued_sig"] = sig
                    # FIX: Use session state for notice to persist across rerun
                    st.session_state["queue_notice"] = {
                        "level": "success", 
                        "text": f"Automatically queued {count} videos."
                    }
                    st.rerun()
                else:
                    st.error("Failed to queue videos. Check logs.")
            else:
                st.info("Uploads already added to the queue.")
    else:
        # Clear signature if user cleared files
        st.session_state.pop("queued_sig", None)

    # -- Queue Table --
    st.markdown("### Queue")
    df = ui_logic.format_queue_dataframe(queue_rows)
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "Last Error": st.column_config.TextColumn("Last Error", width="large")
        },
    )

    # Recent uploads
    st.markdown("### Recent uploads")
    if uploaded_rows:
        uploaded_df = []
        for row in uploaded_rows:
            uploaded_df.append({
                "Upload ID": row.get("id"),
                "Queue ID": row.get("queue_id"),
                "File": Path(row.get("file_path") or "").name,
                "Uploaded": ui_logic.format_uploaded_time(row.get("uploaded_at")),
            })
        st.dataframe(
            uploaded_df,
            width="stretch",
            hide_index=True,
        )
        with st.expander("Uploaded item details"):
            for row in uploaded_rows[:50]:
                st.write(f"Upload #{row.get('id')} (queue #{row.get('queue_id')}) - {Path(row.get('file_path') or '').name}")
                st.caption(f"Uploaded: {ui_logic.format_uploaded_time(row.get('uploaded_at'))}")
                logs = row.get("platform_logs")
                if logs:
                    try:
                        st.json(json.loads(logs))
                    except Exception:
                        st.text(logs)
                st.markdown("---")
    else:
        st.info("No uploaded items recorded yet.")

    # -- Item Management --
    if queue_rows:
        st.markdown("### Manage items")
        for row in queue_rows:
            label = f"#{row['id']} - {Path(row['file_path']).name}"
            with st.expander(label):
                col_info, col_vid = st.columns([1, 1])
                
                with col_info:
                    st.write(f"**Scheduled:** {ui_logic.format_datetime_for_ui(row.get('scheduled_for'))}")
                    st.write(f"**Status:** {row.get('status')}")
                    st.write(f"**Attempts:** {row.get('attempts', 0)}")
                    if row.get("last_error"):
                        st.error(f"Error: {row['last_error']}")
                        
                    logs = row.get("platform_logs")
                    if logs:
                        try:
                            st.json(json.loads(logs))
                        except Exception:
                            st.text(logs)
                            
                    # Actions
                    b_col1, b_col2 = st.columns(2)
                    if b_col1.button("Delete", key=f"del_{row['id']}"):
                        delete_from_queue(row["id"])
                        fp = Path(row["file_path"])
                        if fp.exists():
                            fp.unlink(missing_ok=True)
                        logger.info("Deleted queue item %s (%s).", row["id"], row["file_path"])
                        st.rerun()
                        
                    if b_col2.button("Reschedule (Next Slot)", key=f"rsc_{row['id']}"):
                        anchor = ui_logic.parse_iso(row.get("scheduled_for")) or ui_logic.get_schedule_start_time(queue_rows)
                        occupied = ui_logic.occupied_schedule_dates(queue_rows)
                        curr_dt = ui_logic.parse_iso(row.get("scheduled_for"))
                        if curr_dt:
                            occupied.discard(curr_dt.date().isoformat())
                        future = next_daily_slots(1, start=anchor, occupied_dates=occupied)
                        if future:
                            reschedule_queue_item(row["id"], future[0].isoformat())
                            logger.info("Rescheduled queue item %s to %s.", row["id"], future[0].isoformat())
                            st.success(f"Moved to {future[0]}")
                            st.rerun()
                        else:
                            st.warning("No future slots available.")
                            logger.warning("Reschedule failed for %s: no slot available.", row["id"])

                with col_vid:
                    if Path(row["file_path"]).exists():
                        st.video(str(row["file_path"]))
                    else:
                        st.warning("File not found on disk.")

def render_accounts_tab():
    st.subheader("Platform Accounts")

    # --- Google/YouTube ---
    st.markdown("#### Google API Setup")
    google_config_present = has_google_client_config()
    
    with st.expander("OAuth client JSON (Desktop app)", expanded=not google_config_present):
        st.caption(
            "1. Google Cloud Console -> Create OAuth Client ID (Desktop app).\n"
            "2. Download JSON and paste below."
        )
        google_json = st.text_area(
            "Google client JSON",
            value=get_google_client_config(pretty=True),
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

    st.markdown("#### YouTube")
    yt_connected = youtube_connected()
    if yt_connected:
        st.success("YouTube linked.")
    else:
        st.warning("YouTube not linked.")
        
    if not google_config_present:
        st.info("Provide Google OAuth JSON above first.")
    else:
        auth_url, err = get_google_auth_url()
        if err:
            st.error(err)
        else:
            if not yt_connected:
                st.link_button("1. Open Google OAuth", auth_url)
                yt_code = st.text_input("2. Paste auth code", key="yt_code")
                if st.button("3. Finish YouTube link"):
                    if not yt_code:
                        st.warning("Enter the auth code.")
                    else:
                        ok, message = finish_google_auth(yt_code.strip())
                        if ok:
                            logger.info("YouTube account linked.")
                            st.success(message)
                            st.rerun()
                        else:
                            st.error(message)
                            logger.warning("YouTube link failed: %s", message)

    if st.button("Verify YouTube token"):
        ok, msg = verify_youtube_credentials(probe_api=False)
        if ok:
            st.success(msg)
            logger.info("YouTube token verification succeeded.")
        else:
            st.error(msg)
            logger.warning("YouTube token verification failed: %s", msg)
            
    if yt_connected and st.button("Disconnect YouTube"):
        set_config(YOUTUBE_KEY, "")
        set_account_state("youtube", False, "Disconnected by user")
        logger.info("YouTube account disconnected by user.")
        st.rerun()

    # --- Instagram ---
    st.markdown("#### Instagram")
    with st.expander("Username/Password (Fallback)", expanded=False):
        with st.form("ig_form"):
            ig_user = st.text_input("Username", value=get_config("insta_user", ""))
            ig_pass = st.text_input("Password", type="password")
            if st.form_submit_button("Save Credentials"):
                set_config("insta_user", ig_user)
                set_config("insta_pass", ig_pass)
                set_account_state("instagram", bool(ig_user and ig_pass), None)
                logger.info("Instagram credentials updated (user=%s).", ig_user)
                st.success("Saved.")

    with st.form("ig_session_form"):
        st.caption("Paste 'sessionid' cookie or Instagrapi JSON.")
        ig_session_raw = st.text_area("Session Data", value=get_config("insta_sessionid", ""), height=100)
        if st.form_submit_button("Save Session"):
            ok, msg = instagram_platform.save_sessionid(ig_session_raw)
            if ok:
                st.success(msg)
            else:
                st.error(msg)
            st.rerun()

    if st.button("Verify Instagram Login"):
        ok, msg = instagram_platform.verify_login()
        if ok:
            st.success(msg)
            logger.info("Instagram verification succeeded.")
        else:
            st.error(msg)
            logger.warning("Instagram verification failed: %s", msg)

    # --- TikTok ---
    st.markdown("#### TikTok")
    tt_status = tiktok_platform.session_status()
    
    if not tt_status["sessionid"]:
        st.warning("No Session.")
    elif not tt_status["valid"]:
        st.error(f"Invalid: {tt_status.get('message')}")
    else:
        st.success(f"Active: @{tt_status.get('account_name', 'user')}")

    with st.form("tt_form"):
        st.caption("Paste `sessionid` cookie value or exported JSON.")
        tt_input = st.text_area("Input", value=tt_status.get("sessionid", ""), height=100)
        if st.form_submit_button("Save TikTok Session"):
            # FIX: Handle URL encoded cookies
            raw_input = tt_input.strip()
            if "%" in raw_input:
                try:
                    raw_input = unquote(raw_input)
                except Exception:
                    pass

            clean_session = ui_logic.extract_tiktok_session(raw_input)
            if not clean_session:
                st.warning("Could not find sessionid in input.")
            else:
                tiktok_platform.save_session(clean_session)
                tiktok_platform.verify_session(force=True)
                logger.info("TikTok session saved (chars=%s).", len(clean_session))
                st.success("Saved and verified.")
                st.rerun()

    c1, c2 = st.columns(2)
    if c1.button("Verify TikTok Now"):
        ok, msg = tiktok_platform.verify_session(force=True)
        if ok:
            st.success(msg)
            logger.info("Manual TikTok verification -> %s", msg)
        else:
            st.error(msg)
            logger.warning("Manual TikTok verification failed: %s", msg)
        st.rerun()
        
    if c2.button("Clear TikTok"):
        tiktok_platform.save_session("")
        logger.warning("TikTok session cleared by user.")
        st.rerun()

def render_settings_tab():
    st.subheader("Global Settings")

    # Schedule
    st.markdown("### Publishing Schedule")
    schedule = get_schedule()
    days_map = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
    
    with st.form("schedule_form"):
        selected_days = st.multiselect(
            "Days",
            options=list(days_map.keys()),
            default=[k for k, v in days_map.items() if v in schedule["days"]],
        )
        times_input = st.text_input("Times (HH:MM, comma sep)", value=", ".join(schedule["times"]))
        
        # Timezone selection
        tz_options = list(pytz.common_timezones)
        curr_tz = schedule["timezone"]
        if curr_tz not in tz_options:
            tz_options.insert(0, curr_tz)
        timezone = st.selectbox("Timezone", tz_options, index=tz_options.index(curr_tz))
        
        if st.form_submit_button("Save Schedule"):
            p_times = [t.strip() for t in times_input.split(",") if t.strip()]
            p_days = [days_map[d] for d in selected_days]
            save_schedule(p_days, p_times, timezone)
            logger.info("Schedule updated: days=%s, times=%s, timezone=%s", p_days, p_times, timezone)
            st.success("Schedule updated.")
            st.rerun()

    # Metadata
    st.markdown("### Default Metadata")
    with st.form("meta_form"):
        title = st.text_input("Title", value=get_config("global_title", ""))
        desc = st.text_area("Caption / Hashtags", value=get_config("global_desc", ""), height=150)
        if st.form_submit_button("Save Metadata"):
            set_config("global_title", title)
            set_config("global_desc", desc)
            logger.info("Global metadata updated (title_len=%s).", len(title))
            st.success("Metadata saved.")

    # Telegram
    st.markdown("### Notifications")
    with st.form("telegram_form"):
        bot_token = st.text_input("Telegram Bot Token", value=get_config("telegram_bot_token", ""), type="password")
        chat_id = st.text_input("Chat ID", value=get_config("telegram_chat_id", ""))
        if st.form_submit_button("Save Telegram"):
            set_config("telegram_bot_token", bot_token)
            set_config("telegram_chat_id", chat_id)
            logger.info("Telegram settings updated.")
            st.success("Telegram config saved.")
            
    if telegram_enabled() and st.button("Send Test Notification"):
        send_telegram_message("Test from Social Scheduler UI.")
        logger.info("Test Telegram notification dispatched.")
        st.success("Sent.")

    st.markdown("### Backup & Restore")
    backup_payload = json.dumps(export_config(), indent=2)
    st.download_button(
        "Download config backup",
        data=backup_payload,
        file_name="scheduler-config-backup.json",
        mime="application/json",
        help="Includes settings and platform state (not video files).",
    )

    with st.expander("Restore from backup", expanded=False):
        raw_backup = st.text_area("Paste backup JSON", height=200)
        if st.button("Restore backup"):
            if not raw_backup.strip():
                st.warning("Paste a backup first.")
            else:
                try:
                    payload = json.loads(raw_backup)
                    settings_count, accounts_count = import_config(payload)
                    logger.info("Backup restored (settings=%s, accounts=%s).", settings_count, accounts_count)
                    st.success(f"Restored {settings_count} settings and {accounts_count} account states. Please refresh.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Restore failed: {exc}")
                    logger.error("Backup restore error: %s", exc)

def render_logs_tab():
    st.subheader("System Logs")
    log_path = get_log_file_path()
    
    col1, col2, col3, col4 = st.columns([1.2, 1, 1, 2])
    view_choice = col1.selectbox(
        "View window",
        options=[("Last 20 lines", 20), ("Last 120 lines", 120), ("Last 1000 lines", 1000), ("Entire file", None)],
        format_func=lambda opt: opt[0],
        index=0,
        key="log_view_window",
        help="Choose how much of the log to display.",
    )
    if col2.button("Refresh"):
        st.rerun()
    if col3.button("Clear Log", help="Empty the current log file."):
        if log_path.exists():
            log_path.write_text("", encoding="utf-8")
            st.success("Log file cleared.")
            st.rerun()
        else:
            st.info("Log file not found.")
    filter_text = col4.text_input(
        "Filter (optional)",
        value="",
        placeholder="Search text, e.g. ERROR or TikTok",
        key="log_filter_text",
    )
        
    # Load log content based on selection
    if view_choice[1] is None and log_path.exists():
        raw_log = log_path.read_text(encoding="utf-8", errors="ignore")
    else:
        raw_log = tail_log(view_choice[1] or 200)

    # Apply simple filter if provided
    if filter_text.strip():
        filtered_lines = [line for line in raw_log.splitlines() if filter_text.lower() in line.lower()]
        display_log = "\n".join(filtered_lines) if filtered_lines else "No log lines match that filter."
    else:
        display_log = raw_log

    st.caption(f"Showing {view_choice[0]}")
    st.code(display_log, language="text")
    
    if log_path.exists():
        st.download_button(
            "Download Log",
            data=log_path.read_text(encoding="utf-8", errors="ignore"),
            file_name="scheduler.log",
            mime="text/plain",
        )

# --- Main Render ---

st.title("Social Scheduler")
st.caption("Centralized Short Video Publishing")

# Load data once per render
queue_data = get_queue()
uploaded_count = get_uploaded_count()
uploaded_rows = get_uploaded_items(200)

render_platform_status_badge()
st.divider()

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
