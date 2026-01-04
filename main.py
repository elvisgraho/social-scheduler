import streamlit as st
from pathlib import Path

# --- Configuration & Init ---
st.set_page_config(
    page_title="Social Scheduler",
    page_icon="⚙",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# Performance optimization: Disable automatic reruns on file changes in production
# This reduces unnecessary re-renders and improves stability
if "initialized" not in st.session_state:
    st.session_state.initialized = True

# Load custom CSS
try:
    with open("assets/style.css", "r") as f:
        st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
except Exception:
    pass

from src.database import init_db
from src.logging_utils import init_logging, log_once

# Initialize
init_db()
logger = init_logging("ui")
log_once(logger, "ui_started", "Streamlit UI started.")

# Import UI components
from src.ui.components.header import render_header
from src.ui.components.platform_status import render_platform_status
from src.ui.components.dashboard import render_dashboard_tab
from src.ui.components.queue import render_queue_tab
from src.ui.components.accounts import render_accounts_tab
from src.ui.components.settings import render_settings_tab
from src.ui.components.logs import render_logs_tab

# Constants
UPLOAD_DIR = Path("data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# --- Main ---

render_header()

# Load data with caching to reduce database calls
from src.database import get_queue, get_uploaded_count, get_uploaded_items

@st.cache_data(ttl=2, show_spinner=False)
def get_queue_cached():
    """Cache queue data for 2 seconds to reduce DB load."""
    try:
        return get_queue()
    except Exception as e:
        logger.error("Failed to fetch queue data: %s", e, exc_info=True)
        return []

@st.cache_data(ttl=5, show_spinner=False)
def get_uploaded_data_cached():
    """Cache uploaded data for 5 seconds."""
    try:
        return get_uploaded_count(), get_uploaded_items(200)
    except Exception as e:
        logger.error("Failed to fetch uploaded data: %s", e, exc_info=True)
        return 0, []

# Load data with error recovery
try:
    queue_data = get_queue_cached()
    uploaded_count, uploaded_rows = get_uploaded_data_cached()
except Exception as e:
    logger.critical("Critical error loading data: %s", e, exc_info=True)
    st.error("Failed to load data. Please refresh the page.")
    queue_data = []
    uploaded_count = 0
    uploaded_rows = []

# Platform status
render_platform_status(logger)

# Tabs
tabs = st.tabs(["Dashboard", "Queue", "Accounts", "Settings", "Logs"])

with tabs[0]:
    render_dashboard_tab(queue_data, uploaded_count, logger)

with tabs[1]:
    render_queue_tab(queue_data, uploaded_rows, UPLOAD_DIR, logger)

with tabs[2]:
    render_accounts_tab(logger)

with tabs[3]:
    render_settings_tab(logger)

with tabs[4]:
    render_logs_tab()

# Footer
st.caption("Social Scheduler  |  Raspberry Pi 5")
