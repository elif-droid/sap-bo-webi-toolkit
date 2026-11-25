"""Undo conversions by restoring the backup copy over the original."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from bo import documents as docs
from bo.mapping import revert_mapping
from core.storage import read_csv
from views.common import credentials, session, store


def render():
    s = store()
    st.title("↩️ Roll back")
    st.caption("The backup copy's content is written over the original report, so its id, name and folder "
               "stay the same. This also works for .unx → .unv. If no backup exists (e.g. 'No copy' mode), "
               "the queries are mapped back to their recorded universes instead, which BO only allows between "
               "universes of the same kind.")

    df = read_csv(s.rollback)
    if df.empty:
        st.info("No conversions recorded yet.")
        return
    df["Reverted"] = df["Reverted"].fillna(False).astype(bool)
    st.dataframe(df, width="stretch", height=280, hide_index=True)

    server, user, pwd = credentials()
    open_rows = df[~df["Reverted"]]
    chosen = st.multiselect("Reports to roll back",
                            [f"{r['Report ID']} — {r['Report name']}" for _, r in open_rows.iterrows()])
    if not st.button("↩️ Restore selected", type="primary") or not chosen:
        return

    results = []
    with session(server, user, pwd) as h:
        if not h:
            return
        for label in chosen:
            rid = label.split(" — ")[0].strip()
            row = open_rows[open_rows["Report ID"].astype(str) == rid].iloc[-1]
            backup_id, bname, err = docs.find_backup(server, h, row["Report name"], rid)
            if backup_id:
                ok, detail = docs.restore_from_backup(server, h, rid, backup_id)
                method = f"Restore from backup ({bname}, ID {backup_id})"
            else:
                try:
                    ok, detail = revert_mapping(server, h, rid, json.loads(row["Previous mapping (JSON)"]))
                    detail = f"{err} → reverse mapping: {detail}"
                except (ValueError, TypeError):
                    ok, detail = False, "rollback record is unreadable"
                method = "Reverse mapping"
            status = "🟢 ROLLED BACK" if ok else "🔴 FAILED"
            results.append({"Report ID": rid, "Report": row["Report name"], "Method": method,
                            "Status": status, "Detail": detail})
            s.log_action(row.get("Operator", "-"), user, rid, row["Report name"], "(rollback)", status, 0, detail)
            if ok:
                s.mark_reverted(rid)
    st.dataframe(pd.DataFrame(results), width="stretch", hide_index=True)
