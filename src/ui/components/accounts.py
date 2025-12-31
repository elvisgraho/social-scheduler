import streamlit as st
from urllib.parse import unquote
from src.auth_utils import (
    finish_google_auth,
    get_google_auth_url,
    get_google_client_config,
    has_google_client_config,
    save_google_client_config,
    verify_youtube_credentials,
    youtube_connected,
    set_config as auth_set_config,
    set_account_state,
)
from src.database import get_config, set_config
from src.platforms import instagram as instagram_platform
from src.platforms import tiktok as tiktok_platform
from src import ui_logic


def render_accounts_tab(logger):
    """Render platform accounts."""
    
    # YouTube
    st.markdown("### **YouTube**")
    google_config_present = has_google_client_config()
    
    with st.expander("OAuth client JSON (Desktop app)", expanded=not google_config_present):
        st.caption(
            "1. Google Cloud Console -> Create OAuth Client ID (Desktop app).\n"
            "2. Download JSON and paste below."
        )
        google_json = st.text_area(
            "Google client JSON",
            value=get_google_client_config(pretty=True) or "",
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
                    logger.warning("Failed to save Google OAuth JSON: %s", msg)
                    st.error(msg)
        
        yt_connected = youtube_connected()
        if google_config_present:
            if not yt_connected:
                auth_url, err = get_google_auth_url()
                if not err:
                    st.link_button("Connect YouTube", auth_url)
                    yt_code = st.text_input("Auth code", key="yt_code")
                    if st.button("Finish", key="yt_finish_btn", type="primary"):
                        ok, message = finish_google_auth(yt_code.strip())
                        if ok:
                            logger.info("YouTube authentication successful")
                            st.success("Connected!")
                            st.rerun()
                        else:
                            logger.warning("YouTube authentication failed: %s", message)
                            st.error(message)
            else:
                st.success("YouTube connected!")
                c1, c2 = st.columns(2)
                if c1.button("Verify", key="yt_verify_btn"):
                    ok, msg = verify_youtube_credentials(probe_api=False)
                    logger.info("YouTube verification: %s - %s", ok, msg)
                    st.success(msg) if ok else st.error(msg)
                if c2.button("Disconnect", key="yt_disconnect_btn"):
                    logger.info("YouTube disconnected by user")
                    auth_set_config("youtube_credentials", "")
                    set_account_state("youtube", False, "")
                    st.rerun()
    
    # Instagram
    st.markdown("### **Instagram**")
    
    with st.expander("Instagram"):
        # Session ID input
        st.caption("Session ID (from browser cookies)")
        ig_session = st.text_area("Session ID", value=get_config("insta_sessionid", ""), height=60, key="ig_session")
        
        # Username/Password input
        st.markdown("---")
        st.caption("Or login with username/password")
        ig_user = st.text_input("Username", value=get_config("insta_user", ""), key="ig_user")
        ig_pass = st.text_input("Password", type="password", key="ig_pass")
        
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Save Session", key="ig_save_btn"):
                ok, msg = instagram_platform.save_sessionid(ig_session)
                logger.info("Instagram session save: %s - %s", ok, msg)
                st.success(msg) if ok else st.error(msg)
                st.rerun()
        with c2:
            if st.button("Save Credentials", key="ig_creds_btn"):
                set_config("insta_user", ig_user)
                set_config("insta_pass", ig_pass)
                if ig_user and ig_pass:
                    logger.info("Instagram credentials saved")
                    st.success("Credentials saved!")
                else:
                    logger.info("Instagram credentials cleared")
                    st.info("Credentials cleared.")
                st.rerun()
        with c3:
            if st.button("Verify", key="ig_verify_btn"):
                ok, msg = instagram_platform.verify_login()
                logger.info("Instagram verification: %s - %s", ok, msg)
                st.success(msg) if ok else st.error(msg)
    
    # TikTok
    st.markdown("### **TikTok**")
    
    tt_status = tiktok_platform.session_status()
    
    if tt_status["valid"]:
        st.success(f"@{tt_status.get('account_name', 'user')}")
        logger.info("TikTok connected: @%s", tt_status.get('account_name', 'user'))
    elif tt_status["sessionid"]:
        st.error(tt_status.get('message', 'Invalid'))
        logger.warning("TikTok session invalid: %s", tt_status.get('message', 'Invalid'))
    else:
        st.warning("No session")
        logger.debug("TikTok: no session configured")
    
    with st.form("tiktok_form"):
        st.caption("Paste sessionid")
        tt_input = st.text_area("Session", value=get_config("tiktok_sessionid", ""), height=60, key="tt_session")
        if st.form_submit_button("Save", key="tt_save_btn", type="primary"):
            raw_input = tt_input.strip()
            if "%" in raw_input:
                try:
                    raw_input = unquote(raw_input)
                except:
                    pass
            clean_session = ui_logic.extract_tiktok_session(raw_input)
            if clean_session:
                tiktok_platform.save_session(clean_session)
                tiktok_platform.verify_session(force=True)
                logger.info("TikTok session saved")
                st.success("Saved!")
                st.rerun()
            else:
                logger.warning("No sessionid found in TikTok input")
                st.warning("No sessionid found")
    
    c1, c2 = st.columns(2)
    if c1.button("Verify", key="tt_verify_btn"):
        ok, msg = tiktok_platform.verify_session(force=True)
        logger.info("TikTok verification: %s - %s", ok, msg)
        st.success(msg) if ok else st.error(msg)
        st.rerun()
    if c2.button("Clear", key="tt_clear_btn"):
        logger.info("TikTok session cleared by user")
        tiktok_platform.save_session("")
        st.rerun()
