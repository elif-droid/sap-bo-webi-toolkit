"""Bulk conversion of Webi reports to new universes, with backups and an audit trail."""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import streamlit as st

from bo import cms, documents as docs
from bo.client import BOError, bo_session
from bo.mapping import ALREADY_CONVERTED, build_name_rules, convert_document, make_name_resolver
from bo.text import norm
from views.common import config, credentials, keep, session, status_style, store

BACKUP_MODES = {
    "auto": "Copy automatically (server-side copy before converting)",
    "verify_only": "Copies were made in BI Launch Pad — only verify they exist",
    "rollback_only": "No copy — keep a rollback record only",
}


def _target_settings(cfg):
    """Returns (fixed_target_id, target_label, name_to_id, id_overrides)."""
    mode = st.radio("🎯 Target universe", ["Match by name (automatic)", "Pick one universe"],
                    index=keep("target_mode", 0),
                    help="Automatic: each query goes to the target universe with the same name as its "
                         "source universe, so one report can be split across several universes. "
                         "Pick one: every query goes to the chosen universe.")
    st.session_state["keep_target_mode"] = 0 if mode.startswith("Match") else 1

    if not cfg.universes:
        st.warning("No target universes in config.toml (section [universes]).")
        return None, "", {}, {}

    if mode.startswith("Pick"):
        labels = list(cfg.universes)
        prev = keep("universe", labels[0])
        label = st.selectbox("Target universe (.unx)", labels, index=labels.index(prev) if prev in labels else 0)
        st.session_state["keep_universe"] = label
        st.caption(f"Target id: **{cfg.universes[label]}**")
        return cfg.universes[label], label, None, None

    rules = build_name_rules(cfg.universes, cfg.preferred_universes)
    name_to_id = {}
    with st.expander(f"📋 Name-matching rules ({len(rules)} target universes)"):
        for key, opts in sorted(rules.items()):
            if len(opts) == 1:
                name_to_id[key] = opts[0][1]
                st.write(f"• **{opts[0][0]}** → id {opts[0][1]}")
            else:
                pick = st.selectbox(f"Several universes are named '{opts[0][0]}' — which one?",
                                    [f"{n} (id {i})" for n, i in opts], key=f"amb_{key}")
                name_to_id[key] = pick.rsplit("(id ", 1)[1].rstrip(")")

    with st.expander("🔧 Manual id rules (for sources whose name cannot be read)"):
        st.caption("One `sourceId=targetId` per line. Saved locally.")
        text = st.text_area("Rules", value=keep("id_rules", store().load_id_rules_text()), height=110)
        st.session_state["keep_id_rules"] = text
        if st.button("💾 Save id rules"):
            store().save_id_rules_text(text)
            st.success("Saved.")
    return None, "Automatic (by name)", name_to_id, store().parse_id_rules(text)


def _backup_folder_picker(cfg, server, user, pwd, operator):
    st.subheader("📁 Backup folder")
    c1, c2 = st.columns([1, 2])
    with c1:
        if st.button("🔄 Load folders from BO"):
            with session(server, user, pwd) as h:
                if h:
                    with st.spinner("Reading folders…"):
                        folders, errs = cms.backup_folder_tree(server, h, cfg.backup_folder_prefix)
                    st.session_state["bo_folders"], st.session_state["bo_folder_errors"] = folders, errs
        for e in st.session_state.get("bo_folder_errors", []):
            st.caption(f"⚠️ {e}")

    selected_id, selected_label = None, ""
    folders = st.session_state.get("bo_folders", [])
    with c2:
        if folders:
            labels = [f"{f['path']}  (ID {f['id']})" for f in folders]
            # Default to the operator's own sub-folder if there is one.
            default = next((i for i, f in enumerate(folders) if norm(f["name"]) == norm(operator)), 0)
            selected_label = st.selectbox("Backups go to", labels, index=default)
            selected_id = folders[labels.index(selected_label)]["id"]
        else:
            st.caption(f"Load the folder list, or enter an id below. Otherwise the folder named "
                       f"'{cfg.backup_folder_name}' is used.")
    manual = st.text_input("…or folder SI_ID", placeholder="BI Launch Pad › folder › Properties › ID")
    if manual.strip():
        return manual.strip(), f"folder id {manual.strip()}"
    if selected_id:
        return str(selected_id), selected_label.split("  (ID")[0]
    return "", f"'{cfg.backup_folder_name}'"


def _name_to_id_helper(server, user, pwd):
    with st.expander("📄 Resolve report names to ids"):
        names_text = st.text_area("Report names (one per line, e.g. a column pasted from Excel)", height=120)
        if st.button("🔎 Resolve"):
            names = [x.strip() for x in names_text.splitlines() if x.strip()]
            with session(server, user, pwd) as h:
                if h and names:
                    found, ambiguous, missing = [], [], []
                    bar = st.progress(0.0)
                    for i, n in enumerate(names, 1):
                        hits = docs.find_reports_by_name(server, h, n, with_path=True)
                        (found.append((n, hits[0]["id"])) if len(hits) == 1
                         else ambiguous.append((n, hits)) if hits else missing.append(n))
                        bar.progress(i / len(names))
                    st.session_state["resolve"] = (found, ambiguous, missing)

        found, ambiguous, missing = st.session_state.get("resolve", ([], [], []))
        if found or ambiguous or missing:
            st.success(f"Unique: {len(found)} · Ambiguous: {len(ambiguous)} · Not found: {len(missing)}")
            chosen = [rid for _, rid in found]
            for n, hits in ambiguous:
                opts = {"(skip)": None} | {f"{h.get('path', '')} → {h['name']} (ID {h['id']})": h["id"] for h in hits}
                pick = st.selectbox(f"Which '{n}'?", list(opts), key=f"amb_name_{n}")
                if opts[pick]:
                    chosen.append(opts[pick])
            if missing:
                st.warning("Not found: " + ", ".join(missing[:30]))
            if st.button("⬇️ Use these ids"):
                st.session_state["keep_ids"] = "\n".join(chosen)
                st.rerun()


def _convert_one(cfg, server, user, pwd, doc_id, doc_name, opts) -> tuple[str, str, int, str]:
    """Backs up and converts one report. Returns (status, detail, query_count, sql_flag)."""
    s = store()
    if opts["backup_mode"] == "rollback_only":
        backup_msg = "No copy taken; rollback record kept"
    else:
        with bo_session(server, user, pwd) as h:
            folder_id = opts["folder_id"]
            if not folder_id:
                folder_id, err = cms.find_folder_by_name(server, h, cfg.backup_folder_name)
                if not folder_id:
                    return "🔴 FAILED (NO BACKUP FOLDER)", err, 0, "-"
            ok, backup_msg = docs.backup_document(server, h, doc_id, doc_name, folder_id, mode=opts["backup_mode"])
        if not ok:
            return "🔴 FAILED (NO BACKUP)", backup_msg, 0, "-"
        backup_msg += f" [folder: {opts['folder_label']}]"

    # Convert in a fresh session so the backup copy is never the active document.
    with bo_session(server, user, pwd) as h:
        resolver = (make_name_resolver(server, h, opts["name_to_id"], opts["id_overrides"])
                    if opts["name_to_id"] is not None else None)
        before = None
        if opts["verify_rows"]:
            before, d = docs.refresh_and_count(server, h, doc_id, cfg.date_start, cfg.date_end,
                                               opts["debug"], "before", end_keywords=cfg.date_end_keywords)
            if before is None:
                return "🟠 WARNING (REFRESH FAILED – NOT SAVED)", f"{backup_msg} | {d}", 0, "-"

        r = convert_document(server, h, doc_id, opts["target_id"], apply=True, resolver=resolver,
                             universe_names=cfg.universe_names)
        opts["debug"].extend(r.debug)
        old = "previous ids: " + ",".join(r.old_map.values())
        notes = "".join([
            f" | ⚠️ {len(r.skipped)} queries left unchanged: {'; '.join(r.skipped)}" if r.skipped else "",
            f" | 🟡 {len(r.ambiguous)} ambiguous mappings: {'; '.join(r.ambiguous)}" if r.ambiguous else "",
            f" | {r.sql_summary}" if r.sql_summary else "",
        ])
        sql_flag = "-" if not r.sql_summary else ("⚠️ Changed" if r.sql_changed else "✅ Same")

        if r.error == ALREADY_CONVERTED:
            return "⏭️ SKIPPED (ALREADY ON TARGET)", f"{r.query_count} queries already on target.", r.query_count, sql_flag
        if r.error:
            return "🟠 WARNING (NOT SAVED)", f"{backup_msg} | {r.applied}/{r.query_count} mapped. {r.error} [{old}]", r.query_count, sql_flag
        if r.applied == 0:
            return "🟠 WARNING (NOTHING CONVERTED)", f"{backup_msg} | no query matched.{notes} [{old}]", r.query_count, sql_flag

        extra = ""
        if opts["verify_rows"]:
            after, d = docs.refresh_and_count(server, h, doc_id, cfg.date_start, cfg.date_end,
                                              opts["debug"], "after", end_keywords=cfg.date_end_keywords)
            if after is None:
                return "🟠 WARNING (REFRESH FAILED – NOT SAVED)", f"{backup_msg} | {d} [{old}]", r.query_count, sql_flag
            if after != before:
                return "🔴 ROW MISMATCH (NOT SAVED)", f"{backup_msg} | before={before}, after={after} [{old}]", r.query_count, sql_flag
            ok, pdetail = docs.purge_data(server, h, doc_id, opts["purge_log"])
            if not ok:
                return "🟠 WARNING (PURGE FAILED – NOT SAVED)", f"{backup_msg} | {pdetail}", r.query_count, sql_flag
            _, cdetail = docs.clear_prompts(server, h, doc_id, opts["purge_log"])
            extra = f" | rows equal ({after}), {pdetail}, {cdetail}"

        saved, msg = docs.save_document(server, h, doc_id)
        if not saved:
            return "🔴 FAILED (NOT SAVED)", f"{backup_msg} | mapped but not saved: {msg} [{old}]", r.query_count, sql_flag

    s.record_rollback(doc_id, doc_name, opts["target_label"], r.old_map, opts["operator"])
    detail = f"{backup_msg} | {r.applied}/{r.query_count} queries converted and saved.{extra}{notes} [{old}]"
    if r.skipped:
        if r.pending:
            s.add_pending(doc_id, doc_name, opts["operator"], r.pending)
        return "🟡 PARTIAL", detail, r.query_count, sql_flag
    if r.ambiguous or r.sql_changed:
        return "🟡 SUCCESS – CHECK", detail, r.query_count, sql_flag
    s.remove_pending(doc_id)
    return "🟢 SUCCESS", detail, r.query_count, sql_flag


def _dry_run_one(cfg, server, h, doc_id, opts):
    resolver = (make_name_resolver(server, h, opts["name_to_id"], opts["id_overrides"])
                if opts["name_to_id"] is not None else None)
    r = convert_document(server, h, doc_id, opts["target_id"], apply=False, resolver=resolver,
                         universe_names=cfg.universe_names)
    opts["debug"].extend(r.debug)
    old = "previous ids: " + ",".join(r.old_map.values())
    skip = f" | would skip {len(r.skipped)}: {'; '.join(r.skipped)}" if r.skipped else ""
    amb = f" | ⚠️ {len(r.ambiguous)} ambiguous: {'; '.join(r.ambiguous)}" if r.ambiguous else ""
    if r.error == ALREADY_CONVERTED:
        return "⏭️ SKIPPED (ALREADY ON TARGET)", f"{r.query_count} queries already on target.", r.query_count
    if r.error:
        return "🧪 DRY RUN – WARNING", f"{r.applied}/{r.query_count} mappable. {r.error} [{old}]{skip}{amb}", r.query_count
    if r.skipped:
        return "🧪 DRY RUN – PARTIAL", f"{r.applied}/{r.query_count} mappable [{old}]{skip}{amb}", r.query_count
    if r.ambiguous:
        return "🧪 DRY RUN – CHECK", f"All {r.query_count} mappable, some ambiguous [{old}]{amb}", r.query_count
    return "🧪 DRY RUN – OK", f"All {r.query_count} queries can be mapped. [{old}]", r.query_count


def render():
    cfg, s = config(), store()
    st.title("🔄 Convert reports")
    st.caption("Re-points Web Intelligence reports to new universes. Every report is backed up first; "
               "a report is only saved when every query maps cleanly.")

    left, right = st.columns(2)
    with left:
        mode_key = st.selectbox("🛡️ Backup", list(BACKUP_MODES), format_func=BACKUP_MODES.get,
                                index=keep("backup_mode", 0))
        st.session_state["keep_backup_mode"] = list(BACKUP_MODES).index(mode_key)
        with st.expander("Advanced"):
            dry_run = st.checkbox("🧪 Dry run (check mappings only, change nothing)")
            verify_rows = st.checkbox(
                "Verify row counts (refresh before and after, then purge)",
                help="Answers every prompt with all values and the configured date range. "
                     "Slower; the report is only saved if the row counts match.")
    with right:
        target_id, target_label, name_to_id, id_overrides = _target_settings(cfg)

    st.divider()
    st.subheader("👤 Operator and BO credentials")
    if cfg.operators:
        prev = keep("operator", cfg.operators[0])
        operator = st.selectbox("Operator", cfg.operators,
                                index=cfg.operators.index(prev) if prev in cfg.operators else 0)
    else:
        operator = st.text_input("Operator", value=keep("operator", ""))
    st.session_state["keep_operator"] = operator
    server, user, pwd = credentials()

    if mode_key != "rollback_only":
        st.divider()
        folder_id, folder_label = _backup_folder_picker(cfg, server, user, pwd, operator)
    else:
        folder_id, folder_label = "", ""

    st.divider()
    st.subheader("📋 Reports")
    _name_to_id_helper(server, user, pwd)
    ids_text = st.text_area("Report ids (one per line)", height=140, value=keep("ids"), placeholder="1001\n1002")
    st.session_state["keep_ids"] = ids_text

    if not st.button("🧪 Start dry run" if dry_run else "🚀 Start conversion", type="primary"):
        return

    doc_ids = list(dict.fromkeys(x.strip() for x in ids_text.splitlines() if x.strip()))
    bad = [i for i in doc_ids if not i.isdigit()]
    if not operator or not user or not pwd:
        st.error("Operator, username and password are required.")
        return
    if bad or not doc_ids:
        st.error(f"Enter numeric report ids. Invalid: {', '.join(bad[:10])}" if bad else "Enter at least one report id.")
        return
    if target_id is None and not name_to_id:
        st.error("No target universe configured.")
        return

    opts = dict(backup_mode=mode_key, folder_id=folder_id, folder_label=folder_label, target_id=target_id,
                target_label=target_label, name_to_id=name_to_id, id_overrides=id_overrides,
                verify_rows=verify_rows, operator=operator, debug=[], purge_log=[])
    results, bar, status = [], st.progress(0.0), st.empty()
    try:
        with bo_session(server, user, pwd) as h:
            for i, doc_id in enumerate(doc_ids, 1):
                status.text(f"[{i}/{len(doc_ids)}] report {doc_id}…")
                name, sql_flag, count = f"Report_{doc_id}", "-", 0
                try:
                    name = docs.get_document_name(server, h, doc_id)
                    if dry_run:
                        result, detail, count = _dry_run_one(cfg, server, h, doc_id, opts)
                    else:
                        result, detail, count, sql_flag = _convert_one(cfg, server, user, pwd, doc_id, name, opts)
                except Exception as e:
                    result, detail = "🔴 ERROR", str(e)
                if not dry_run:
                    s.log_action(operator, user, doc_id, name, target_label, result, count, detail)
                    if not result.startswith(("🟢", "⏭️")):
                        s.track_error(doc_id, name, operator, detail)
                results.append({"Report ID": doc_id, "Report": name, "Queries": count, "Status": result,
                                "SQL check": sql_flag, "Detail": detail})
                bar.progress(i / len(doc_ids))
    except BOError as e:
        st.error(f"Could not log on: {e}")
        return
    status.text("✅ Done (BO session closed).")

    df = pd.DataFrame(results)
    st.subheader("📊 Results")
    st.dataframe(df.style.apply(status_style, axis=1), width="stretch")
    st.download_button("📥 Download results (CSV)", df.to_csv(index=False).encode("utf-8-sig"),
                       f"conversion_results_{datetime.now():%Y%m%d_%H%M%S}.csv", "text/csv")
    if opts["debug"] or opts["purge_log"]:
        with st.expander("🔍 Debug details"):
            st.code(json.dumps(opts["debug"], ensure_ascii=False, indent=2), language="json")
            if opts["purge_log"]:
                st.code("\n".join(opts["purge_log"]))
