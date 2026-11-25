"""SAP BO Webi Toolkit — Streamlit entry point.

Run with:  streamlit run app.py
"""

from pathlib import Path

import streamlit as st

from core.storage import read_csv
from views import (common, convert, explorer, history, inventory, pending, rls, rollback, security,
                   sql_pull)

st.set_page_config(page_title="SAP BO Webi Toolkit", page_icon="🔄", layout="wide")
st.markdown(f"<style>{(Path(__file__).parent / 'assets' / 'style.css').read_text()}</style>",
            unsafe_allow_html=True)

pages = {
    "Migration": [
        st.Page(convert.render, title="Convert reports", icon="🔄", url_path="convert", default=True),
        st.Page(pending.render, title="Pending conversions", icon="⏳", url_path="pending"),
        st.Page(rls.render, title="Row-level security check", icon="🔐", url_path="rls"),
        st.Page(rollback.render, title="Roll back", icon="↩️", url_path="rollback"),
    ],
    "Audit trail": [
        st.Page(history.render_log, title="Activity log", icon="📂", url_path="log"),
        st.Page(history.render_errors, title="Error tracker", icon="🚨", url_path="errors"),
    ],
    "Analysis": [
        st.Page(inventory.render, title="Inventory", icon="📊", url_path="inventory"),
        st.Page(security.render, title="Security audit", icon="🛡️", url_path="security"),
        st.Page(sql_pull.render, title="Report SQL", icon="🧾", url_path="sql"),
        st.Page(explorer.render, title="API explorer", icon="🔧", url_path="api"),
    ],
}
nav = st.navigation(pages)

cfg, store = common.config(), common.store()
with st.sidebar:
    waiting = read_csv(store.pending)
    if not waiting.empty:
        st.markdown("**⏳ Waiting for universes**")
        for name, n in waiting.groupby("Waiting for universe")["Report ID"].nunique().items():
            st.caption(f"• {name}: {n} report(s)")
    st.divider()
    if st.session_state.get("cred_user"):
        st.caption(f"🔐 Signed in as **{st.session_state['cred_user']}** for this tab")
    if st.button("🚪 Forget credentials"):
        common.clear_credentials()
        st.rerun()
    st.caption(f"⚙️ Config: `{cfg.source}`  \n🗂️ Data: `{store.dir}`")
    if cfg.source != "config.toml":
        st.info("Using example settings. Copy `config.example.toml` to `config.toml` and fill in your server.")

nav.run()
