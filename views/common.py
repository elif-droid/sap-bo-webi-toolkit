"""Shared Streamlit helpers: config/store access, credential inputs, sessions, tables."""

from __future__ import annotations

import io
from contextlib import contextmanager
from datetime import datetime

import pandas as pd
import streamlit as st

from bo.client import BOError, bo_session
from bo.text import norm
from core.config import load_config
from core.storage import Store


@st.cache_resource
def config():
    return load_config()


@st.cache_resource
def store() -> Store:
    return Store(config().data_dir)


def keep(key: str, default=""):
    """Value that survives page switches (Streamlit drops widget state of pages not shown)."""
    return st.session_state.setdefault(f"keep_{key}", default)


# ---------------------------------------------------------------------------
# Credentials: entered once per browser tab, shared by every page
# ---------------------------------------------------------------------------
def _persistent_input(label: str, key: str, default: str = "", password: bool = False) -> str:
    store_key, widget_key = f"cred_{key}", f"cred_{key}_w"
    st.session_state.setdefault(store_key, default)
    st.session_state.setdefault(widget_key, st.session_state[store_key])

    def sync():
        st.session_state[store_key] = st.session_state[widget_key]

    value = st.text_input(label, key=widget_key, type="password" if password else "default", on_change=sync)
    st.session_state[store_key] = value
    return value


def server_input() -> str:
    return _persistent_input("BO server URL (…/biprws)", "server", config().server_url).rstrip("/")


def credentials(show_server: bool = True) -> tuple[str, str, str]:
    server = server_input() if show_server else st.session_state.get("cred_server", config().server_url).rstrip("/")
    c1, c2 = st.columns(2)
    with c1:
        user = _persistent_input("BO username", "user")
    with c2:
        pwd = _persistent_input("BO password", "pass", password=True)
    return server, user, pwd


def secret_input(label: str, key: str) -> str:
    return _persistent_input(label, key, password=True)


def clear_credentials():
    for k in list(st.session_state):
        if k.startswith("cred_") and not k.startswith("cred_server"):
            st.session_state.pop(k)


@contextmanager
def session(server: str, user: str, pwd: str):
    """Opens a BO session or shows an error. Usage:

        with session(s, u, p) as h:
            if h: ...
    """
    if not user or not pwd:
        st.error("Enter the BO username and password first.")
        yield None
        return
    try:
        with bo_session(server, user, pwd) as headers:
            yield headers
    except BOError as e:
        st.error(f"Could not log on: {e}")
        yield None


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
def search_df(df: pd.DataFrame, columns: list[str], query: str) -> pd.DataFrame:
    q = norm(query) if query and query.strip() else ""
    if not q or df.empty:
        return df
    mask = False
    for c in columns:
        mask = mask | df[c].astype(str).map(norm).str.contains(q, regex=False)
    return df[mask]


def download_table(df: pd.DataFrame, base_name: str, key: str, label: str = "📥 Download Excel"):
    """Excel with auto-filter and a frozen header; CSV when openpyxl is missing."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        from openpyxl.utils import get_column_letter
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            df.to_excel(xw, index=False, sheet_name="Data")
            ws = xw.sheets["Data"]
            ws.auto_filter.ref = ws.dimensions
            ws.freeze_panes = "A2"
            for i, col in enumerate(df.columns, 1):
                width = max([len(str(col))] + [len(str(v)) for v in df[col].head(300)]) + 2
                ws.column_dimensions[get_column_letter(i)].width = min(width, 60)
        kwargs = dict(data=buf.getvalue(), file_name=f"{base_name}_{stamp}.xlsx",
                      mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except ImportError:
        kwargs = dict(data=df.to_csv(index=False).encode("utf-8-sig"), file_name=f"{base_name}_{stamp}.csv",
                      mime="text/csv")
        label = "📥 Download CSV"
    st.download_button(label, key=key, on_click="ignore", **kwargs)


STATUS_COLORS = [  # first match wins
    (("FAILED", "ERROR", "MISMATCH"), "background-color:#fce8e6;color:#c5221f"),
    (("PARTIAL", "CHECK"), "background-color:#fef7e0;color:#b06000"),
    (("WARNING",), "background-color:#fff3e0;color:#a65b00"),
    (("SKIPPED",), "background-color:#eef1f5;color:#444"),
    (("SUCCESS", "OK"), "background-color:#e6f4ea;color:#137333"),
]


def status_style(row) -> list[str]:
    s = str(row.get("Status", "")).upper()
    for words, css in STATUS_COLORS:
        if any(w in s for w in words):
            return [css] * len(row)
    return [""] * len(row)
