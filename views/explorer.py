"""Read-only API explorer: inspect a report's data providers, find universe ids, list endpoints."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from bo import cms, documents as docs
from views.common import credentials, session


def render():
    st.title("🔧 API explorer")
    st.caption("Read-only tools for looking inside the BO REST API.")
    server, user, pwd = credentials()

    st.subheader("🔍 Data providers of one report")
    rid = st.text_input("Report id")
    if st.button("Show data providers") and rid.strip():
        with session(server, user, pwd) as h:
            if h:
                dps, err = docs.get_dataproviders(server, h, rid.strip())
                if err:
                    st.error(err)
                for dp in dps:
                    dp_id = dp.get("id")
                    st.markdown(f"**{dp.get('name', dp_id)}** · type `{dp.get('dataSourceType')}` · "
                                f"dataSourceId `{dp.get('dataSourceId')}`")
                    st.json({k: v for k, v in dp.items() if k != "_docId"}, expanded=False)
                    sql, serr, src = docs.get_dataprovider_sql_robust(server, h, rid.strip(), dp_id)
                    with st.expander(f"SQL ({src or 'not found'})"):
                        st.code(sql or serr, language="sql")

    st.divider()
    st.subheader("🌐 Find universe ids")
    q = st.text_input("Name contains (empty = all)")
    if st.button("List universes"):
        with session(server, user, pwd) as h:
            if h:
                entries, err = cms.find_universes(server, h, q)
                if err:
                    st.error(err)
                elif entries:
                    st.dataframe(pd.DataFrame([{"ID": str(e.get("SI_ID")), "Name": e.get("SI_NAME", ""),
                                                "Kind": e.get("SI_KIND", "")} for e in entries]),
                                 width="stretch", hide_index=True)
                    st.caption("Use the ids of the .unx universes in the [universes] section of config.toml.")
                else:
                    st.info("No results.")

    st.divider()
    st.subheader("📜 Endpoint list (WADL)")
    if st.button("Fetch WADL"):
        with session(server, user, pwd) as h:
            found = docs.fetch_wadl(server, h) if h else []
        for url, status, _ in found:
            st.text(f"{status}  {url}")
        for url, _, text in (f for f in found if f[2]):
            lines, hits = docs.summarize_wadl(text)
            st.success(f"{url}: {len(lines)} endpoints")
            st.code("\n".join(hits) or "(no matches)", language="text")
            with st.expander("All endpoints"):
                st.code("\n".join(lines), language="text")
            st.download_button("📥 Raw WADL", text.encode("utf-8"), "raylight.wadl.xml", "application/xml",
                               key=f"wadl_{url}")
