"""CMS inventory: folders, report counts, user groups, report → universe index, security. Read-only."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

from bo.client import VERIFY_TLS, cmsquery_all, infostore_query, rest_collect_all, short
from bo.documents import PUBLIC_FOLDERS_ROOT_ID, get_dataprovider, get_dataproviders
from bo.mapping import source_universe_name
from bo.text import contains_any, norm


def _as_list(x):
    return [x] if isinstance(x, dict) else (x or [])


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------
def list_children(server, headers, folder_id, page_size=200, max_pages=200):
    """Direct children of a folder via /infostore/{id}/children (paged).

    Note: on some BI 4.x servers CMS queries silently ignore WHERE filters such as
    SI_ANCESTOR or SI_KIND and return the same rows for every folder, so the folder
    tree is walked with this endpoint instead.
    """
    items = []
    for page in range(1, max_pages + 1):
        try:
            res = requests.get(f"{server}/infostore/{folder_id}/children", headers=headers,
                               params={"page": page, "pageSize": page_size}, verify=VERIFY_TLS, timeout=30)
        except requests.RequestException as e:
            return items, f"request error (page {page}): {e}"
        if res.status_code != 200:
            return (items, None) if page > 1 else ([], f"HTTP {res.status_code}: {short(res)}")
        try:
            data = res.json()
        except ValueError:
            return items, f"response is not JSON (page {page})"
        batch = _as_list(data.get("entries", data.get("entry", [])))
        items.extend(batch)
        if len(batch) < page_size:
            break
    return items, None


def _kind(it) -> str:
    return str(it.get("SI_KIND") or it.get("kind") or it.get("type") or "").lower()


def _id(it):
    return it.get("SI_ID") or it.get("id")


def _name(it) -> str:
    return str(it.get("SI_NAME") or it.get("name") or "")


def child_folders(server, headers, parent_id):
    """Sub-folders of a folder. Returns ([{id, name}], error)."""
    items, err = list_children(server, headers, parent_id)
    if not err:
        return [{"id": int(_id(i)), "name": _name(i)} for i in items
                if _id(i) is not None and _name(i) and (not _kind(i) or _kind(i) == "folder")], None
    entries, qerr = infostore_query(server, headers,
                                    f"SELECT SI_ID, SI_NAME FROM CI_INFOOBJECTS WHERE SI_KIND='Folder' AND SI_PARENTID={parent_id}")
    if qerr is None:
        return [{"id": int(e["SI_ID"]), "name": str(e["SI_NAME"])} for e in entries
                if e.get("SI_ID") is not None and e.get("SI_NAME")], None
    return [], f"{err} | {qerr}"


def backup_folder_tree(server, headers, root_prefix: str):
    """Top-level folders whose name starts with `root_prefix` plus their direct sub-folders.

    Only two levels are read, so this stays fast on large systems. Returns ([{id, name, path}], errors).
    """
    top, err = child_folders(server, headers, PUBLIC_FOLDERS_ROOT_ID)
    if err:
        return [], [f"Public Folders could not be read: {err}"]
    roots = [f for f in top if norm(f["name"]).startswith(norm(root_prefix))]
    if not roots:
        return [], [f"No top-level folder starting with '{root_prefix}'. Seen: {', '.join(f['name'] for f in top)[:300]}"]
    out, errors = [], []
    for r in roots:
        path = f"Public Folders / {r['name']}"
        out.append({**r, "path": path})
        subs, err = child_folders(server, headers, r["id"])
        if err:
            errors.append(f"{path}: {err}")
        for s in sorted(subs, key=lambda c: norm(c["name"])):
            out.append({**s, "path": f"{path} / {s['name']}"})
    return out, errors


def find_folder_by_name(server, headers, name: str):
    entries, err = infostore_query(server, headers,
                                   f"SELECT SI_ID FROM CI_INFOOBJECTS WHERE SI_KIND='Folder' AND SI_NAME='{name}'")
    if not err and entries and entries[0].get("SI_ID") is not None:
        return int(entries[0]["SI_ID"]), None
    for f in child_folders(server, headers, PUBLIC_FOLDERS_ROOT_ID)[0]:
        if norm(f["name"]) == norm(name):
            return f["id"], None
    return None, f"Folder '{name}' not found (or not visible to this account)."


def _walk_counts(server, headers, folder_id, path, kind, exclude, depth=0, max_depth=20):
    if depth > max_depth:
        return [], 0, [f"{path}: max depth {max_depth} reached"]
    items, err = list_children(server, headers, folder_id)
    errors = [f"{path}: {err}"] if err else []
    direct, subs = 0, []
    for it in items:
        if _id(it) is None:
            continue
        if _kind(it) == kind.lower():
            direct += 1
        elif _kind(it) == "folder" and not contains_any(_name(it), exclude):
            subs.append((int(_id(it)), _name(it)))
    rows, total = [], direct
    for sid, sname in sorted(subs, key=lambda x: norm(x[1])):
        r, t, e = _walk_counts(server, headers, sid, f"{path}/{sname}", kind, exclude, depth + 1, max_depth)
        rows += r
        errors += e
        total += t
    rows.append({"id": folder_id, "folder": path, "direct": direct, "total": total})
    return rows, total, errors


def folder_report_counts(server, headers, kind="Webi", exclude=()):
    """Every folder under Public Folders with its direct and total (recursive) report count.

    Returns (rows, errors).
    """
    top, err = child_folders(server, headers, PUBLIC_FOLDERS_ROOT_ID)
    if err:
        return [], [err]
    rows, errors = [], []
    for f in sorted(top, key=lambda x: norm(x["name"])):
        if contains_any(f["name"], exclude):
            continue
        r, _, e = _walk_counts(server, headers, f["id"], f["name"], kind, exclude)
        rows += r
        errors += e
    return rows, errors


def index_reports(server, headers, kind="Webi", exclude=(), max_folders=5000):
    """Lists every report under Public Folders as {id, name, path}. Returns (reports, errors)."""
    reports, errors = [], []
    stack, visited = [(PUBLIC_FOLDERS_ROOT_ID, "Public Folders")], 0
    while stack and visited < max_folders:
        fid, path = stack.pop()
        visited += 1
        items, err = list_children(server, headers, fid)
        if err:
            errors.append(f"{path}: {err}")
        for it in items:
            if _id(it) is None:
                continue
            if _kind(it) == kind.lower():
                reports.append({"id": str(_id(it)), "name": _name(it), "path": path})
            elif _kind(it) == "folder" and not contains_any(_name(it), exclude):
                stack.append((int(_id(it)), f"{path}/{_name(it)}"))
    if stack:
        errors.append(f"Stopped at {max_folders} folders; {len(stack)} not scanned.")
    return reports, errors


# ---------------------------------------------------------------------------
# Users and groups
# ---------------------------------------------------------------------------
def list_users(server, headers):
    users, err = rest_collect_all(f"{server}/v1/users", headers)
    if users:
        return users, err
    entries, qerr = infostore_query(server, headers,
                                    "SELECT SI_ID, SI_NAME FROM CI_SYSTEMOBJECTS WHERE SI_KIND='User'")
    return entries or [], qerr


def list_user_groups(server, headers):
    """All user groups from GET /v1/usergroups, including nested sub-groups."""
    groups, err = rest_collect_all(f"{server}/v1/usergroups", headers)
    if err and not groups:
        return [], err
    seen = {str(_id(g)) for g in groups}
    queue = [_id(g) for g in groups]
    guard = 0
    while queue and guard < 2000:
        guard += 1
        parent = queue.pop(0)
        if parent is None:
            continue
        subs, _ = rest_collect_all(f"{server}/v1/usergroups/{parent}/usergroups", headers, max_pages=20)
        for sg in subs:
            sid = _id(sg)
            if sid is not None and str(sid) not in seen:
                seen.add(str(sid))
                groups.append(sg)
                queue.append(sid)
    return groups, None


def group_member_counts(server, headers, extra_ids=()):
    """Member count for every user group; empty groups are likely unused.

    Uses /v1/usergroups/{id}/users. Returns (rows, error).
    """
    groups, err = list_user_groups(server, headers)
    known = {str(_id(g)) for g in groups}
    for xid in extra_ids:
        if str(xid) not in known:
            groups.append({"id": xid, "name": f"(added manually, ID {xid})"})
    if not groups:
        return [], err or "No user groups found."
    rows = []
    for g in groups:
        gid = _id(g)
        name = g.get("SI_NAME") or g.get("name") or ""
        desc = g.get("SI_DESCRIPTION") or g.get("description") or ""
        if gid is None:
            rows.append({"id": None, "group": name, "description": desc, "members": 0, "error": "no id"})
            continue
        members, merr = rest_collect_all(f"{server}/v1/usergroups/{gid}/users", headers)
        rows.append({"id": gid, "group": name, "description": desc, "members": len(members),
                     "error": merr if merr and not members else ""})
    return rows, None


# ---------------------------------------------------------------------------
# Report → universe index
# ---------------------------------------------------------------------------
STATUS_UNX = "✅ .unx"
STATUS_UNV = "❌ .unv"
STATUS_OTHER = "📝 custom/other"
STATUS_UNKNOWN = "❓ unknown"


def dp_status(kind: str, ds_id: str, known_unx_ids: set) -> str:
    if kind == "unx" or str(ds_id) in known_unx_ids:
        return STATUS_UNX
    if kind == "unv":
        return STATUS_UNV
    return STATUS_UNKNOWN if not kind else STATUS_OTHER


def report_status(rows: list[dict]) -> str:
    states = {r.get("status", STATUS_UNKNOWN) for r in rows}
    if STATUS_UNV in states:
        return "⚠️ Partly converted" if STATUS_UNX in states else "❌ Not converted (.unv)"
    if STATUS_UNX in states:
        return "✅ Converted (.unx)"
    return "❓ Other / custom / unknown"


def report_universes(server, headers, report: dict, name_cache: dict, meta: dict, known_unx_ids: set):
    """One row per query: which universe it uses and whether that is .unx/.unv/custom."""
    dps, err = get_dataproviders(server, headers, report["id"])
    if err:
        return [{"query": "", "universe": "", "type": "", "status": STATUS_UNKNOWN, "error": err}]
    if not dps:
        return [{"query": "", "universe": "(no data provider)", "type": "", "status": STATUS_UNKNOWN, "error": ""}]
    rows = []
    for dp in dps:
        kind = str(dp.get("dataSourceType", "")).strip().lower()
        ds_id = str(dp.get("dataSourceId") or "")
        if ds_id in meta:
            uname, cached_kind = meta[ds_id]
            kind = kind or cached_kind
        else:
            if not kind:
                d = get_dataprovider(server, headers, report["id"], dp.get("id"))
                kind = str(d.get("dataSourceType", "")).strip().lower()
                ds_id = ds_id or str(d.get("dataSourceId") or "")
            try:
                uname, _ = source_universe_name(server, headers, dp, name_cache)
            except Exception:
                uname = ""
            if ds_id and uname and kind:
                meta[ds_id] = (uname, kind)
        rows.append({"query": str(dp.get("name") or ""), "universe": uname or "(unreadable)", "type": kind,
                     "status": dp_status(kind, ds_id, known_unx_ids), "error": ""})
    return rows


def scan_reports(server, headers, reports, cache: dict, known_unx_ids: set, server_key: str,
                 workers=5, progress=None, on_checkpoint=None, checkpoint_every=50):
    """Fills `cache` ({report_id: {...}}) with universe info for `reports`, in parallel.

    Reports that fail are not cached so the next scan retries them. Returns failures.
    """
    name_cache, meta, failed, done = {}, {}, [], 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(report_universes, server, headers, r, name_cache, meta, known_unx_ids): r
                   for r in reports}
        for f in as_completed(futures):
            rep = futures[f]
            done += 1
            try:
                rows = f.result()
            except Exception as e:
                failed.append({"report": rep["name"], "error": str(e)})
                rows = None
            if rows is not None:
                bad = [r["error"] for r in rows if r.get("error")]
                if bad:
                    failed.append({"report": rep["name"], "error": bad[0]})
                else:
                    cache[rep["id"]] = {
                        "name": rep["name"], "path": rep["path"], "server": server_key,
                        "rows": [{k: r[k] for k in ("query", "universe", "type", "status")} for r in rows],
                        "ts": datetime.now().isoformat(timespec="seconds"),
                    }
            if progress:
                progress(done, len(reports))
            if on_checkpoint and done % checkpoint_every == 0:
                on_checkpoint(cache)
    if on_checkpoint:
        on_checkpoint(cache)
    return failed


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
def bag_values(bag) -> list:
    """{'SI_TOTAL': n, '1': x, '2': y} -> [x, y]"""
    return [v for k, v in bag.items() if k != "SI_TOTAL"] if isinstance(bag, dict) else []


def reports_visible_to_groups(server, headers, group_ids, group_names: dict, kinds=("Webi", "CrystalReport"),
                              progress=None):
    """Reports each group may view (IsAllowed(group, 3)). Returns (rows, errors)."""
    kind_list = ",".join(f"'{k}'" for k in kinds)
    out, errors = [], []
    for n, gid in enumerate(group_ids, 1):
        q = (f"SELECT SI_ID, SI_NAME, SI_KIND, SI_OWNER, SI_PARENTID FROM CI_INFOOBJECTS "
             f"WHERE SI_KIND IN ({kind_list}) AND IsAllowed({gid}, 3)")
        rows, err = cmsquery_all(server, headers, q)
        if err:
            errors.append(f"{group_names.get(str(gid), gid)}: {err}")
        out += [{**r, "group_id": gid, "group": group_names.get(str(gid), "")} for r in rows]
        if progress:
            progress(n, len(group_ids))
    return out, errors


def folders_by_id(server, headers):
    entries, err = cmsquery_all(server, headers,
                                "SELECT SI_ID,SI_NAME,SI_PARENTID,SI_ROLES_ON_OBJECT FROM CI_INFOOBJECTS WHERE SI_KIND='Folder'")
    return {e["SI_ID"]: e for e in entries if "SI_ID" in e}, err


def path_of(folders: dict, parent_id) -> str:
    parts, seen = [], set()
    while parent_id in folders and parent_id not in seen:
        seen.add(parent_id)
        parts.insert(0, folders[parent_id].get("SI_NAME", ""))
        parent_id = folders[parent_id].get("SI_PARENTID")
    return "/".join(parts)


def report_roles(server, headers, kinds=("Webi", "CrystalReport")):
    """Roles assigned on each report plus those inherited from parent folders. Returns (rows, errors)."""
    kind_list = ",".join(f"'{k}'" for k in kinds)
    roles_raw, e1 = cmsquery_all(server, headers, "SELECT SI_ID,SI_NAME FROM CI_SYSTEMOBJECTS WHERE SI_KIND='CustomRole'")
    folders, e2 = folders_by_id(server, headers)
    reports, e3 = cmsquery_all(server, headers,
                               "SELECT SI_ID,SI_NAME,SI_KIND,SI_OWNER,SI_PARENTID,SI_ROLES_ON_OBJECT "
                               f"FROM CI_INFOOBJECTS WHERE SI_KIND IN ({kind_list}) AND SI_INSTANCE=0")
    errors = [e for e in (e1, e2, e3) if e]
    roles = {e["SI_ID"]: e["SI_NAME"] for e in roles_raw}

    def roles_of(entry):
        return [roles.get(int(r), str(r)) for r in bag_values(entry.get("SI_ROLES_ON_OBJECT"))]

    out = []
    for entry in reports:
        try:
            parts, inherited, seen = [], [], set()
            pid = entry.get("SI_PARENTID")
            while pid in folders and pid not in seen:
                seen.add(pid)
                f = folders[pid]
                parts.insert(0, f["SI_NAME"])
                inherited += [f"{r} ({f['SI_NAME']})" for r in roles_of(f)]
                pid = f.get("SI_PARENTID")
            out.append({"id": entry["SI_ID"], "name": entry["SI_NAME"], "kind": entry["SI_KIND"],
                        "owner": entry.get("SI_OWNER", ""), "path": "/".join(parts),
                        "own_roles": ", ".join(roles_of(entry)), "inherited_roles": ", ".join(inherited)})
        except Exception as e:
            errors.append(f"{entry.get('SI_NAME')}: {e}")
    return out, errors


def find_universes(server, headers, name_contains: str = ""):
    """Universes (.unv / .unx) in the CMS, optionally filtered by name."""
    q = ("SELECT SI_ID, SI_NAME, SI_KIND FROM CI_APPOBJECTS "
         "WHERE (SI_KIND='Universe' OR SI_KIND='DSL.Universe' OR SI_KIND='DSL.MetaDataFile')")
    safe = name_contains.strip().replace("'", "''")
    if safe:
        q += f" AND SI_NAME LIKE '%{safe}%'"
    return infostore_query(server, headers, q)
