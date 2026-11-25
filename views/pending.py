"""Reports that were only partly converted because a target universe did not exist yet."""

from __future__ import annotations

import streamlit as st

from bo import documents as docs
from bo.client import BOError, bo_session
from bo.mapping import ALREADY_CONVERTED, build_name_rules, convert_document, make_name_resolver
from core.storage import read_csv
from views.common import config, credentials, keep, store


def render():
    cfg, s = config(), store()
    st.title("⏳ Pending conversions")
    st.caption("Queries whose target universe was not published yet were left unchanged. Once the universe "
               "exists, convert the remaining queries here. A report leaves this list when it is complete.")

    df = read_csv(s.pending)
    if df.empty:
        st.info("Nothing pending.")
        return

    operator = st.selectbox("Operator", cfg.operators) if cfg.operators else st.text_input("Operator", keep("operator"))
    server, user, pwd = credentials()
    st.caption("Only the remaining queries are mapped and saved; row counts are not compared. "
               "Check important reports in BI Launch Pad afterwards.")

    rules = build_name_rules(cfg.universes, cfg.preferred_universes)
    name_to_id = {k: v[0][1] for k, v in rules.items() if len(v) == 1}
    id_overrides = s.parse_id_rules(s.load_id_rules_text())

    for uname in sorted(df["Waiting for universe"].dropna().unique()):
        sub = df[df["Waiting for universe"] == uname]
        uids = sorted(set(sub["Universe ID"].astype(str)))
        report_ids = list(dict.fromkeys(sub["Report ID"].astype(str)))
        st.markdown(f"### 🎯 {uname} · {len(report_ids)} report(s)")
        st.dataframe(sub[["Report ID", "Report name", "Timestamp", "Operator"]], width="stretch", hide_index=True)
        if not st.button(f"✅ '{uname}' is published — convert remaining queries", key=f"go_{uname}", type="primary"):
            continue
        if not user or not pwd:
            st.error("Username and password are required.")
            continue
        done = still = 0
        bar = st.progress(0.0)
        for i, rid in enumerate(report_ids, 1):
            try:
                with bo_session(server, user, pwd) as h:
                    name = docs.get_document_name(server, h, rid)
                    r = convert_document(server, h, rid, None, apply=True,
                                         resolver=make_name_resolver(server, h, name_to_id, id_overrides),
                                         universe_names=cfg.universe_names)
                    if r.error and r.error != ALREADY_CONVERTED:
                        st.warning(f"[{name}] {r.error}")
                        still += 1
                        continue
                    saved, msg = docs.save_document(server, h, rid) if r.applied else (True, "")
                    if not saved:
                        st.error(f"[{name}] not saved: {msg}")
                        still += 1
                        continue
                s.remove_pending(rid, universe_ids=uids)
                if r.pending:
                    s.add_pending(rid, name, operator, r.pending)
                    still += 1
                    st.info(f"[{name}] converted; still waiting for another universe.")
                else:
                    done += 1
                    st.success(f"[{name}] complete.")
                for note in r.ambiguous:
                    s.track_error(rid, name, operator, f"Ambiguous mapping applied, check manually: {note}")
                if r.sql_changed:
                    s.track_error(rid, name, operator, r.sql_summary)
                s.log_action(operator, user, rid, name, uname, "🟢 PENDING CONVERTED", r.query_count,
                             f"{r.applied}/{r.query_count} queries")
            except BOError as e:
                st.error(f"[{rid}] could not log on: {e}")
            bar.progress(i / len(report_ids))
        st.success(f"{done} complete, {still} still pending.")
