import json
import random
import shutil
from datetime import datetime
from pathlib import Path
from typing import List

import pandas as pd
import pytz
import streamlit as st

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
    add_to_queue,
    delete_from_queue,
    get_config,
    get_json_config,
    get_queue,
    init_db,
    reschedule_queue_item,
    set_config,
    set_json_config,
    set_account_state,
)
from src.logging_utils import get_log_file_path, init_logging, tail_log, log_once
from src.notifier import send_telegram_message, telegram_enabled
from src.platform_registry import all_platform_statuses, get_platforms
from src.platforms import tiktok as tiktok_platform
from src.scheduling import get_schedule, human_readable_schedule, next_slots, save_schedule

st.set_page_config(page_title="Social Scheduler", page_icon="SS", layout="centered")
init_db()
logger = init_logging("ui")
log_once(logger, "ui_started", "Streamlit UI started.")

DATA_DIR = Path("data")
UPLOAD_DIR = Path("data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
logger.debug("Uploads directory ready at %s", UPLOAD_DIR)
YOUTUBE_KEY = "youtube_credentials"


def _parse_iso(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _schedule_start(queue_rows) -> datetime:
    cfg = get_schedule()
    tz = pytz.timezone(cfg["timezone"])
    now = datetime.now(tz)
    scheduled = [
        _parse_iso(row.get("scheduled_for"))
        for row in queue_rows
        if row.get("scheduled_for")
    ]
    scheduled = [dt for dt in scheduled if dt]
    if not scheduled:
        return now
    latest = max(scheduled)
    return latest if latest > now else now


def _queue_dataframe(queue_rows):
    data = []
    for row in queue_rows:
        data.append(
            {
                "ID": row["id"],
                "File": Path(row["file_path"]).name,
                "Scheduled": row.get("scheduled_for") or "None",
                "Status": row.get("status"),
                "Attempts": row.get("attempts", 0),
                "Last Error": row.get("last_error") or "",
            }
        )
    if not data:
        return pd.DataFrame(
            columns=["ID", "File", "Scheduled", "Status", "Attempts", "Last Error"]
        )
    return pd.DataFrame(data)


def _save_files_to_queue(files: List, slots: List[datetime], shuffle_order: bool):
    title = get_config("global_title", "Daily Short")
    desc = get_config("global_desc", "#shorts")
    paired = list(zip(files, slots))
    if shuffle_order:
        random.shuffle(paired)
    for uploaded, slot in paired:
        destination = (
            UPLOAD_DIR
            / f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uploaded.name}"
        )
        with destination.open("wb") as f:
            f.write(uploaded.getbuffer())
        add_to_queue(str(destination), slot.isoformat(), title, desc)
        logger.info(
            "Queued file %s for %s",
            destination.name,
            slot.isoformat(),
        )


def _format_datetime(value: str) -> str:
    dt = _parse_iso(value)
    if not dt:
        return "Not scheduled"
    local = dt.astimezone(pytz.timezone(get_schedule()["timezone"]))
    return local.strftime("%b %d, %Y %H:%M")


def _extract_tiktok_session(raw_value: str) -> str:
    if not raw_value:
        return ""
    raw = raw_value.strip()
    # JSON export from browser devtools
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                if "sessionid" in data:
                    return str(data["sessionid"])
                cookies = data.get("cookies")
                if isinstance(cookies, list):
                    for cookie in cookies:
                        if cookie.get("name") == "sessionid":
                            return str(cookie.get("value", ""))
        except json.JSONDecodeError:
            pass
    # Cookie header string: sessionid=XYZ; path=/; ...
    if "sessionid" in raw:
        parts = raw.split(";")
        for part in parts:
            if "sessionid" in part:
                key, _, val = part.partition("=")
                # Clean up "Cookie: sessionid" prefix if present
                key_clean = key.strip()
                if ":" in key_clean:
                    key_clean = key_clean.split(":")[-1].strip()
                if key_clean == "sessionid":
                    return val.strip()
    return raw

def _platform_status_badge():
    statuses = all_platform_statuses()
    registry = get_platforms()
    cols = st.columns(len(registry))
    for col, (key, cfg) in zip(cols, registry.items()):
        connected = cfg["connected"]()
        label = cfg["label"]
        if connected:
            col.success(f"{label}\nConnected")
        else:
            col.warning(f"{label}\nNot linked")
        state = statuses.get(key, {})
        error = state.get("last_error")
        if error and not connected:
            col.caption(error)


def _storage_summary():
    try:
        usage = shutil.disk_usage(DATA_DIR)
        used_gb = usage.used / (1024**3)
        total_gb = usage.total / (1024**3)
        free_gb = usage.free / (1024**3)
        percent = (usage.used / usage.total) * 100 if usage.total else 0
        return used_gb, free_gb, total_gb, percent
    except Exception as exc:
        logger.warning("Unable to read disk usage: %s", exc)
        return None, None, None, None


st.title("Social Scheduler")
st.caption("Upload shorts once and let the Raspberry Pi worker post them everywhere.")

queue_rows = get_queue()
schedule_description = human_readable_schedule()

_platform_status_badge()
st.divider()

tabs = st.tabs(["Dashboard", "Queue", "Accounts", "Settings", "Logs"])

with tabs[0]:
    st.subheader("Status Overview")
    pending = sum(1 for row in queue_rows if row["status"] in ("pending", "retry"))
    processing = sum(1 for row in queue_rows if row["status"] == "processing")
    uploaded = sum(1 for row in queue_rows if row["status"] == "uploaded")
    metrics = st.columns(4)
    metrics[0].metric("Pending", pending)
    metrics[1].metric("Processing", processing)
    metrics[2].metric("Done (recent)", uploaded)
    paused = bool(int(get_config("queue_paused", 0) or 0))
    metrics[3].metric("Queue", "Paused" if paused else "Live")

    st.write(f"**Schedule:** {schedule_description}")

    used_gb, free_gb, total_gb, percent = _storage_summary()
    if total_gb:
        st.progress(percent / 100.0, text=f"Storage used: {used_gb:.1f} / {total_gb:.1f} GB ({percent:.1f}%)")
        storage_cols = st.columns(2)
        storage_cols[0].caption(f"Free: {free_gb:.1f} GB")
        with storage_cols[1]:
            if st.button("Clean oldest uploads", key="clean_uploaded"):
                from src.database import cleanup_uploaded

                deleted, freed = cleanup_uploaded(20)
                logger.info("Cleanup triggered via UI. Removed %s items, freed %s bytes.", deleted, freed)
                if deleted:
                    st.success(f"Deleted {deleted} uploaded items, freed {freed/ (1024**2):.1f} MB.")
                else:
                    st.info("No uploaded items to clean.")

    next_up = next(
        (row for row in queue_rows if row.get("scheduled_for") and row["status"] in ("pending", "retry")),
        None,
    )
    if next_up:
        st.info(f"Next upload #{next_up['id']} at {_format_datetime(next_up['scheduled_for'])}")
    else:
        upcoming = next_slots(1)
        if upcoming:
            st.info(f"No videos waiting. Next slot: {upcoming[0].strftime('%b %d %H:%M %Z')}")
        else:
            st.warning("No valid schedule slots configured.")

with tabs[1]:
    st.subheader("Upload & Queue")
    paused = bool(int(get_config("queue_paused", 0) or 0))
    pause_col, force_col = st.columns([2, 1])
    pause_toggle = pause_col.toggle(
        "Pause uploads",
        value=paused,
        help="When on, the worker will not post until you turn it off.",
        key="pause_toggle",
    )
    if pause_toggle != paused:
        set_config("queue_paused", int(pause_toggle))
        if pause_toggle:
            logger.warning("Queue paused via UI.")
            st.info("Uploads paused. Nothing will be posted until resumed.")
        else:
            logger.info("Queue resumed via UI.")
            st.success("Uploads resumed. Next due item will post at its scheduled slot.")

    confirm = force_col.checkbox("Confirm now", key="confirm_force_upload", value=False)
    force_now = force_col.button(
        "Upload next now",
        help="Process the next queued item immediately.",
        disabled=not confirm,
    )
    if force_now and confirm:
        set_config("queue_force_run", 1)
        logger.info("Force upload requested via UI.")
        st.success("Next due item will be processed immediately by the worker.")

    uploaded_files = st.file_uploader(
        "Drop multiple shorts (mp4/mov)", type=["mp4", "mov", "m4v"], accept_multiple_files=True
    )
    st.caption("Tip: Upload 7–10 at once with one shared title/description.")
    shuffle_order = st.checkbox(
        "Shuffle video order before scheduling (randomize which video goes first)", value=True
    )
    per_platform_shuffle = st.checkbox(
        "Randomize platform order + add 30s gap between platforms",
        value=bool(int(get_config("platform_shuffle", 1) or 0)),
    )
    set_config("platform_shuffle", int(per_platform_shuffle))

    if uploaded_files:
        start = _schedule_start(queue_rows)
        slots = next_slots(len(uploaded_files), start=start)
        if len(slots) < len(uploaded_files):
            st.error(
                "Not enough schedule slots are available in the next weeks. Update the schedule first."
            )
            logger.warning(
                "Queue request rejected: %s uploads, only %s slots available.",
                len(uploaded_files),
                len(slots),
            )
        else:
            readable_slots = "\n".join(slot.strftime("%b %d %H:%M %Z") for slot in slots)
            st.write("These videos will be scheduled for:")
            st.code(readable_slots)
            preview_cols = st.columns(min(3, len(uploaded_files)))
            for idx, uploaded in enumerate(uploaded_files[:3]):
                preview_cols[idx % len(preview_cols)].video(uploaded)
            if st.button("Add videos to queue", width="stretch"):
                logger.info("Queuing %s new videos.", len(uploaded_files))
                _save_files_to_queue(uploaded_files, slots, shuffle_order)
                st.success(f"Queued {len(slots)} videos.")
                st.rerun()

    st.markdown("### Queue")
    df = _queue_dataframe(queue_rows)
    st.dataframe(df, width="stretch", hide_index=True)

    if queue_rows:
        st.markdown("### Manage items")
        for row in queue_rows:
            with st.expander(f"#{row['id']} - {Path(row['file_path']).name}"):
                st.write(f"Scheduled: {_format_datetime(row.get('scheduled_for'))}")
                st.write(f"Status: {row.get('status')} - Attempts: {row.get('attempts', 0)}")
                if Path(row["file_path"]).exists():
                    st.video(str(row["file_path"]), format="video/mp4")
                if row.get("last_error"):
                    st.error(row["last_error"])
                logs = row.get("platform_logs")
                if logs:
                    try:
                        st.json(json.loads(logs))
                    except Exception:
                        st.text(logs)
                actions = st.columns(2)
                if actions[0].button("Delete", key=f"delete_{row['id']}"):
                    delete_from_queue(row["id"])
                    file_path = Path(row["file_path"])
                    if file_path.exists():
                        file_path.unlink(missing_ok=True)
                    logger.info("Deleted queue item %s (%s).", row["id"], row["file_path"])
                    st.rerun()
                if actions[1].button("Push to next slot", key=f"resched_{row['id']}"):
                    anchor = _parse_iso(row.get("scheduled_for")) or _schedule_start(queue_rows)
                    future = next_slots(1, start=anchor)
                    if future:
                        reschedule_queue_item(row["id"], future[0].isoformat())
                        logger.info(
                            "Rescheduled queue item %s to %s.",
                            row["id"],
                            future[0].isoformat(),
                        )
                        st.success("Rescheduled!")
                        st.rerun()
                    else:
                        st.warning("No schedule slot available.")
                        logger.warning("Reschedule failed for %s: no slot available.", row["id"])

with tabs[2]:
    st.subheader("Platform Accounts")

    st.markdown("#### Google API Setup")
    google_config_present = has_google_client_config()
    with st.expander("OAuth client JSON (Desktop app)", expanded=not google_config_present):
        st.caption(
            "Step 1: In Google Cloud Console -> APIs & Services -> Credentials, create OAuth Client ID (Desktop app).\n"
            "Step 2: Download the JSON.\n"
            "Step 3: Paste it here and save."
        )
        google_json = st.text_area(
            "Google client JSON",
            value=get_google_client_config(pretty=True),
            height=200,
            key="google_json_input",
        )
        if st.button("Save Google OAuth JSON", key="save_google_json"):
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
        st.info("Provide Google OAuth client JSON above before linking YouTube.")
    else:
        st.caption(
            "Step 4: Click OAuth link and sign in.\n"
            "Step 5: Copy the verification code Google shows.\n"
            "Step 6: Paste the code below and finish."
        )
        auth_url, err = get_google_auth_url()
        if err:
            st.error(err)
        else:
            st.link_button("Open Google OAuth screen", auth_url)
            yt_code = st.text_input("Paste Google auth code", key="yt_code")
            if st.button("Finish YouTube link"):
                if not yt_code:
                    st.warning("Enter the auth code first.")
                else:
                    ok, message = finish_google_auth(yt_code.strip())
                    if ok:
                        logger.info("YouTube account linked.")
                        st.success(message)
                        st.rerun()
                    else:
                        st.error(message)
                        logger.warning("YouTube link failed: %s", message)
    yt_actions = st.columns(2)
    if yt_actions[0].button("Verify YouTube token"):
        ok, msg = verify_youtube_credentials()
        if ok:
            st.success(msg)
        else:
            st.error(msg)
    if yt_connected and yt_actions[1].button("Disconnect YouTube"):
        set_config(YOUTUBE_KEY, "")
        set_account_state("youtube", False, "Disconnected by user")
        logger.info("YouTube account disconnected by user.")
        st.rerun()

    st.markdown("#### Instagram")
    st.caption("Prefer session cookie login to avoid challenges; username/password is optional fallback.")
    with st.expander("Optional username/password (fallback only)", expanded=False):
        with st.form("ig_form"):
            ig_user = st.text_input("Username", value=get_config("insta_user", ""))
            ig_pass = st.text_input("Password", type="password")
            if st.form_submit_button("Save Instagram credentials"):
                set_config("insta_user", ig_user)
                set_config("insta_pass", ig_pass)
                set_account_state("instagram", bool(ig_user and ig_pass), None)
                logger.info(
                    "Instagram credentials updated (user=%s, password_set=%s).",
                    ig_user or "<blank>",
                    bool(ig_pass),
                )
                st.success("Instagram credentials saved.")
    with st.form("ig_session_form"):
        st.caption("Paste Instagram sessionid (cookie header, raw value, or instagrapi JSON).")
        ig_session_raw = st.text_area("Session cookie / JSON", value=get_config("insta_sessionid", ""), height=120)
        if st.form_submit_button("Save Instagram session"):
            from src.platforms import instagram as instagram_platform

            ok, msg = instagram_platform.save_sessionid(ig_session_raw)
            if ok:
                st.success(msg)
            else:
                st.error(msg)
            st.rerun()
    ig_actions = st.columns(2)
    if ig_actions[0].button("Verify Instagram login now"):
        from src.platforms import instagram as instagram_platform

        ok, msg = instagram_platform.verify_login()
        if ok:
            st.success(msg)
        else:
            st.error(msg)

    st.markdown("#### TikTok")
    tt_status = tiktok_platform.session_status()
    if not tt_status["sessionid"]:
        st.warning("No TikTok session cookie stored.")
    elif not tt_status["valid"]:
        st.error(f"Session invalid: {tt_status.get('message') or 'Re-paste a fresh cookie.'}")
    else:
        handle = tt_status.get("account_name") or "account"
        st.success(f"Session active for @{handle}.")
        if tt_status["needs_refresh"]:
            st.warning("Session cookie is over 25 days old. Grab a fresh cookie soon.")

    last_verified_dt = _parse_iso(tt_status.get("last_verified", ""))
    if last_verified_dt:
        st.caption(f"Last verified {last_verified_dt.strftime('%b %d %H:%M UTC')}")
    if tt_status.get("age_days") is not None:
        st.caption(f"Stored {tt_status['age_days']} days ago (TikTok cookies usually last ~30 days).")

    with st.form("tt_form"):
        st.caption("Paste either the raw `sessionid`, a cookie header snippet, or the exported JSON.")
        tt_session = st.text_area("sessionid cookie", value=tt_status.get("sessionid", ""), height=120)
        if st.form_submit_button("Save TikTok session"):
            session_id = _extract_tiktok_session(tt_session)
            if not session_id:
                st.warning("No sessionid value detected.")
            else:
                tiktok_platform.save_session(session_id)
                # Run verification immediately to prevent "Invalid: success" state
                tiktok_platform.verify_session(force=True)
                logger.info("TikTok session saved (chars=%s).", len(session_id))
                st.success("Session stored and verified.")
                st.rerun()

    tt_actions = st.columns(2)
    if tt_actions[0].button("Verify session now"):
        ok, msg = tiktok_platform.verify_session(force=True)
        logger.info("Manual TikTok verification -> %s", msg)
        if ok:
            st.success(msg)
        else:
            st.error(msg)
        st.rerun()
    if tt_actions[1].button("Clear TikTok session"):
        tiktok_platform.save_session("")
        logger.warning("TikTok session cleared by user.")
        st.info("TikTok session cleared.")
        st.rerun()

with tabs[3]:
    st.subheader("Global Settings")

    st.markdown("### Publishing cadence")
    schedule = get_schedule()
    days_map = {
        "Mon": 0,
        "Tue": 1,
        "Wed": 2,
        "Thu": 3,
        "Fri": 4,
        "Sat": 5,
        "Sun": 6,
    }
    with st.form("schedule_form"):
        selected_days = st.multiselect(
            "Days of week",
            options=list(days_map.keys()),
            default=[k for k, v in days_map.items() if v in schedule["days"]],
        )
        times_input = st.text_input(
            "Times (HH:MM, comma separated)", value=", ".join(schedule["times"])
        )
        tz_options = list(pytz.common_timezones)
        tz_name = schedule["timezone"]
        if tz_name not in tz_options:
            tz_options = [tz_name] + tz_options
        timezone = st.selectbox("Timezone", tz_options, index=tz_options.index(tz_name))
        if st.form_submit_button("Save schedule"):
            parsed_times = [t.strip() for t in times_input.split(",") if t.strip()]
            parsed_days = [days_map[d] for d in selected_days]
            save_schedule(parsed_days, parsed_times, timezone)
            logger.info(
                "Schedule updated: days=%s, times=%s, timezone=%s",
                parsed_days,
                parsed_times,
                timezone,
            )
            st.success("Schedule saved.")
            st.rerun()

    st.markdown("### Global metadata")
    with st.form("meta_form"):
        title = st.text_input("Title", value=get_config("global_title", ""))
        desc = st.text_area("Caption / Description", value=get_config("global_desc", ""), height=150)
        if st.form_submit_button("Save metadata"):
            set_config("global_title", title)
            set_config("global_desc", desc)
            logger.info("Global metadata updated (title_len=%s, desc_len=%s).", len(title), len(desc))
            st.success("Metadata saved.")

    st.markdown("### Telegram alerts")
    with st.form("telegram_form"):
        bot_token = st.text_input(
            "Bot token", value=get_config("telegram_bot_token", ""), type="password"
        )
        chat_id = st.text_input("Chat ID", value=get_config("telegram_chat_id", ""))
        if st.form_submit_button("Save Telegram settings"):
            set_config("telegram_bot_token", bot_token)
            set_config("telegram_chat_id", chat_id)
            logger.info("Telegram settings updated (token_set=%s, chat_id=%s).", bool(bot_token), chat_id or "<blank>")
            st.success("Telegram settings saved.")
    if telegram_enabled():
        if st.button("Send test alert"):
            send_telegram_message("Telegram notifications are configured.")
            logger.info("Test Telegram notification dispatched.")
            st.success("Test notification sent (check Telegram).")

with tabs[4]:
    st.subheader("Logs")
    log_path = get_log_file_path()
    st.caption(f"Log file: {log_path}")
    lines = st.slider("Lines to display", 50, 1000, 200, step=50, key="log_lines")
    if st.button("Refresh logs", key="refresh_logs"):
        st.rerun()
    log_text = tail_log(lines)
    st.code(log_text, language="text")
    if log_path.exists():
        log_data = log_path.read_text(encoding="utf-8", errors="ignore")
        st.download_button(
            "Download log file",
            data=log_data,
            file_name="scheduler.log",
            mime="text/plain",
        )
