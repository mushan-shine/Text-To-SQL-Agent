"""Entry point: Ask (product), Review (outer-loop approvals), Loop Debug Console, Run loop (evaluation).

    streamlit run app/streamlit_app.py
"""
import streamlit as st

st.set_page_config(page_title="Self-healing Text2SQL", page_icon=":material/autorenew:", layout="wide")

page = st.navigation([
    st.Page("app_pages/ask.py", title="提问", icon=":material/chat:", default=True),
    st.Page("app_pages/review.py", title="审核", icon=":material/fact_check:"),
    st.Page("dashboard.py", title="Loop Debug Console", icon=":material/monitoring:"),
    st.Page("app_pages/run_loop.py", title="运行 Loop", icon=":material/play_circle:"),
], position="top")
page.run()
