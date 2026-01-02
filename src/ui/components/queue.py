import json
import streamlit as st
from pathlib import Path
from datetime import datetime, timedelta
from src.database import clear_platform_status, delete_from_queue, reschedule_queue_item, update_queue_status, get_queue_item, set_config, get_config
from src.scheduling import next_daily_slots
from src.platform_registry import get_platforms
from src import ui_logic

FORCE_KEY = "queue_force_run"
FORCE_PLATFORM_KEY = "queue_force_platform"


def render_calendar_view(queue_rows):
    """Render a calendar view of scheduled uploads with gap detection."""
    from src.scheduling import get_schedule

    st.markdown("### **Calendar View**")

    # Parse scheduled dates
    scheduled_dates = {}
    for row in queue_rows:
        if row.get("status") in ("pending", "retry", "processing"):
            scheduled_for = row.get("scheduled_for")
            if scheduled_for:
                try:
                    dt = ui_logic.parse_iso(scheduled_for)
                    if dt:
                        date_key = dt.date().isoformat()
                        if date_key not in scheduled_dates:
                            scheduled_dates[date_key] = []
                        scheduled_dates[date_key].append(row)
                except Exception:
                    pass

    if not scheduled_dates:
        st.info("No scheduled uploads to display")
        return

    # Get schedule config for gap detection
    schedule = get_schedule()
    enabled_weekdays = set(schedule["days"])  # 0=Monday, 6=Sunday

    # Show next 14 days
    today = datetime.now().date()
    num_days = 14

    st.markdown("**Next 14 Days**")

    # Build calendar HTML using CSS Grid
    calendar_html = '<div class="calendar-grid">'

    for i in range(num_days):
        day = today + timedelta(days=i)
        date_key = day.isoformat()
        is_scheduled_day = day.weekday() in enabled_weekdays
        has_upload = date_key in scheduled_dates
        is_today = day == today

        # Determine CSS class
        if has_upload:
            day_class = "calendar-day has-upload"
            count = len(scheduled_dates[date_key])
            content = f"✓ {count} video{'s' if count > 1 else ''}"
        elif is_scheduled_day:
            day_class = "calendar-day gap-day"
            content = "⚠ No upload"
        else:
            day_class = "calendar-day no-schedule"
            content = "—"

        # Header styling
        header_class = "calendar-day-header today" if is_today else "calendar-day-header"

        calendar_html += f'''
        <div class="{day_class}">
            <div class="{header_class}">{day.strftime('%a %d')}</div>
            <div class="calendar-day-content">{content}</div>
        </div>
        '''

    calendar_html += '</div>'

    # Render the calendar
    st.html(calendar_html)

    # Show gap summary
    gaps = []
    for i in range(num_days):
        day = today + timedelta(days=i)
        is_scheduled_day = day.weekday() in enabled_weekdays
        date_key = day.isoformat()
        has_upload = date_key in scheduled_dates

        if is_scheduled_day and not has_upload:
            gaps.append(day.strftime("%a, %b %d"))

    if gaps:
        gap_warning_html = f'''
        <div class="calendar-gap-warning">
            <div class="calendar-gap-warning-title">⚠ Gaps Detected</div>
            <div class="calendar-gap-warning-text">{', '.join(gaps[:5])}</div>
        </div>
        '''
        st.html(gap_warning_html)
        if len(gaps) > 5:
            st.caption(f"... and {len(gaps) - 5} more")


def _parse_platform_logs(log_value):
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


def render_platform_status_row(row_id: int, platform_key: str, label: str, log_value, file_path: str, logger):
    """Render status and force upload button for a single platform."""
    logs = _parse_platform_logs(log_value)
    status_text = logs.get(platform_key, "")
    
    # Determine status
    is_success = "success" in str(status_text).lower() or "uploaded" in str(status_text).lower() or "id:" in str(status_text).lower()
    is_failed = status_text and not is_success
    
    # Show status
    if is_success:
        st.success(f"✓ {label}: Success")
    elif is_failed:
        st.error(f"✗ {label}: {status_text[:40]}...")
    else:
        st.info(f"○ {label}: Pending")
    
    # Force button below status (only if not already successful)
    if not is_success:
        if st.button(f"Force {label}", key=f"force_{row_id}_{platform_key}"):
            # Clear the platform status to allow retry
            cleared = clear_platform_status(row_id, platform_key)
            if cleared:
                # Get updated logs and set queue status to retry
                row = get_queue_item(row_id)
                current_logs = _parse_platform_logs(row.get("platform_logs")) if row else {}
                update_queue_status(row_id, "retry", None, current_logs)
                
                # Set force flag for this platform
                set_config(FORCE_KEY, 1)
                set_config(FORCE_PLATFORM_KEY, platform_key)
                logger.info("Manual force upload triggered for queue #%s, platform: %s", row_id, label)
                st.success(f"Force {label} queued!")
                st.rerun()
            else:
                logger.warning("Failed to clear platform status for queue #%s, platform: %s", row_id, label)
                st.error("Failed to clear platform status")


def render_queue_tab(queue_rows, uploaded_rows, UPLOAD_DIR, logger):
    """Render upload queue."""

    # Calendar view at top
    with st.expander("📅 Calendar View", expanded=False):
        render_calendar_view(queue_rows)

    # Quick actions
    has_queue_items = any(row["status"] in ("pending", "retry") for row in queue_rows)
    
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Shuffle Queue", key="shuffle_queue_btn", disabled=not has_queue_items):
            shuffled, _ = ui_logic.shuffle_queue(queue_rows)
            logger.info("Queue shuffled: %d items", shuffled)
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
                logger.info("Deleted queue item #%s", next_item["id"])
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
        # Handle both single file and list
        if not isinstance(uploaded_files, list):
            uploaded_files = [uploaded_files]

        # Single video = custom scheduling mode
        if len(uploaded_files) == 1:
            st.markdown("**Custom Video Settings**")

            # Platform selection
            st.markdown("**Select Platforms:**")
            platforms = get_platforms()
            col_p1, col_p2, col_p3 = st.columns(3)
            enabled_platforms = []

            with col_p1:
                if "youtube" in platforms and st.checkbox("YouTube", value=True, key="custom_yt"):
                    enabled_platforms.append("youtube")
            with col_p2:
                if "instagram" in platforms and st.checkbox("Instagram", value=True, key="custom_ig"):
                    enabled_platforms.append("instagram")
            with col_p3:
                if "tiktok" in platforms and st.checkbox("TikTok", value=True, key="custom_tt"):
                    enabled_platforms.append("tiktok")

            # Custom title and description
            custom_title = st.text_input(
                "Title (for YouTube)",
                value=get_config("global_title", "Daily Short"),
                max_chars=100,
                key="custom_title_input"
            )

            custom_desc = st.text_area(
                "Description",
                value=get_config("global_desc", "#shorts"),
                max_chars=2200,
                key="custom_desc_input"
            )

            # Custom date/time picker
            col_date, col_time = st.columns(2)
            with col_date:
                custom_date = st.date_input(
                    "Schedule Date",
                    value=ui_logic.get_schedule_start_time(queue_rows).date(),
                    key="custom_date_input"
                )
            with col_time:
                custom_time = st.time_input(
                    "Schedule Time",
                    value=ui_logic.get_schedule_start_time(queue_rows).time(),
                    key="custom_time_input"
                )

            # Combine date and time
            from datetime import datetime
            custom_datetime = datetime.combine(custom_date, custom_time)

            # Replace timezone info from schedule start time
            start_dt = ui_logic.get_schedule_start_time(queue_rows)
            if start_dt.tzinfo:
                custom_datetime = custom_datetime.replace(tzinfo=start_dt.tzinfo)

            # Preview
            st.video(uploaded_files[0])

            if st.button("Queue Video", key="queue_custom_video_btn", type="primary"):
                if not enabled_platforms:
                    st.error("Please select at least one platform!")
                else:
                    sig = (uploaded_files[0].name, getattr(uploaded_files[0], "size", None), custom_datetime.isoformat())
                    if st.session_state.get("queued_sig") != sig:
                        count = ui_logic.save_custom_video_to_queue(
                            uploaded_files[0],
                            custom_datetime,
                            UPLOAD_DIR,
                            custom_title,
                            custom_desc,
                            enabled_platforms
                        )
                        if count > 0:
                            logger.info("Queued custom video for %s", custom_datetime.isoformat())
                            st.session_state["queued_sig"] = sig
                            st.success(f"Video queued for {custom_datetime.strftime('%b %d, %Y at %H:%M')}!")
                            st.rerun()
        else:
            # Multiple videos = batch mode with global settings
            start_dt = ui_logic.get_schedule_start_time(queue_rows)
            occupied = ui_logic.occupied_schedule_dates(queue_rows)
            slots = next_daily_slots(len(uploaded_files), start=start_dt, occupied_dates=occupied)

            if len(slots) < len(uploaded_files):
                logger.warning("Not enough schedule slots for %d videos", len(uploaded_files))
                st.error(f"Not enough slots")
            else:
                st.markdown(f"**{len(uploaded_files)} videos** scheduled")

                # Preview first 2
                for uploaded in uploaded_files[:2]:
                    st.video(uploaded)

                # View schedule
                with st.expander("Schedule"):
                    st.write("\n".join(s.strftime("%b %d %H:%M") for s in slots))

                if st.button(f"Queue {len(uploaded_files)} Videos", key="queue_videos_btn", type="primary"):
                    sig = tuple((f.name, getattr(f, "size", None)) for f in uploaded_files)
                    if st.session_state.get("queued_sig") != sig:
                        count = ui_logic.save_files_to_queue(uploaded_files, slots, UPLOAD_DIR, shuffle_order=False)
                        if count > 0:
                            logger.info("Queued %d videos for upload", count)
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

            # Parse enabled platforms for display
            enabled_platforms_display = []
            raw_enabled = row.get("enabled_platforms")
            if raw_enabled:
                try:
                    if isinstance(raw_enabled, str):
                        enabled_platforms_list = json.loads(raw_enabled)
                    else:
                        enabled_platforms_list = raw_enabled

                    # Map platform keys to emoji/short labels
                    platform_labels = {
                        "youtube": "YT",
                        "instagram": "IG",
                        "tiktok": "TT"
                    }
                    enabled_platforms_display = [platform_labels.get(p, p.upper()[:2]) for p in enabled_platforms_list]
                except (json.JSONDecodeError, TypeError):
                    pass

            # Build title with platform indicators
            platform_indicator = f" [{'/'.join(enabled_platforms_display)}]" if enabled_platforms_display else ""
            expander_title = f"{icon} #{row['id']} - {Path(row['file_path']).name[:25]}{platform_indicator}"

            with st.expander(expander_title):
                col_info, col_vid = st.columns([1, 1])

                with col_info:
                    st.write(f"**{ui_logic.format_datetime_for_ui(row.get('scheduled_for'))}**")
                    st.write(f"Status: {row['status']}")

                    # Show custom title/description if set
                    if row.get("title") and row.get("title") != get_config("global_title", ""):
                        st.write(f"Title: {row['title'][:40]}...")
                    if row.get("description") and row.get("description") != get_config("global_desc", ""):
                        st.write(f"Desc: {row['description'][:40]}...")
                    if row.get("last_error"):
                        st.error(row['last_error'][:50])
                    
                    # Platform status section
                    st.markdown("**Platforms:**")

                    # Determine which platforms to show for this video
                    platforms_to_show = platforms.keys()
                    if raw_enabled:
                        try:
                            if isinstance(raw_enabled, str):
                                enabled_list = json.loads(raw_enabled)
                            else:
                                enabled_list = raw_enabled
                            platforms_to_show = [p for p in platforms.keys() if p in enabled_list]
                        except (json.JSONDecodeError, TypeError):
                            pass

                    for pkey in platforms_to_show:
                        if pkey in platforms:
                            pcfg = platforms[pkey]
                            render_platform_status_row(
                                row["id"],
                                pkey,
                                pcfg["label"],
                                row.get("platform_logs"),
                                row["file_path"],
                                logger,
                            )
                    
                    st.markdown("---")
                    ac1, ac2 = st.columns(2)
                    if ac1.button("Delete", key=f"del_{row['id']}"):
                        delete_from_queue(row["id"])
                        fp = Path(row["file_path"])
                        if fp.exists():
                            fp.unlink(missing_ok=True)
                        logger.info("Deleted queue item #%s from queue list", row["id"])
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
                            logger.info("Rescheduled queue item #%s to %s", row["id"], future[0].isoformat())
                            st.success("Rescheduled!")
                            st.rerun()
                
                with col_vid:
                    if Path(row["file_path"]).exists():
                        st.video(str(row["file_path"]))
                    else:
                        logger.warning("File missing for queue item #%s: %s", row["id"], row["file_path"])
                        st.warning("File missing")
    else:
        st.info("No videos in queue")
