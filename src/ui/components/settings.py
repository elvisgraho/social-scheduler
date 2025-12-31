import json
import streamlit as st
import pytz
from src.database import get_config, set_config, export_config, import_config
from src.scheduling import get_schedule, save_schedule
from src.notifier import send_telegram_message, telegram_enabled


def render_settings_tab(logger):
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
            logger.info("Schedule saved: days=%s, times=%s, timezone=%s", p_days, p_times, timezone)
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
            logger.info("Default metadata saved: title='%s', desc='%s...'", title, desc[:50])
            st.success("Saved!")
    
    # Telegram
    st.markdown("### **Telegram**")
    with st.form("telegram_form"):
        bot_token = st.text_input("Bot Token", value=get_config("telegram_bot_token", ""), type="password")
        chat_id = st.text_input("Chat ID", value=get_config("telegram_chat_id", ""))
        if st.form_submit_button("Save", key="telegram_save_btn"):
            set_config("telegram_bot_token", bot_token)
            set_config("telegram_chat_id", chat_id)
            logger.info("Telegram settings saved: bot_token_set=%s, chat_id=%s", bool(bot_token), chat_id)
            st.success("Saved!")
    
    if telegram_enabled() and st.button("Test", key="telegram_test_btn"):
        send_telegram_message("Test!")
        logger.info("Telegram test message sent")
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
                logger.info("Config restored: %d settings, %d accounts", s, a)
                st.success(f"Restored {s} settings, {a} accounts")
                st.rerun()
            except Exception as e:
                logger.error("Config restore failed: %s", e)
                st.error(f"Error: {e}")
