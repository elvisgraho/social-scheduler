import streamlit as st

def render_header():
    """Render minimal header."""
    st.markdown("""
    <div style="text-align: center; padding: 0.5rem 0 0 0;">
        <h1 style="margin: 0; font-size: 1.25rem;">Social Scheduler</h1>
    </div>
    """, unsafe_allow_html=True)
