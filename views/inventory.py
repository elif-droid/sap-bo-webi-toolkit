"""Inventory: report counts per folder, members per group, and which universe each report uses."""

from __future__ import annotations

from urllib.parse import urlparse

import pandas as pd
import streamlit as st

from bo import cms
from bo.text import contains_any, norm, parse_list
from views.common import config, credentials, download_table, keep, search_df, session, store


def _server_key(server: str) -> str:
    """Cache entries are tagged per server: test and production systems reuse ids."""
    return (urlparse(server).netloc or server).lower()


def _load_cache() -> dict:
    if "scan_cache" not in st.session_state:
        st.session_state["scan_cache"] = store().load_json(store().scan_cache, {})
    return st.session_state["scan_cache"]


def _folders(server, user, pwd, exclude):
    st.subheader("📁 Reports per folder")
    if st.button("🔄 Count reports", type="primary", key="btn_folders"):
        with session(server, user, pwd) as h:
            if h:
                with st.spinner("Walking the folder tree…"):
                    rows, errs = cms.folder_report_counts(server, h, exclude=exclude)
                st.session_state["inv_folders"] = (rows, errs)
    rows, errs = st.session_state.get("inv_folders", ([], []))
    if not rows:
        return
    df = (pd.DataFrame([{"Folder": r["folder"], "Direct": r["direct"], "Total (incl. sub-folders)": r["total"],
                         "SI_ID": r["id"]} for r in rows])
          .sort_values("Total (incl. sub-folders)", ascending=False))
    st.success(f"{len(df)} folders · {int(df['Direct'].sum())} reports")
    view = search_df(df, ["Folder"], st.text_input("🔎 Search folders", key="inv_f_q"))
    st.dataframe(view, width="stretch", hide_index=True)
    if errs:
        with st.expander(f"⚠️ {len(errs)} warnings"):
            st.text("\n".join(errs))
    download_table(view, "folder_report_counts", "dl_folders")


def _groups(server, user, pwd):
    st.subheader("👥 Members per user group")
    extra = st.text_input("Extra group SI_IDs not listed by the API (comma separated)")
    if st.button("🔄 Count members", type="primary", key="btn_groups"):
        with session(server, user, pwd) as h:
            if h:
                with st.spinner("Reading groups…"):
                    rows, err = cms.group_member_counts(server, h, [x.strip() for x in extra.split(",") if x.strip().isdigit()])
                if err and not rows:
                    st.error(err)
                st.session_state["inv_groups"] = rows
    rows = st.session_state.get("inv_groups")
    if not rows:
        return
    df = pd.DataFrame([{"Group": r["group"], "Description": r["description"], "Members": r["members"],
                        "SI_ID": r["id"], "Error": r["error"]} for r in rows]).sort_values("Members")
    st.success(f"{len(df)} groups · {int((df['Members'] == 0).sum())} empty (probably unused)")
    view = search_df(df, ["Group", "Description"], st.text_input("🔎 Search groups", key="inv_g_q"))
    st.dataframe(view.style.apply(lambda r: ["background-color:#fef7e0"] * len(r) if r["Members"] == 0 else [""] * len(r),
                                  axis=1), width="stretch", hide_index=True)
    download_table(view, "group_member_counts", "dl_groups")


def _universes(server, user, pwd, exclude):
    cfg, s = config(), store()
    st.subheader("🌐 Universe per report")
    if st.button("📚 Load report list", type="primary", key="btn_index"):
        with session(server, user, pwd) as h:
            if h:
                with st.spinner("Listing every report (may take a few minutes)…"):
                    reports, errs = cms.index_reports(server, h, exclude=exclude)
                st.session_state["inv_index"], st.session_state["inv_index_errs"] = reports, errs
    index = st.session_state.get("inv_index") or []
    cache = _load_cache()
    key = _server_key(server)

    if index:
        q = norm(st.text_input("🔎 Filter by report or folder name", key="inv_r_q"))
        matches = [r for r in index if not q or q in norm(r["name"]) or q in norm(r["path"])]
        st.write(f"{len(matches)} of {len(index)} reports" + (" (table shows the first 500)" if len(matches) > 500 else ""))
        table = pd.DataFrame([{"Select": False, "Report": r["name"], "Folder": r["path"], "ID": r["id"]}
                              for r in matches[:500]])
        edited = st.data_editor(table, hide_index=True, width="stretch", disabled=["Report", "Folder", "ID"]) \
            if not table.empty else table
        b1, b2, b3 = st.columns([1, 1, 1])
        run_sel = b1.button("▶️ Scan selected")
        run_all = b2.button(f"⏩ Scan all {len(matches)}")
        force = b3.checkbox("Rescan already scanned")
        if run_sel or run_all:
            ids = set(edited[edited["Select"]]["ID"]) if run_sel and not table.empty else None
            targets = [r for r in matches if ids is None or r["id"] in ids]
            todo = [r for r in targets if force or cache.get(r["id"], {}).get("server") != key]
            if todo:
                with session(server, user, pwd) as h:
                    if h:
                        bar = st.progress(0.0)
                        failed = cms.scan_reports(server, h, todo, cache, cfg.unx_ids, key,
                                                  progress=lambda d, n: bar.progress(d / n, f"{d}/{n}"),
                                                  on_checkpoint=lambda c: s.save_json(s.scan_cache, c))
                        if failed:
                            with st.expander(f"⚠️ {len(failed)} reports could not be read"):
                                st.dataframe(pd.DataFrame(failed), hide_index=True)

    entries = {rid: e for rid, e in cache.items() if e.get("server") == key
               and not any(contains_any(seg, exclude) for seg in str(e.get("path", "")).split("/"))}
    if not entries:
        st.caption("Nothing scanned yet for this server.")
        return

    st.markdown("#### Conversion status")
    rep = pd.DataFrame([{"Report": e["name"], "Folder": e["path"], "Status": cms.report_status(e["rows"]),
                         "Universes": ", ".join(sorted({r["universe"] for r in e["rows"] if r["universe"]})),
                         "Scanned": str(e.get("ts", ""))[:16].replace("T", " "), "Report ID": rid}
                        for rid, e in entries.items()])
    rep.insert(1, "Same name", rep.groupby(rep["Report"].map(norm))["Report"].transform("count"))
    m = st.columns(4)
    for col, (label, prefix) in zip(m, [("Converted", "✅"), ("Partly", "⚠️"), ("Not converted", "❌"), ("Other", "❓")]):
        col.metric(f"{prefix} {label}", int(rep["Status"].str.startswith(prefix).sum()))
    f1, f2 = st.columns([1, 2])
    status = f1.selectbox("Status", ["All"] + sorted(rep["Status"].unique()))
    view = rep if status == "All" else rep[rep["Status"] == status]
    if st.checkbox("Only reports whose name appears more than once (copies / old versions?)"):
        view = view[view["Same name"] > 1].sort_values(["Report", "Folder"])
    view = search_df(view, ["Report", "Folder", "Universes"], f2.text_input("🔎 Search", key="inv_s_q"))
    st.dataframe(view, width="stretch", hide_index=True)
    download_table(view, "report_conversion_status", "dl_status")

    st.markdown("#### Universes")
    rows = pd.DataFrame([{"Universe": r["universe"], "Status": r.get("status", cms.STATUS_UNKNOWN), "Report ID": rid}
                         for rid, e in entries.items() for r in e["rows"] if r["universe"] != "(no data provider)"])
    if rows.empty:
        return
    uni = (rows.groupby(["Universe", "Status"])["Report ID"].nunique().reset_index()
           .rename(columns={"Report ID": "Reports"}).sort_values("Reports", ascending=False).reset_index(drop=True))
    uni = search_df(uni, ["Universe"], st.text_input("🔎 Search universes", key="inv_u_q")).reset_index(drop=True)
    event = st.dataframe(uni, width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row")
    if event.selection.rows:
        u, stt = uni.iloc[event.selection.rows[0]][["Universe", "Status"]]
        ids = set(rows[(rows["Universe"] == u) & (rows["Status"] == stt)]["Report ID"])
        st.markdown(f"**{u}** ({stt}) is used by {len(ids)} reports")
        st.dataframe(rep[rep["Report ID"].isin(ids)][["Report", "Folder", "Report ID"]], width="stretch", hide_index=True)


def render():
    cfg = config()
    st.title("📊 Inventory")
    server, user, pwd = credentials()
    excl_text = st.text_input("🚫 Skip folders whose name contains (comma separated)",
                              value=keep("exclude", cfg.exclude_folders))
    st.session_state["keep_exclude"] = excl_text
    exclude = parse_list(excl_text)
    tab1, tab2, tab3 = st.tabs(["Folders", "Groups", "Universes"])
    with tab1:
        _folders(server, user, pwd, exclude)
    with tab2:
        _groups(server, user, pwd)
    with tab3:
        _universes(server, user, pwd, exclude)
