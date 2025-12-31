import streamlit as st
from src.logging_utils import get_log_file_path, tail_log
from src.log_display import parse_log_data


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
