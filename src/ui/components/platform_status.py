import streamlit as st
from src.platform_registry import all_platform_statuses, get_platforms
from src.platforms import tiktok as tiktok_platform
from src.platforms import instagram as instagram_platform
from src.auth_utils import verify_youtube_credentials


def refresh_platform_statuses(logger):
    """Check platform connectivity."""
    try:
        verify_youtube_credentials(probe_api=True)
        logger.debug("YouTube credentials verified during refresh")
    except Exception as e:
        logger.warning("YouTube credentials verification failed during refresh: %s", e)
    try:
        instagram_platform.verify_login()
        logger.debug("Instagram login verified during refresh")
    except Exception as e:
        logger.warning("Instagram login verification failed during refresh: %s", e)
    try:
        tiktok_platform.verify_session(force=True)
        logger.debug("TikTok session verified during refresh")
    except Exception as e:
        logger.warning("TikTok session verification failed during refresh: %s", e)


def render_platform_status(logger):
    """Render platform connections - minimal row."""
    st.markdown("### **Platforms**")
    
    statuses = all_platform_statuses()
    registry = get_platforms()
    
    # Log overall platform statuses
    connected_count = 0
    for key, cfg in registry.items():
        state = statuses.get(key, {})
        live_connected = cfg["connected"]()
        state_connected = bool(state.get("connected"))
        connected = live_connected and state_connected
        if connected:
            connected_count += 1
    logger.debug("Platform statuses: %d/%d connected", connected_count, len(registry))
    
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
            refresh_platform_statuses(logger)
        logger.info("Platform statuses refreshed by user")
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)
