"""Security audit: users, groups, which group can view which report, and assigned roles."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from bo import cms
from bo.text import norm
from views.common import credentials, download_table, search_df, session


def render():
    st.title("🛡️ Security audit")
    st.caption("Read-only. Most queries need an administrator account.")
    server, user, pwd = credentials()

    st.subheader("👥 Users and groups")
    c1, c2 = st.columns(2)
    which = "users" if c1.button("Load users") else "groups" if c2.button("Load groups") else None
    if which:
        with session(server, user, pwd) as h:
            if h:
                entries, err = cms.list_users(server, h) if which == "users" else cms.list_user_groups(server, h)
                if err and not entries:
                    st.error(err)
                st.session_state["sec_principals"] = pd.DataFrame([
                    {"SI_ID": e.get("SI_ID") or e.get("id"), "Name": e.get("SI_NAME") or e.get("name"),
                     "Full name / description": e.get("fullname") or e.get("description") or "",
                     "Email": e.get("email") or ""} for e in entries])
    df = st.session_state.get("sec_principals")
    if df is not None and not df.empty:
        st.dataframe(search_df(df, ["Name", "Full name / description", "Email"],
                               st.text_input("🔎 Search", key="sec_p_q")), width="stretch", hide_index=True)

    st.divider()
    st.subheader("📑 Which group can view which report")
    if st.button("Load group list"):
        with session(server, user, pwd) as h:
            if h:
                groups, err = cms.list_user_groups(server, h)
                st.session_state["sec_groups"] = [{"id": str(g.get("SI_ID") or g.get("id")),
                                                   "name": g.get("SI_NAME") or g.get("name") or ""} for g in groups]
                if err and not groups:
                    st.error(err)
    groups = st.session_state.get("sec_groups") or []
    if groups:
        labels = {f"{g['name']} (ID {g['id']})": g for g in groups}
        q = st.text_input("🔎 Filter groups", key="sec_g_q")
        pick = st.multiselect("Groups", [k for k in labels if not q or norm(q) in norm(k)])
        all_groups = st.checkbox(f"All {len(groups)} groups (slow, heavy on the server)")
        if st.button("▶️ Find reports", type="primary"):
            targets = list(labels.values()) if all_groups else [labels[k] for k in pick]
            if not targets:
                st.warning("Choose at least one group.")
            else:
                with session(server, user, pwd) as h:
                    if h:
                        bar = st.progress(0.0)
                        rows, errs = cms.reports_visible_to_groups(
                            server, h, [t["id"] for t in targets], {g["id"]: g["name"] for g in groups},
                            progress=lambda d, n: bar.progress(d / n))
                        folders, _ = cms.folders_by_id(server, h)
                        st.session_state["sec_access"] = pd.DataFrame([
                            {"Group": r["group"], "Report": r.get("SI_NAME", ""), "Kind": r.get("SI_KIND", ""),
                             "Owner": r.get("SI_OWNER", ""), "Folder": cms.path_of(folders, r.get("SI_PARENTID")),
                             "Report ID": r.get("SI_ID", "")} for r in rows])
                        for e in errs:
                            st.caption(f"⚠️ {e}")
    access = st.session_state.get("sec_access")
    if access is not None and not access.empty:
        st.success(f"{len(access)} rows · {access['Group'].nunique()} groups · {access['Report ID'].nunique()} reports")
        view = search_df(access, ["Report", "Group", "Folder", "Owner"], st.text_input("🔎 Search", key="sec_a_q"))
        st.dataframe(view, width="stretch", hide_index=True)
        download_table(view, "group_report_access", "dl_access")

    st.divider()
    st.subheader("🧩 Roles on reports (own and inherited from folders)")
    if st.button("Load roles"):
        with session(server, user, pwd) as h:
            if h:
                with st.spinner("Reading reports, folders and roles…"):
                    rows, errs = cms.report_roles(server, h)
                st.session_state["sec_roles"] = pd.DataFrame([
                    {"Report": r["name"], "Kind": r["kind"], "Owner": r["owner"], "Folder": r["path"],
                     "Own roles": r["own_roles"], "Inherited": r["inherited_roles"], "Report ID": r["id"]} for r in rows])
                for e in errs[:20]:
                    st.caption(f"⚠️ {e}")
    roles = st.session_state.get("sec_roles")
    if roles is not None and not roles.empty:
        view = search_df(roles, ["Report", "Folder", "Owner", "Own roles", "Inherited"],
                         st.text_input("🔎 Search", key="sec_r_q"))
        st.dataframe(view, width="stretch", hide_index=True)
        download_table(view, "report_roles", "dl_roles")
