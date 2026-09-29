"""Entry point: Loop Debug Console + the live "Run loop" page.

    streamlit run app/streamlit_app.py
"""
import streamlit as st

st.set_page_config(page_title="Loop Debug Console", page_icon=":material/autorenew:", layout="wide")

page = st.navigation([
    st.Page("dashboard.py", title="Loop Debug Console", icon=":material/monitoring:", default=True),
    st.Page("app_pages/run_loop.py", title="运行 Loop", icon=":material/play_circle:"),
], position="top")
page.run()
