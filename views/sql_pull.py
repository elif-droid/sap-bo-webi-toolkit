"""Pull the SQL of reports by name and search across it (read-only)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from bo import documents as docs
from bo.sqltools import search
from views.common import credentials, session


def render():
    st.title("📊 Report SQL")
    st.caption("Paste report names (one per line). The SQL of every data provider is read from BO; "
               "nothing is changed.")
    server, user, pwd = credentials()
    names_text = st.text_area("Exact report names", height=150, placeholder="Monthly Sales Overview\nOpen Orders")

    if st.button("🔎 Get SQL", type="primary"):
        names = list(dict.fromkeys(x.strip() for x in names_text.splitlines() if x.strip()))
        if not names:
            st.error("Enter at least one report name.")
        else:
            with session(server, user, pwd) as h:
                if h:
                    results, bar = [], st.progress(0.0)
                    for i, name in enumerate(names, 1):
                        found = docs.find_reports_by_name(server, h, name, with_path=True)
                        if not found:
                            results.append({"name": name, "status": "❌ not found", "dps": []})
                        else:
                            doc = found[0]
                            dps, err = docs.get_dataproviders(server, h, doc["id"])
                            out = [{"name": "-", "sql": f"-- {err}", "source": "-"}] if err else []
                            for dp in dps:
                                sql, serr, src = docs.get_dataprovider_sql_robust(server, h, doc["id"], dp.get("id"))
                                out.append({"name": dp.get("name", dp.get("id")), "source": src or "-",
                                            "sql": sql or f"-- SQL not available: {serr}"})
                            status = f"✅ ID {doc['id']}"
                            if len(found) > 1:
                                status += f" ⚠️ {len(found)} reports with this name (used: {doc.get('path', '')})"
                            results.append({"name": name, "status": status, "dps": out})
                        bar.progress(i / len(names))
                    st.session_state["sql_results"] = results

    results = st.session_state.get("sql_results", [])
    if not results:
        return

    st.subheader("🔍 Search the SQL")
    st.caption('Words are matched accent- and case-insensitively; "quoted phrases" stay together; '
               "underscores count as spaces.")
    c1, c2 = st.columns([4, 1])
    q = c1.text_input("Search", label_visibility="collapsed", placeholder='e.g. "customer id" or order_date')
    mode = c2.radio("Match", ["all", "any"], horizontal=True, label_visibility="collapsed")
    if q.strip():
        terms, hits = search(results, q, mode)
        if not hits:
            st.warning(f"No matches for: {', '.join(terms)}")
        else:
            st.success(f"{len(hits)} queries match: {', '.join(terms)}")
            st.dataframe(pd.DataFrame([{"Report": h["report"], "Data provider": h["dp"], "Hits": h["count"]}
                                       for h in hits]), width="stretch", hide_index=True)
            for h in hits:
                with st.expander(f"{h['report']} — {h['dp']} ({len(h['lines'])} lines)"):
                    st.code("\n".join(f"{n:>4}: {t}" for n, t in h["lines"][:40]) or "(match in the name only)",
                            language="sql")

    st.divider()
    dump = []
    for r in results:
        with st.expander(f"{r['status']} — {r['name']}", expanded=len(results) == 1):
            for d in r["dps"]:
                st.markdown(f"**{d['name']}** · source `{d.get('source', '-')}`")
                st.code(d["sql"], language="sql")
                dump.append(f"-- Report: {r['name']} | Data provider: {d['name']}\n{d['sql'].strip()}\n")
    st.download_button("📥 Download all as .sql", "\n\n".join(dump).encode("utf-8"),
                       f"report_sql_{datetime.now():%Y%m%d_%H%M%S}.sql", "text/plain")
