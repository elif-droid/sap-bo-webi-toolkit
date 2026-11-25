"""Row-level security check after conversion (read-only)."""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import streamlit as st

from bo import documents as docs
from bo.client import BOError, bo_session
from bo.rls import check_user, pick_column
from core.config import ROOT
from core.storage import append_csv, read_csv
from views.common import config, secret_input, server_input, store

EXAMPLE_RULES = ROOT / "examples" / "rls_rules.example.json"


def _rules() -> dict:
    s = store()
    if "rls_rules" not in st.session_state:
        st.session_state["rls_rules"] = s.load_json(s.rls_rules, None) or s.load_json(EXAMPLE_RULES, {})
    return st.session_state["rls_rules"]


def render():
    cfg, s = config(), store()
    st.title("🔐 Row-level security check")
    st.caption("Logs on as each test user, refreshes the report and checks that the user only sees the rows "
               "their rules allow. Nothing is written to the report. Passwords are kept only for this browser tab.")

    rules = _rules()
    with st.expander("📋 Rules per test user (JSON)"):
        st.caption('Format: {"USER": {"brand": {"allow": [...], "deny": [...]}, "region": {...}}}. '
                   "allow = every value must contain one of these; deny = no value may contain these.")
        text = st.text_area("Rules", json.dumps(rules, indent=2, ensure_ascii=False), height=260)
        if st.button("💾 Save rules"):
            try:
                st.session_state["rls_rules"] = rules = json.loads(text)
                s.save_json(s.rls_rules, rules)
                st.success("Saved.")
            except ValueError as e:
                st.error(f"Invalid JSON: {e}")

    candidates = cfg.rls_columns or {"brand": ["Brand"], "region": ["Region"]}
    st.caption("Column names per dimension (from config.toml `[rls.columns]`): " +
               "; ".join(f"**{d}**: {', '.join(c)}" for d, c in candidates.items()))

    st.divider()
    server = server_input()
    log = read_csv(s.log)
    today = datetime.now().strftime("%Y-%m-%d")
    if not log.empty and st.button("📅 Use reports converted today"):
        done = log[log["Timestamp"].astype(str).str.startswith(today)
                   & log["Result"].astype(str).str.contains("SUCCESS|PARTIAL", na=False)]
        st.session_state["rls_ids"] = "\n".join(dict.fromkeys(done["Report ID"].astype(str)))
    ids_text = st.text_area("Report ids", value=st.session_state.get("rls_ids", ""), height=90)
    st.session_state["rls_ids"] = ids_text
    c1, c2 = st.columns(2)
    date_start = c1.text_input("Date prompt start", cfg.date_start,
                               help="Pick a range that returns data — an empty report cannot be checked.")
    date_end = c2.text_input("Date prompt end", cfg.date_end)

    with st.expander("🔍 Preview columns with one user"):
        p1, p2 = st.columns(2)
        probe_user = p1.text_input("Username", key="rls_probe_user")
        with p2:
            probe_pwd = secret_input("Password", "rls_probe_pwd")
        if st.button("Read columns"):
            rid = next((x.strip() for x in ids_text.splitlines() if x.strip()), "")
            try:
                with bo_session(server, probe_user, probe_pwd) as h:
                    docs.refresh_and_count(server, h, rid, date_start, date_end, end_keywords=cfg.date_end_keywords)
                    cols, rows, _, err = docs.fetch_rows(server, h, rid)
                if err and not cols:
                    st.warning(err)
                else:
                    st.success(f"{len(cols)} columns, {len(rows)} rows.")
                    st.caption("Detected: " + "; ".join(f"{d} → {pick_column(cols, c) or '—'}"
                                                         for d, c in candidates.items()))
                    st.dataframe(pd.DataFrame(rows[:20]), width="stretch")
            except BOError as e:
                st.error(str(e))

    users = st.multiselect("Test users", list(rules), default=list(rules))
    passwords = {u: secret_input(f"Password for {u}", f"rls_pwd_{u}") for u in users}

    if st.button("🔐 Run check", type="primary"):
        rids = [x.strip() for x in ids_text.splitlines() if x.strip()]
        if not rids or not users:
            st.error("Enter at least one report id and choose at least one user.")
            return
        results, bar, n = [], st.progress(0.0), 0
        for rid in rids:
            for u in users:
                n += 1
                bar.progress(n / (len(rids) * len(users)))
                row = {"Report ID": rid, "User": u}
                if not passwords.get(u):
                    results.append({**row, "Status": "⏭️ SKIPPED (no password)", "Detail": ""})
                    continue
                try:
                    with bo_session(server, u, passwords[u]) as h:
                        total, d = docs.refresh_and_count(server, h, rid, date_start, date_end,
                                                          end_keywords=cfg.date_end_keywords)
                        if total is None:
                            results.append({**row, "Status": "🚫 NO ACCESS", "Detail": d})
                            continue
                        cols, data, _, err = docs.fetch_rows(server, h, rid)
                except BOError as e:
                    results.append({**row, "Status": "🔒 LOGON FAILED", "Detail": str(e)})
                    continue
                if err and not data:
                    results.append({**row, "Status": "❔ NO DATA", "Detail": err})
                    continue
                violations, used = check_user(data, cols, rules.get(u, {}), candidates)
                used_txt = ", ".join(f"{k}: {v or '—'}" for k, v in used.items())
                if not any(used.values()):
                    results.append({**row, "Status": "❔ COLUMNS NOT FOUND", "Detail": f"columns: {', '.join(cols)}"})
                elif violations:
                    results.append({**row, "Status": "⚠️ VIOLATION", "Detail": f"[{used_txt}] " + " | ".join(violations)})
                else:
                    results.append({**row, "Status": "✅ OK", "Detail": f"[{used_txt}] {len(data)} rows checked"})

        df = pd.DataFrame(results)
        st.dataframe(df, width="stretch", hide_index=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in results:
            append_csv(s.rls_results, {"Timestamp": ts, **r})
        (st.error if (df["Status"] == "⚠️ VIOLATION").any() else st.success)(
            "Violations found — see the table." if (df["Status"] == "⚠️ VIOLATION").any() else "No violations.")

    st.divider()
    st.subheader("🗂️ Previous checks")
    hist = read_csv(s.rls_results)
    if hist.empty:
        st.caption("None yet.")
    else:
        if st.checkbox("Violations only"):
            hist = hist[hist["Status"] == "⚠️ VIOLATION"]
        st.dataframe(hist.iloc[::-1], width="stretch", hide_index=True)
