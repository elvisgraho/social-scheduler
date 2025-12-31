import streamlit as st
from pathlib import Path
from src.database import get_config, set_config, cleanup_uploaded
from src.scheduling import human_readable_schedule, next_slots
from src import ui_logic


def render_dashboard_tab(queue_rows, uploaded_count: int, logger):
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
        
        if pause_toggle:
            # Queue was paused
            logger.info("Queue paused by user")
        else:
            # Queue was unpaused - reschedule all pending items to next available slots
            logger.info("Queue unpaused by user - rescheduling pending items")
            rescheduled_count, _ = ui_logic.reschedule_pending_items(queue_rows)
            logger.info("Rescheduled %d pending items after unpause", rescheduled_count)
        
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
    used_gb, free_gb, total_gb, percent = ui_logic.get_storage_summary(Path("data"))
    if total_gb is not None:
        st.progress(percent / 100.0)
        st.caption(f"Used {used_gb:.1f}GB / {total_gb:.1f}GB")
        if st.button("Clean Old Uploads", key="clean_uploaded_btn"):
            deleted, freed = cleanup_uploaded(20)
            if deleted:
                st.success(f"Deleted {deleted}, freed {freed/ (1024**2):.1f} MB")
                st.rerun()
