"""Activity log and error tracker."""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from core.storage import read_csv, write_csv
from views.common import config, store


def render_log():
    cfg, s = config(), store()
    st.title("📂 Activity log")
    df = read_csv(s.log)
    if df.empty:
        st.info("No activity recorded yet.")
        return
    who = st.selectbox("Operator", ["All"] + sorted(set(cfg.operators) | set(df["Operator"].dropna().astype(str))))
    if who != "All":
        df = df[df["Operator"].astype(str) == who]
    st.caption(f"{len(df)} records · stored in {s.dir}")
    st.dataframe(df.iloc[::-1], width="stretch", height=520, hide_index=True)
    st.download_button("📥 Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                       f"activity_log_{datetime.now():%Y%m%d_%H%M%S}.csv", "text/csv")


def render_errors():
    s = store()
    st.title("🚨 Error tracker")
    st.caption("Reports that failed, were only partly converted or need a manual check.")
    df = read_csv(s.errors)
    if df.empty:
        st.success("Nothing to follow up. 🎉")
        return
    df["Checked"] = df["Checked"].fillna(False).astype(bool)
    st.warning(f"{int((~df['Checked']).sum())} of {len(df)} reports still need a check.")
    edited = st.data_editor(
        df, hide_index=True, width="stretch",
        column_config={"Checked": st.column_config.CheckboxColumn("Checked", help="Tick once verified")},
        disabled=[c for c in df.columns if c != "Checked"],
    )
    if st.button("💾 Save"):
        write_csv(s.errors, edited)
        st.success("Saved.")
        st.rerun()
    st.download_button("📥 Download CSV", edited.to_csv(index=False).encode("utf-8-sig"),
                       f"error_tracker_{datetime.now():%Y%m%d_%H%M%S}.csv", "text/csv")
