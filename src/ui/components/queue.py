import json
import streamlit as st
from pathlib import Path
from src.database import clear_platform_status, delete_from_queue, reschedule_queue_item, update_queue_status, get_queue_item, set_config
from src.scheduling import next_daily_slots
from src.platform_registry import get_platforms
from src import ui_logic

FORCE_KEY = "queue_force_run"
FORCE_PLATFORM_KEY = "queue_force_platform"


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
                st.write("\n".join(s.l.strftime("%b %d %H:%M") for s in slots))
            
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
