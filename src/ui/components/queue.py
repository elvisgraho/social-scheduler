import json
import streamlit as st
import pytz
from pathlib import Path
from datetime import datetime, timedelta
from src.database import clear_platform_status, delete_from_queue, reschedule_queue_item, update_queue_status, get_queue_item, set_config, get_config, restore_archived_to_queue, delete_uploaded_item, get_uploaded_items
from src.scheduling import next_daily_slots, get_schedule
from src.platform_registry import get_platforms
from src import ui_logic

FORCE_KEY = "queue_force_run"
FORCE_PLATFORM_KEY = "queue_force_platform"
FORCE_QUEUE_ID_KEY = "queue_force_id"


def _get_schedule_timezone():
    """Return the configured schedule timezone, falling back to UTC."""
    schedule = get_schedule()
    try:
        return pytz.timezone(schedule.get("timezone", "UTC"))
    except pytz.UnknownTimeZoneError:
        return pytz.UTC


def _as_schedule_datetime(value):
    """Convert a stored datetime string into the configured schedule timezone."""
    dt = ui_logic.parse_iso(value)
    if not dt:
        return None

    tz = _get_schedule_timezone()
    if dt.tzinfo is None:
        return tz.localize(dt)
    return dt.astimezone(tz)


def _combine_schedule_datetime(selected_date, selected_time):
    """Build a timezone-aware datetime from separate date and time inputs."""
    tz = _get_schedule_timezone()
    combined = datetime.combine(selected_date, selected_time)
    if hasattr(tz, "localize"):
        return tz.localize(combined)
    return combined.replace(tzinfo=tz)


@st.fragment
def render_calendar_view(queue_rows):
    """Render a calendar view of scheduled uploads with gap detection."""
    st.markdown("### **Calendar View**")

    # Get schedule config for timezone consistency
    schedule = get_schedule()
    tz = pytz.timezone(schedule.get("timezone", "UTC"))
    enabled_weekdays = set(schedule["days"])  # 0=Monday, 6=Sunday

    # Use schedule timezone for today to match scheduled dates
    today = datetime.now(tz).date()

    # Parse scheduled dates from queue (pending uploads)
    scheduled_dates = {}
    for row in queue_rows or []:
        if row.get("status") in ("pending", "retry", "processing"):
            scheduled_for = row.get("scheduled_for")
            if scheduled_for:
                try:
                    dt = ui_logic.parse_iso(scheduled_for)
                    if dt:
                        # Convert to schedule timezone before extracting date
                        if dt.tzinfo:
                            dt = dt.astimezone(tz)
                        date_key = dt.date().isoformat()
                        if date_key not in scheduled_dates:
                            scheduled_dates[date_key] = []
                        scheduled_dates[date_key].append(row)
                except Exception:
                    pass

    # Also include already-uploaded items (from uploads table)
    uploaded_dates = set()
    for row in get_uploaded_items(limit=10):
        uploaded_at = row.get("uploaded_at")
        if uploaded_at:
            try:
                dt = ui_logic.parse_iso(uploaded_at)
                if dt:
                    if dt.tzinfo:
                        dt = dt.astimezone(tz)
                    uploaded_dates.add(dt.date().isoformat())
            except Exception:
                pass

    if not scheduled_dates and not uploaded_dates:
        st.info("No scheduled uploads to display")
        return

    # Show next 21 days
    num_days = 21

    st.markdown("**Next 21 Days**")

    # Pre-calculate all calendar data to minimize HTML string operations
    calendar_cells = []
    gaps = []

    for i in range(num_days):
        day = today + timedelta(days=i)
        date_key = day.isoformat()
        is_scheduled_day = day.weekday() in enabled_weekdays
        has_pending = date_key in scheduled_dates
        has_uploaded = date_key in uploaded_dates
        is_today = day == today

        # Determine CSS class and content
        if has_pending or has_uploaded:
            day_class = "calendar-day has-upload"
            pending_count = len(scheduled_dates.get(date_key, []))
            if has_pending and has_uploaded:
                content = f"✓ {pending_count}+"
            elif has_pending:
                content = f"✓ {pending_count}"
            else:
                content = "✓"
        elif is_scheduled_day:
            day_class = "calendar-day gap-day"
            content = "⚠"
            gaps.append(day.strftime("%a, %b %d"))
        else:
            day_class = "calendar-day no-schedule"
            content = "—"

        # Header styling
        header_class = "calendar-day-header today" if is_today else "calendar-day-header"

        # Store cell HTML
        calendar_cells.append(
            f'<div class="{day_class}">'
            f'<div class="{header_class}">{day.strftime("%a %d")}</div>'
            f'<div class="calendar-day-content">{content}</div>'
            f'</div>'
        )

    # Build complete calendar HTML in one operation (faster than concatenation)
    calendar_html = '<div class="calendar-grid">' + ''.join(calendar_cells) + '</div>'

    # Render the calendar
    st.html(calendar_html)

    # Show gap summary if needed
    if gaps:
        gap_warning_html = (
            '<div class="calendar-gap-warning">'
            '<div class="calendar-gap-warning-title">⚠ Gaps Detected</div>'
            f'<div class="calendar-gap-warning-text">{", ".join(gaps[:5])}</div>'
            '</div>'
        )
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


def render_platform_status_row(row_id: int, platform_key: str, label: str, log_value, logger):
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
        truncated = str(status_text)
        suffix = "..." if len(truncated) > 40 else ""
        st.error(f"✗ {label}: {truncated[:40]}{suffix}")
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
                
                # Set force flag for this specific queue item and platform
                set_config(FORCE_KEY, 1)
                set_config(FORCE_PLATFORM_KEY, platform_key)
                set_config(FORCE_QUEUE_ID_KEY, row_id)
                logger.info("Manual force upload triggered for queue #%s, platform: %s", row_id, label)
                st.success(f"✓ Force {label} queued!")
                st.rerun()
            else:
                logger.warning("Failed to clear platform status for queue #%s, platform: %s", row_id, label)
                st.error("Failed to clear platform status")


def render_queue_tab(queue_rows, uploaded_rows, UPLOAD_DIR, logger):
    """Render upload queue."""

    # Initialize session state for expanded items to prevent re-expansion on rerun
    if "expanded_queue_items" not in st.session_state:
        st.session_state.expanded_queue_items = set()
    
    # Initialize processing state for buttons
    if "queue_processing" not in st.session_state:
        st.session_state.queue_processing = {}

    # Calendar view at top
    with st.expander("📅 Calendar View", expanded=False):
        render_calendar_view(queue_rows)

    # Quick actions
    has_queue_items = any(row["status"] in ("pending", "retry") for row in queue_rows)

    c1, c2, c3 = st.columns(3)

    # Disable buttons while processing
    is_shuffling = st.session_state.queue_processing.get("shuffle", False)
    is_deleting = st.session_state.queue_processing.get("delete_next", False)
    is_compacting = st.session_state.queue_processing.get("fill_gaps", False)

    with c1:
        if st.button("Shuffle Queue", key="shuffle_queue_btn", disabled=not has_queue_items or is_shuffling):
            st.session_state.queue_processing["shuffle"] = True
            try:
                shuffled, _ = ui_logic.shuffle_queue(queue_rows)
                if shuffled > 0:
                    logger.info("Queue shuffled: %d items", shuffled)
                    st.cache_data.clear()
                    st.success(f"✓ Shuffled {shuffled} items!")
                else:
                    st.info("No items to shuffle")
            except Exception as e:
                logger.error("Failed to shuffle queue: %s", e, exc_info=True)
                st.error("Failed to shuffle queue. Please try again.")
            st.session_state.queue_processing["shuffle"] = False
            st.rerun()

    with c3:
        if st.button(
            "Fill Gaps",
            key="fill_gaps_btn",
            help="Pack all pending videos into the nearest consecutive slots (preserves order, closes gaps left by deletions)",
            disabled=not has_queue_items or is_compacting,
        ):
            st.session_state.queue_processing["fill_gaps"] = True
            try:
                compacted, first_slot = ui_logic.reschedule_pending_items(queue_rows)
                if compacted > 0:
                    logger.info("Queue compacted: %d items moved, first slot %s", compacted, first_slot)
                    st.cache_data.clear()
                    st.success(f"✓ Moved {compacted} items — next up {first_slot.strftime('%b %d at %H:%M') if first_slot else '?'}")
                else:
                    st.info("No items to compact")
            except Exception as e:
                logger.error("Failed to compact queue: %s", e, exc_info=True)
                st.error("Failed to fill gaps. Please try again.")
            st.session_state.queue_processing["fill_gaps"] = False
            st.rerun()

    with c2:
        if st.button("Delete Next", key="delete_next_btn", type="secondary", disabled=not bool(queue_rows) or is_deleting):
            st.session_state.queue_processing["delete_next"] = True
            try:
                next_item = next((row for row in queue_rows if row.get("status") in ("pending", "retry", "failed")), None)
                if next_item:
                    delete_from_queue(next_item["id"])
                    fp = Path(next_item["file_path"])
                    if fp.exists():
                        fp.unlink(missing_ok=True)
                    logger.info("Deleted queue item #%s", next_item["id"])
                    # Clear cache to show updated data
                    st.cache_data.clear()
                    st.success(f"✓ Removed #{next_item['id']}")
                else:
                    st.info("No items to delete")
            except Exception as e:
                logger.error("Failed to delete queue item: %s", e, exc_info=True)
                st.error("Failed to delete item. Please try again.")
            st.session_state.queue_processing["delete_next"] = False
            st.rerun()
    
    # Upload section
    st.markdown("### **Upload**")
    uploaded_files = st.file_uploader(
        "Drop videos (mp4, mov)",
        label_visibility="hidden",
        type=["mp4", "mov", "m4v"],
        accept_multiple_files=True
    )

    if uploaded_files:
        # Handle both single file and list
        if not isinstance(uploaded_files, list):
            uploaded_files = [uploaded_files]

        # Single video = choice between custom scheduling and regular queue
        if len(uploaded_files) == 1:
            # Preview video safely with error handling
            try:
                if uploaded_files[0].size > 0:
                    st.video(uploaded_files[0])
                else:
                    st.warning("⚠ Video file is empty")
            except Exception as e:
                logger.warning("Failed to preview uploaded video: %s", e)
                st.warning("⚠ Video preview unavailable (file may be processing)")

            # Choice between custom schedule and regular queue
            schedule_mode = st.radio(
                "How would you like to schedule this video?",
                options=["Add to Regular Queue", "Custom Schedule"],
                horizontal=True,
                key="single_video_schedule_mode"
            )

            if schedule_mode == "Add to Regular Queue":
                # Regular queue mode - auto-schedule like batch uploads
                st.markdown("**Add to Regular Queue**")
                st.info("Video will be scheduled for the next available slot based on your schedule settings.")
                
                # Platform selection for regular queue
                st.markdown("**Select Platforms:**")
                platforms = get_platforms()
                platform_cols = st.columns([1, 1])
                enabled_platforms = []
                
                platform_list = list(platforms.keys())
                for i, pkey in enumerate(platform_list):
                    col = platform_cols[i % 2]
                    with col:
                        label = platforms[pkey]["label"]
                        if st.checkbox(label, value=True, key=f"regular_{pkey}"):
                            enabled_platforms.append(pkey)

                # Queue Video button
                is_queueing = st.session_state.queue_processing.get("queue_video_regular", False)
                file_sig_regular = (uploaded_files[0].name, uploaded_files[0].size, "regular")
                already_queued_regular = st.session_state.get("queued_video_sig_regular") == file_sig_regular
                
                if st.button(
                    "Add to Queue",
                    key="queue_regular_video_btn",
                    type="primary",
                    disabled=is_queueing or already_queued_regular
                ):
                    if not enabled_platforms:
                        st.error("Please select at least one platform!")
                    else:
                        st.session_state.queue_processing["queue_video_regular"] = True
                        
                        if st.session_state.get("queued_video_sig_regular") != file_sig_regular:
                            try:
                                with st.spinner("Queueing video..."):
                                    # Get next available slot
                                    start_dt = ui_logic.get_schedule_start_time(queue_rows)
                                    occupied = ui_logic.occupied_schedule_dates(queue_rows)
                                    slots = next_daily_slots(1, start=start_dt, occupied_dates=occupied)
                                    
                                    if slots:
                                        # Save with selected platforms
                                        count = ui_logic.save_custom_video_to_queue(
                                            uploaded_files[0],
                                            slots[0],
                                            UPLOAD_DIR,
                                            get_config("global_title", "Daily Short"),
                                            get_config("global_desc", "#shorts"),
                                            enabled_platforms
                                        )
                                        if count > 0:
                                            logger.info("Queued video for %s (regular queue)", slots[0].isoformat())
                                            st.session_state["queued_video_sig_regular"] = file_sig_regular
                                            # Clear the processing state on success
                                            st.session_state.queue_processing["queue_video_regular"] = False
                                            st.cache_data.clear()
                                            st.success(f"✓ Video queued for {slots[0].strftime('%b %d, %Y at %H:%M')}!")
                                            st.rerun()
                                        else:
                                            st.error("Failed to queue video. Check logs for details.")
                                            st.session_state.queue_processing["queue_video_regular"] = False
                                    else:
                                        st.error("No available schedule slots found!")
                                        st.session_state.queue_processing["queue_video_regular"] = False
                            except Exception as e:
                                logger.error("Failed to queue video: %s", e, exc_info=True)
                                st.error("Failed to queue video. Please try again.")
                                st.session_state.queue_processing["queue_video_regular"] = False
                        else:
                            st.info("This video has already been queued.")
                            st.session_state.queue_processing["queue_video_regular"] = False

            else:
                # Custom scheduling mode
                st.markdown("**Custom Video Settings**")

                # Platform selection - use 2 columns that stack better on mobile
                st.markdown("**Select Platforms:**")
                platforms = get_platforms()
                platform_cols = st.columns([1, 1])
                enabled_platforms = []
                
                # Distribute platforms evenly
                platform_list = list(platforms.keys())
                for i, pkey in enumerate(platform_list):
                    col = platform_cols[i % 2]
                    with col:
                        label = platforms[pkey]["label"]
                        if st.checkbox(label, value=True, key=f"custom_{pkey}"):
                            enabled_platforms.append(pkey)

                # Custom title and description
                col_title, col_desc = st.columns([1, 1])
                with col_title:
                    custom_title = st.text_input(
                        "Title (for YouTube)",
                        value=get_config("global_title", "Daily Short"),
                        max_chars=100,
                        key="custom_title_input"
                    )
                with col_desc:
                    custom_desc = st.text_area(
                        "Description",
                        value=get_config("global_desc", "#shorts"),
                        max_chars=2200,
                        key="custom_desc_input"
                    )

                # Custom date/time picker - stacked on mobile
                st.markdown("**Schedule**")
                col_date, col_time = st.columns([1, 1])
                with col_date:
                    custom_date = st.date_input(
                        "Date",
                        value=ui_logic.get_schedule_start_time(queue_rows).date(),
                        key="custom_date_input"
                    )
                with col_time:
                    custom_time = st.time_input(
                        "Time",
                        value=ui_logic.get_schedule_start_time(queue_rows).time(),
                        key="custom_time_input"
                    )

                # Combine date and time in the configured schedule timezone
                custom_datetime = _combine_schedule_datetime(custom_date, custom_time)
                now_in_schedule_tz = datetime.now(_get_schedule_timezone())

                # Queue Video button with disable state and proper state management
                if "queue_video_attempt" not in st.session_state:
                    st.session_state.queue_video_attempt = None
                
                is_queueing = st.session_state.queue_processing.get("queue_video", False)
                
                # Check if this specific file has already been queued
                file_sig = (uploaded_files[0].name, uploaded_files[0].size, custom_datetime.isoformat())
                already_queued = st.session_state.get("queued_video_sig") == file_sig
                
                if st.button(
                    "Queue Video",
                    key="queue_custom_video_btn",
                    type="primary",
                    disabled=is_queueing or already_queued
                ):
                    if not enabled_platforms:
                        st.error("Please select at least one platform!")
                    elif custom_datetime <= now_in_schedule_tz:
                        st.error("Please choose a future date and time.")
                    else:
                        st.session_state.queue_processing["queue_video"] = True
                        
                        # Double-check signature to prevent duplicate submissions
                        if st.session_state.get("queued_video_sig") != file_sig:
                            try:
                                with st.spinner("Queueing video..."):
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
                                        st.session_state["queued_video_sig"] = file_sig
                                        # Clear the processing state on success
                                        st.session_state.queue_processing["queue_video"] = False
                                        # Clear cache to show updated queue
                                        st.cache_data.clear()
                                        st.success(f"✓ Video queued for {custom_datetime.strftime('%b %d, %Y at %H:%M')}!")
                                        st.rerun()
                                    else:
                                        st.error("Failed to queue video. Check logs for details.")
                                        # Clear the processing state on error
                                        st.session_state.queue_processing["queue_video"] = False
                            except Exception as e:
                                logger.error("Failed to queue video: %s", e, exc_info=True)
                                st.error("Failed to queue video. Please try again.")
                                st.session_state.queue_processing["queue_video"] = False
                        else:
                            st.info("This video has already been queued.")
                            st.session_state.queue_processing["queue_video"] = False
        else:
            # Multiple videos = auto-queue with batch mode
            # Auto-queue: Create signature and check if already processed
            
            # Validate all files have size
            valid_files = True
            for f in uploaded_files:
                if not getattr(f, 'size', 0) or getattr(f, 'size', 0) == 0:
                    st.error(f"❌ File '{f.name}' is empty or invalid")
                    valid_files = False
                    break
            
            if not valid_files:
                st.info("Please upload valid video files with content.")
            else:
                try:
                    sig_data = tuple((f.name, f.size) for f in uploaded_files)
                    sig = hash(sig_data)
                except Exception:
                    # Fallback to simple count if hashing fails
                    sig = f"batch_{len(uploaded_files)}_{datetime.now().timestamp()}"

                # Only process if these files haven't been queued yet
                if st.session_state.get("queued_sig") != sig:
                    # Get schedule slots
                    start_dt = ui_logic.get_schedule_start_time(queue_rows)
                    occupied = ui_logic.occupied_schedule_dates(queue_rows)
                    slots = next_daily_slots(len(uploaded_files), start=start_dt, occupied_dates=occupied)

                    if len(slots) < len(uploaded_files):
                        logger.warning("Not enough schedule slots for %d videos", len(uploaded_files))
                        st.error(f"❌ Not enough schedule slots available for {len(uploaded_files)} videos")
                        st.info(f"Only {len(slots)} slots available. Please check your schedule settings.")
                    else:
                        # Show what's being queued
                        st.markdown(f"**Queuing {len(uploaded_files)} videos...**")

                        # Show progress with file info
                        for f in uploaded_files:
                            st.caption(f"• {f.name} ({f.size / (1024*1024):.1f} MB)")
                        
                        with st.spinner(f"Processing {len(uploaded_files)} files..."):
                            count = ui_logic.save_files_to_queue(uploaded_files, slots, UPLOAD_DIR, shuffle_order=False)

                        if count > 0:
                            logger.info("Auto-queued %d videos for upload", count)
                            st.session_state["queued_sig"] = sig
                            # Clear cache to show updated queue
                            st.cache_data.clear()
                            st.success(f"✅ Successfully queued {count} videos!")

                            # Show schedule preview
                            with st.expander("📅 View Schedule", expanded=False):
                                for i, slot in enumerate(slots[:count], 1):
                                    st.caption(f"{i}. {slot.strftime('%a, %b %d at %H:%M')}")

                            st.rerun()
                        elif count == 0:
                            st.error("❌ Failed to queue videos. Check logs for details.")
                            # Clear signature so user can try again
                            st.session_state.pop("queued_sig", None)
                else:
                    # Already queued - show info
                    st.info(f"ℹ️ {len(uploaded_files)} videos already queued")
                    st.caption("Upload different files or refresh to queue again")
    else:
        st.session_state.pop("queued_sig", None)
        st.session_state.pop("queued_video_sig", None)
        st.session_state.pop("queued_video_sig_regular", None)
    
    # Queue list
    st.markdown("### **Queue**")

    if queue_rows:
        platforms = get_platforms()

        # Read global defaults once — avoids a DB round-trip per queue row
        _global_title = get_config("global_title", "")
        _global_desc = get_config("global_desc", "")

        # Add pagination to prevent crashes with many videos
        items_per_page = 20
        total_items = len(queue_rows)
        total_pages = max(1, (total_items + items_per_page - 1) // items_per_page)

        if total_items > items_per_page:
            page = st.number_input(
                "Page",
                min_value=1,
                max_value=total_pages,
                value=1,
                step=1,
                key="queue_page"
            )
            start_idx = (page - 1) * items_per_page
            end_idx = min(start_idx + items_per_page, total_items)
            st.caption(f"Showing {end_idx - start_idx} of {total_items} items (page {int(page)}/{total_pages})")
            displayed_rows = queue_rows[start_idx:end_idx]
        else:
            displayed_rows = queue_rows

        for row in displayed_rows:
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
            file_name = Path(row['file_path']).name[:30]
            expander_title = f"{icon} #{row['id']} - {file_name}{platform_indicator}"

            # Default to collapsed unless user explicitly opened it
            is_expanded = row['id'] in st.session_state.expanded_queue_items

            with st.expander(expander_title, expanded=is_expanded):
                col_info, col_vid = st.columns([1, 1])

                with col_info:
                    st.write(f"**{ui_logic.format_datetime_for_ui(row.get('scheduled_for'))}**")
                    st.write(f"Status: {row['status']}")

                    # Show custom title/description if set
                    if row.get("title") and row.get("title") != _global_title:
                        t = row["title"]
                        st.write(f"Title: {t[:40]}{'...' if len(t) > 40 else ''}")
                    if row.get("description") and row.get("description") != _global_desc:
                        d = row["description"]
                        st.write(f"Desc: {d[:40]}{'...' if len(d) > 40 else ''}")
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
                                logger,
                            )
                    
                    st.markdown("---")

                    schedule_tz = _get_schedule_timezone()
                    current_dt = _as_schedule_datetime(row.get("scheduled_for")) or datetime.now(schedule_tz)

                    st.markdown("**Reschedule**")
                    rs_date_col, rs_time_col = st.columns([1, 1])
                    with rs_date_col:
                        reschedule_date = st.date_input(
                            "Date",
                            value=current_dt.date(),
                            key=f"reschedule_date_{row['id']}",
                        )
                    with rs_time_col:
                        reschedule_time = st.time_input(
                            "Time",
                            value=current_dt.time().replace(second=0, microsecond=0),
                            key=f"reschedule_time_{row['id']}",
                        )

                    ac1, ac2, ac3 = st.columns(3)
                    
                    # Track delete/reschedule state for this row
                    delete_key = f"del_{row['id']}"
                    reschedule_key = f"rsc_{row['id']}"
                    next_slot_key = f"next_{row['id']}"
                    is_deleting = st.session_state.queue_processing.get(delete_key, False)
                    is_rescheduling = st.session_state.queue_processing.get(reschedule_key, False)
                    is_advancing = st.session_state.queue_processing.get(next_slot_key, False)
                    
                    if ac1.button("Delete", key=delete_key, disabled=is_deleting):
                        st.session_state.queue_processing[delete_key] = True
                        try:
                            delete_from_queue(row["id"])
                            fp = Path(row["file_path"])
                            if fp.exists():
                                fp.unlink(missing_ok=True)
                            logger.info("Deleted queue item #%s from queue list", row["id"])
                            # Clear cache to show updated queue
                            st.cache_data.clear()
                            st.success(f"✓ Deleted #{row['id']}")
                        except Exception as e:
                            logger.error("Failed to delete queue item #%s: %s", row["id"], e, exc_info=True)
                            st.error("Failed to delete. Please try again.")
                        st.session_state.queue_processing[delete_key] = False
                        st.rerun()
                    
                    if ac2.button("Save Time", key=reschedule_key, disabled=is_rescheduling):
                        st.session_state.queue_processing[reschedule_key] = True
                        should_rerun = False
                        try:
                            updated_dt = _combine_schedule_datetime(reschedule_date, reschedule_time)
                            now_in_schedule_tz = datetime.now(schedule_tz)
                            if updated_dt <= now_in_schedule_tz:
                                st.error("Please choose a future date and time.")
                            else:
                                reschedule_queue_item(row["id"], updated_dt.isoformat())
                                logger.info("Manually rescheduled queue item #%s to %s", row["id"], updated_dt.isoformat())
                                st.cache_data.clear()
                                st.success("✓ Schedule updated!")
                                should_rerun = True
                        except Exception as e:
                            logger.error("Failed to reschedule item #%s: %s", row["id"], e, exc_info=True)
                            st.error("Failed to reschedule. Please try again.")
                        st.session_state.queue_processing[reschedule_key] = False
                        if should_rerun:
                            st.rerun()

                    if ac3.button("Next Slot", key=next_slot_key, disabled=is_advancing):
                        st.session_state.queue_processing[next_slot_key] = True
                        should_rerun = False
                        try:
                            anchor = ui_logic.parse_iso(row.get("scheduled_for")) or ui_logic.get_schedule_start_time(queue_rows)
                            occupied = ui_logic.occupied_schedule_dates(queue_rows)
                            curr_dt = _as_schedule_datetime(row.get("scheduled_for"))
                            if curr_dt:
                                occupied.discard(curr_dt.date().isoformat())
                            future = next_daily_slots(1, start=anchor, occupied_dates=occupied)
                            if future:
                                reschedule_queue_item(row["id"], future[0].isoformat())
                                logger.info("Moved queue item #%s to next slot %s", row["id"], future[0].isoformat())
                                st.cache_data.clear()
                                st.success("✓ Moved to next slot!")
                                should_rerun = True
                            else:
                                st.warning("No available schedule slots found")
                        except Exception as e:
                            logger.error("Failed to auto-reschedule item #%s: %s", row["id"], e, exc_info=True)
                            st.error("Failed to move item. Please try again.")
                        st.session_state.queue_processing[next_slot_key] = False
                        if should_rerun:
                            st.rerun()
                
                with col_vid:
                    # Only load video if user wants to see it (performance optimization)
                    if st.checkbox("Show video preview", key=f"preview_{row['id']}", value=False):
                        file_path = Path(row["file_path"])
                        if file_path.exists():
                            try:
                                # Use absolute path for better compatibility
                                abs_path = file_path.resolve()
                                st.video(str(abs_path), start_time=0)
                            except Exception as e:
                                logger.warning("Failed to render video for queue item #%s: %s", row["id"], e)
                                st.warning("⚠ Video preview unavailable")
                                # Show file info as fallback
                                try:
                                    file_size_mb = file_path.stat().st_size / (1024 * 1024)
                                    st.caption(f"File: {file_path.name} ({file_size_mb:.1f} MB)")
                                except Exception:
                                    st.caption(f"File: {file_path.name}")
                        else:
                            logger.warning("File missing for queue item #%s: %s", row["id"], row["file_path"])
                            st.error("❌ File missing")
                    else:
                        # Show file info without loading video
                        file_path = Path(row["file_path"])
                        if file_path.exists():
                            try:
                                file_size_mb = file_path.stat().st_size / (1024 * 1024)
                                st.info(f"📹 {file_path.name}\n\n{file_size_mb:.1f} MB")
                            except Exception:
                                st.info(f"📹 {file_path.name}")
                        else:
                            st.error("❌ File missing")
    else:
        st.info("No videos in queue")

    # Render archived uploads section below the queue
    render_archived_uploads(uploaded_rows, logger)


def render_archived_platform_row(archive_id: int, platform_key: str, label: str, log_value, logger):
    """Render status and restore button for a single platform in archived uploads."""
    logs = _parse_platform_logs(log_value)
    status_text = logs.get(platform_key, "")

    # Determine status
    is_success = "success" in str(status_text).lower() or "uploaded" in str(status_text).lower() or "id:" in str(status_text).lower()
    is_failed = status_text and not is_success

    # Show status
    if is_success:
        st.success(f"✓ {label}: Success")
    elif is_failed:
        truncated = str(status_text)
        suffix = "..." if len(truncated) > 40 else ""
        st.error(f"✗ {label}: {truncated[:40]}{suffix}")
    else:
        st.info(f"○ {label}: Not attempted")

    # Restore button ONLY for failed platforms (not for pending/not attempted)
    if is_failed:
        restore_key = f"restore_force_{archive_id}_{platform_key}"
        is_restoring = st.session_state.queue_processing.get(restore_key, False)
        if st.button(f"Restore & Force {label}", key=restore_key, type="primary", disabled=is_restoring):
            st.session_state.queue_processing[restore_key] = True
            with st.spinner(f"Restoring and queuing {label} upload..."):
                try:
                    # Restore to queue - returns the new queue item ID
                    restored_id = restore_archived_to_queue(archive_id)
                    if restored_id > 0:
                        # Clear the platform status and set to retry
                        cleared = clear_platform_status(restored_id, platform_key)
                        if cleared:
                            from src.database import get_queue_item
                            row = get_queue_item(restored_id)
                            current_logs = _parse_platform_logs(row.get("platform_logs")) if row else {}
                            update_queue_status(restored_id, "retry", None, current_logs)

                            # Set force flag for this specific queue item and platform
                            set_config(FORCE_KEY, 1)
                            set_config(FORCE_PLATFORM_KEY, platform_key)
                            set_config(FORCE_QUEUE_ID_KEY, restored_id)

                            logger.info("Restored archive #%s as queue #%s and queued force upload for platform: %s", archive_id, restored_id, label)
                            st.cache_data.clear()
                            st.success(f"✓ Restored to Queue!")
                            st.session_state.queue_processing[restore_key] = False
                            st.rerun()
                        else:
                            logger.warning("Restored but failed to clear platform status for archive #%s, platform: %s", archive_id, label)
                            st.error("Restored but failed to queue force upload")
                            st.session_state.queue_processing[restore_key] = False
                    else:
                        st.error("Failed to restore. Item may not exist.")
                        st.session_state.queue_processing[restore_key] = False
                except Exception as e:
                    logger.error("Failed to restore archive #%s: %s", archive_id, e, exc_info=True)
                    st.error("Failed to restore. Please try again.")
                    st.session_state.queue_processing[restore_key] = False


def render_archived_row(upload_row: dict, platforms: dict, logger):
    """Render a single archived upload item with restore capability."""
    # Parse platform logs to check for failures
    logs = _parse_platform_logs(upload_row.get("platform_logs"))

    # Check which platforms failed
    has_failures = False
    failed_platforms = []
    success_platforms = []

    for pkey, pcfg in platforms.items():
        status = logs.get(pkey, "")
        if status:
            is_success = "success" in str(status).lower() or "uploaded" in str(status).lower() or "id:" in str(status).lower()
            if is_success:
                success_platforms.append(pcfg["label"])
            else:
                has_failures = True
                failed_platforms.append(pcfg["label"])

    # Build title
    file_name = Path(upload_row['file_path']).name[:30]
    status_icon = "⚠" if has_failures else "✓"
    expander_title = f"{status_icon} Archive #{upload_row['id']} - {file_name}"

    with st.expander(expander_title, expanded=False):
        col_info, col_vid = st.columns([1, 1])

        with col_info:
            st.write(f"**Uploaded:** {ui_logic.format_datetime_for_ui(upload_row.get('uploaded_at'))}")

            # Show custom title/description if set
            if upload_row.get("title"):
                t = upload_row["title"]
                st.write(f"Title: {t[:40]}{'...' if len(t) > 40 else ''}")
            if upload_row.get("description"):
                d = upload_row["description"]
                st.write(f"Desc: {d[:40]}{'...' if len(d) > 40 else ''}")

            # Platform status section with restore buttons
            st.markdown("**Platforms:**")

            for pkey in platforms.keys():
                if pkey in platforms:
                    pcfg = platforms[pkey]
                    render_archived_platform_row(
                        upload_row["id"],
                        pkey,
                        pcfg["label"],
                        upload_row.get("platform_logs"),
                        logger,
                    )

            st.markdown("---")

            # Delete archive button with disable state
            delete_archive_key = f"del_archive_{upload_row['id']}"
            is_deleting = st.session_state.queue_processing.get(delete_archive_key, False)
            
            if st.button("Delete Archive", key=delete_archive_key, type="secondary", disabled=is_deleting):
                st.session_state.queue_processing[delete_archive_key] = True
                try:
                    delete_uploaded_item(upload_row["id"])
                    fp = Path(upload_row["file_path"])
                    if fp.exists():
                        fp.unlink(missing_ok=True)
                    logger.info("Deleted archive #%s", upload_row["id"])
                    st.success(f"✓ Deleted archive #{upload_row['id']}")
                    st.cache_data.clear()
                except Exception as e:
                    logger.error("Failed to delete archive #%s: %s", upload_row["id"], e, exc_info=True)
                    st.error("Failed to delete. Please try again.")
                st.session_state.queue_processing[delete_archive_key] = False
                st.rerun()

        with col_vid:
            # Show video preview option
            if st.checkbox("Show video preview", key=f"preview_archive_{upload_row['id']}", value=False):
                file_path = Path(upload_row["file_path"])
                if file_path.exists():
                    try:
                        abs_path = file_path.resolve()
                        st.video(str(abs_path), start_time=0)
                    except Exception as e:
                        logger.warning("Failed to render video for archive #%s: %s", upload_row["id"], e)
                        st.warning("⚠ Video preview unavailable")
                else:
                    st.error("❌ File missing")
            else:
                file_path = Path(upload_row["file_path"])
                if file_path.exists():
                    try:
                        file_size_mb = file_path.stat().st_size / (1024 * 1024)
                        st.info(f"📹 {file_path.name}\n\n{file_size_mb:.1f} MB")
                    except Exception:
                        st.info(f"📹 {file_path.name}")
                else:
                    st.error("❌ File missing")


def render_archived_uploads(uploaded_rows, logger):
    """Render archived uploads section with ability to restore failed ones."""
    if not uploaded_rows:
        return

    st.markdown("---")
    st.markdown("### **Archived Uploads**")
    st.caption("Recently completed uploads (most recent first)")

    platforms = get_platforms()

    # Show only first 10 by default
    display_limit = 10
    displayed_archived = uploaded_rows[:display_limit]

    for upload_row in displayed_archived:
        render_archived_row(upload_row, platforms, logger)
