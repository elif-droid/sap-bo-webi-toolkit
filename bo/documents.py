"""Web Intelligence document operations through the Raylight REST API."""

from __future__ import annotations

import requests

from bo.client import API_TIMEOUT, VERIFY_TLS, infostore_query, short
from bo.sqltools import extract_sql
from bo.text import norm

PUBLIC_FOLDERS_ROOT_ID = 23  # SI_ID of "Public Folders" on every BO system
WID_ACCEPT_TYPES = ["application/wid", "application/x-wid", "application/vnd.sap.webi.wid", "application/octet-stream"]


def _raylight(server: str) -> str:
    return f"{server}/raylight/v1"


def _as_list(x):
    return [x] if isinstance(x, dict) else (x or [])


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def get_document_meta(server, headers, doc_id):
    """Returns (name, folderId, raw_document_dict)."""
    try:
        res = requests.get(f"{_raylight(server)}/documents/{doc_id}", headers=headers,
                           verify=VERIFY_TLS, timeout=15)
        if res.status_code == 200:
            doc = res.json().get("document", {}) or {}
            return doc.get("name"), doc.get("folderId"), doc
    except Exception:
        pass
    return None, None, {}


def get_document_name(server, headers, doc_id) -> str:
    """Name from Raylight, falling back to the CMS, then to 'Report_<id>'."""
    name, _, _ = get_document_meta(server, headers, doc_id)
    if name:
        return name
    entries, _ = infostore_query(server, headers, f"SELECT SI_NAME FROM CI_INFOOBJECTS WHERE SI_ID={doc_id}")
    if entries and entries[0].get("SI_NAME"):
        return entries[0]["SI_NAME"]
    return f"Report_{doc_id}"


def folder_path(server, headers, folder_id, cache: dict) -> str:
    """Readable folder path built by walking SI_PARENT_FOLDER upwards."""
    if not folder_id or str(folder_id) in ("0", str(PUBLIC_FOLDERS_ROOT_ID)):
        return "Public Folders"
    fid = str(folder_id)
    if fid in cache:
        return cache[fid]
    parts, seen, cur = [], set(), fid
    for _ in range(12):
        if not cur or cur in seen or cur in ("0", str(PUBLIC_FOLDERS_ROOT_ID)):
            break
        seen.add(cur)
        entries, _ = infostore_query(server, headers,
                                     f"SELECT SI_NAME, SI_PARENT_FOLDER FROM CI_INFOOBJECTS WHERE SI_ID={cur}")
        if not entries:
            break
        if entries[0].get("SI_NAME"):
            parts.append(str(entries[0]["SI_NAME"]))
        parent = entries[0].get("SI_PARENT_FOLDER")
        cur = str(parent) if parent is not None else ""
    path = "Public Folders" + "".join(f" / {p}" for p in reversed(parts))
    cache[fid] = path
    return path


def find_reports_by_name(server, headers, name: str, with_path: bool = False) -> list[dict]:
    """Exact-name Webi lookup. Returns [{id, name[, path]}]."""
    safe = str(name).strip().replace("'", "''")
    if not safe:
        return []
    cols = "SI_ID, SI_NAME" + (", SI_PARENT_FOLDER" if with_path else "")
    entries, _ = infostore_query(server, headers,
                                 f"SELECT {cols} FROM CI_INFOOBJECTS WHERE SI_KIND='Webi' AND SI_NAME='{safe}'")
    out, cache = [], {}
    for e in entries or []:
        if e.get("SI_ID") is None:
            continue
        item = {"id": str(e["SI_ID"]), "name": e.get("SI_NAME", "")}
        if with_path:
            item["path"] = folder_path(server, headers, e.get("SI_PARENT_FOLDER"), cache)
        out.append(item)
    exact = [o for o in out if o["name"].strip() == str(name).strip()]
    return exact or out


# ---------------------------------------------------------------------------
# Data providers
# ---------------------------------------------------------------------------
def get_dataproviders(server, headers, doc_id):
    """Returns (data_providers, error). Each item gets `_docId` for later lookups."""
    try:
        res = requests.get(f"{_raylight(server)}/documents/{doc_id}/dataproviders",
                           headers=headers, verify=VERIFY_TLS, timeout=API_TIMEOUT)
    except requests.RequestException as e:
        return [], f"Data provider request failed: {e}"
    if res.status_code != 200:
        return [], f"Could not read data providers (HTTP {res.status_code}): {short(res)}"
    dps = _as_list(res.json().get("dataproviders", {}).get("dataprovider", []))
    for dp in dps:
        if isinstance(dp, dict):
            dp["_docId"] = str(doc_id)
    return dps, None


def get_dataprovider(server, headers, doc_id, dp_id) -> dict:
    try:
        r = requests.get(f"{_raylight(server)}/documents/{doc_id}/dataproviders/{dp_id}",
                         headers=headers, verify=VERIFY_TLS, timeout=API_TIMEOUT)
        if r.status_code == 200:
            return r.json().get("dataprovider", {}) or {}
    except Exception:
        pass
    return {}


def dataprovider_source_id(server, headers, doc_id, dp_id) -> str | None:
    d = get_dataprovider(server, headers, doc_id, dp_id)
    return str(d.get("dataSourceId")) if d else None


def get_dataprovider_sql(server, headers, doc_id, dp_id):
    """Raw /specification of a data provider (read-only). Returns (text, error)."""
    url = f"{_raylight(server)}/documents/{doc_id}/dataproviders/{dp_id}/specification"
    try:
        res = requests.get(url, headers=headers, verify=VERIFY_TLS, timeout=API_TIMEOUT)
    except requests.RequestException as e:
        return None, f"request failed: {e}"
    if res.status_code != 200:
        return None, f"HTTP {res.status_code}: {short(res)}"
    return res.text, None


def get_dataprovider_sql_robust(server, headers, doc_id, dp_id):
    """Tries several endpoints and Accept headers (some servers answer 406 to one format).

    Returns (sql, error, source_label).
    """
    base = f"{_raylight(server)}/documents/{doc_id}/dataproviders/{dp_id}"
    tries, raw = [], None
    for suffix, label in (("/specification", "specification"), ("/queryplan", "queryplan"), ("", "details")):
        for accept in ("application/xml", "application/json", "*/*"):
            h = {**headers, "Accept": accept}
            try:
                r = requests.get(base + suffix, headers=h, verify=VERIFY_TLS, timeout=API_TIMEOUT)
            except requests.RequestException as e:
                tries.append(f"{label}[{accept}] request error: {e}")
                continue
            if r.status_code != 200:
                tries.append(f"{label}[{accept}] HTTP {r.status_code}")
                continue
            sql = extract_sql(r.text)
            if sql:
                return sql, None, f"{label} [{accept}]"
            raw = raw or (label, accept, (r.text or "")[:600])
            break  # 200 without SQL: another Accept header will not change that
    msg = " | ".join(tries)
    if raw:
        msg += f" || 200 OK but no SQL found in {raw[0]}[{raw[1]}]: {raw[2]}"
    return None, msg, None


def total_rows(server, headers, doc_id):
    """Returns (sum of rowCount over data providers | None, [dp ids])."""
    try:
        r = requests.get(f"{_raylight(server)}/documents/{doc_id}/dataproviders",
                         headers=headers, verify=VERIFY_TLS, timeout=60)
        dps = _as_list(r.json().get("dataproviders", {}).get("dataprovider", []))
        return sum(int(d.get("rowCount", 0) or 0) for d in dps), [str(d.get("id")) for d in dps]
    except Exception:
        return None, []


def fetch_rows(server, headers, doc_id, page_size: int = 500, max_pages: int = 400):
    """Reads refreshed data provider rows (read-only).

    Rows live under /dataproviders/{id}/flows/{flowId}. Large reports hit the
    "maximum character output size" limit (WIS 30272), so rows are paged and the page
    size is halved when that error appears. Returns (columns, rows, debug, error).
    """
    debug = {"report": str(doc_id), "attempts": []}
    dps, err = get_dataproviders(server, headers, doc_id)
    if err:
        return [], [], debug, err

    columns, rows = [], []
    for dp in dps:
        dp_id = dp.get("id")
        try:
            flow_count = int(dp.get("flowCount", 1) or 1)
        except (TypeError, ValueError):
            flow_count = 1
        for flow_id in range(max(flow_count, 1)):
            url = f"{_raylight(server)}/documents/{doc_id}/dataproviders/{dp_id}/flows/{flow_id}"
            page, start, got_any = page_size, 1, False
            for _ in range(max_pages):
                try:
                    r = requests.get(url, headers=headers, verify=VERIFY_TLS,
                                     params={"startIndex": start, "pageSize": page}, timeout=120)
                except requests.RequestException as e:
                    debug["attempts"].append(f"{dp_id}/flow{flow_id}@{start}: {e}")
                    break
                if r.status_code != 200:
                    debug["attempts"].append(f"{dp_id}/flow{flow_id}@{start} (page={page}): HTTP {r.status_code}")
                    if "30272" in (r.text or "") and page > 50:
                        page = max(50, page // 2)
                        continue
                    break
                try:
                    flow = r.json()
                except ValueError:
                    break
                flow = flow.get("flow", flow)

                if not columns:
                    meta = flow.get("metadata", {})
                    values = meta.get("value", meta.get("column", [])) if isinstance(meta, dict) else []
                    for c in _as_list(values):
                        name = (c.get("$") or c.get("@name") or c.get("name") or c.get("@id") or "") \
                            if isinstance(c, dict) else str(c)
                        if name and name not in columns:
                            columns.append(name)

                raw_rows = _as_list(flow.get("row", []))
                if not raw_rows and isinstance(flow.get("data"), dict):
                    raw_rows = _as_list(flow["data"].get("row", []))

                count = 0
                for rr in raw_rows:
                    cells = _as_list(rr.get("value", rr.get("cell", [])) if isinstance(rr, dict) else rr)
                    vals = [str(c.get("$", c.get("value", c.get("#text", "")))) if isinstance(c, dict) else str(c)
                            for c in cells]
                    if columns and len(vals) == len(columns):
                        rows.append(dict(zip(columns, vals)))
                    elif vals:
                        rows.append({(columns[i] if i < len(columns) else f"col{i}"): v for i, v in enumerate(vals)})
                    count += 1
                debug["attempts"].append(f"{dp_id}/flow{flow_id}@{start} (page={page}): {count} rows")
                got_any = got_any or count > 0
                if count < page:
                    break
                start += page
            if got_any:
                break

    if not columns and rows:
        columns = sorted({k for r in rows for k in r})
    if not rows:
        return columns, rows, debug, "No rows could be parsed."
    return columns, rows, debug, None


# ---------------------------------------------------------------------------
# Save / purge
# ---------------------------------------------------------------------------
def save_document(server, headers, doc_id):
    """Saves the document in place. Returns (ok, detail)."""
    try:
        res = requests.put(f"{_raylight(server)}/documents/{doc_id}", headers=headers,
                           verify=VERIFY_TLS, timeout=60)
    except requests.RequestException as e:
        return False, f"Save request failed: {e}"
    if res.status_code in (200, 204) and "not been modified" in (res.text or "").lower():
        return False, "Server reports nothing to save (the change was not applied in this session): " + short(res)
    if res.status_code in (200, 204):
        return True, f"Saved: {short(res)[:120]}"
    return False, f"Save failed (HTTP {res.status_code}): {short(res)}"


def purge_data(server, headers, doc_id, log: list | None = None):
    """Purges report data so test data is never saved. Verified by rowCount dropping to 0."""
    doc_url = f"{_raylight(server)}/documents/{doc_id}"
    _, dp_ids = total_rows(server, headers, doc_id)
    candidates = [("PUT document ?purge=true",
                   lambda: requests.put(doc_url, headers=headers, params={"purge": "true"},
                                        verify=VERIFY_TLS, timeout=120))]
    for dp_id in dp_ids:
        dp_url = f"{doc_url}/dataproviders/{dp_id}"
        candidates += [
            (f"PUT dp {dp_id} ?operation=purge",
             lambda u=dp_url: requests.put(u, headers=headers, params={"operation": "purge"},
                                           verify=VERIFY_TLS, timeout=120)),
            (f"PUT dp {dp_id} dataSourceState=Purged",
             lambda u=dp_url: requests.put(u, headers=headers, json={"dataprovider": {"dataSourceState": "Purged"}},
                                           verify=VERIFY_TLS, timeout=120)),
        ]
    tried = []
    for label, fn in candidates:
        try:
            res = fn()
            status, body = res.status_code, (res.text or "").strip().replace("\n", " ")[:400]
        except requests.RequestException as e:
            status, body = "request error", str(e)
        rows, _ = total_rows(server, headers, doc_id)
        line = f"{label} -> HTTP {status}; rows={rows}; {body}"
        tried.append(line)
        if log is not None:
            log.append(line)
        if rows == 0:
            return True, f"purge OK ({label})"
    return False, "purge could not be verified: " + " || ".join(tried)


# ---------------------------------------------------------------------------
# Prompts and refresh
# ---------------------------------------------------------------------------
def _prompt_values(server, headers, doc_id, prompt_id) -> list[str]:
    """All list-of-values entries for a prompt, following pagination."""
    base = f"{_raylight(server)}/documents/{doc_id}/parameters/{prompt_id}"

    def extract(data):
        param = data.get("parameter", data)
        answer = param.get("answer", {}) if isinstance(param, dict) else {}
        info = answer.get("info", {}) if isinstance(answer, dict) else {}
        lov = info.get("lov", {}) if isinstance(info, dict) else {}
        vals = lov.get("values", {}).get("value", []) if isinstance(lov, dict) and isinstance(lov.get("values"), dict) else []
        if not vals:
            vals = data.get("values", {}).get("value", []) or answer.get("values", {}).get("value", [])
        out = []
        for v in _as_list(vals):
            s = (v.get("$") or v.get("value") or v.get("caption") or v.get("id")) if isinstance(v, dict) else v
            if s is not None:
                out.append(str(s))
        return out

    values, seen, start, page = [], set(), 1, 200
    for _ in range(60):
        try:
            res = requests.get(base, headers=headers, verify=VERIFY_TLS,
                               params={"startIndex": start, "pageSize": page}, timeout=90)
            if res.status_code != 200:
                break
            batch = extract(res.json())
        except (requests.RequestException, ValueError):
            break
        new = [v for v in batch if v not in seen]
        seen.update(new)
        values.extend(new)
        if not new or len(batch) < page:
            break
        start += page
    return values


def refresh_and_count(server, headers, doc_id, date_start: str, date_end: str,
                      debug: list | None = None, tag: str = "", timeout: int = 180,
                      end_keywords: tuple[str, ...] = ("end", "until", "finish")):
    """Answers every prompt (all values; dates get start/end), refreshes, returns (total_rows, detail).

    A date prompt whose name contains one of `end_keywords` gets `date_end`, any other gets `date_start`.
    """
    dbg = {"report": str(doc_id), "stage": tag}
    if debug is not None:
        debug.append(dbg)
    url = f"{_raylight(server)}/documents/{doc_id}/parameters"
    try:
        p_res = requests.get(url, headers=headers, verify=VERIFY_TLS, timeout=90)
    except requests.RequestException as e:
        return None, f"could not read prompts: {e}"
    if p_res.status_code != 200:
        return None, f"prompts HTTP {p_res.status_code}: {short(p_res)}"
    try:
        params = _as_list(p_res.json().get("parameters", {}).get("parameter", []))
    except ValueError:
        params = []
    dbg["prompts"] = len(params)

    answers = []
    for p in params:
        pid = p.get("id")
        name = str(p.get("name", "")).lower()
        ptype = p.get("answer", {}).get("@type", p.get("type", "String"))
        if ptype in ("Date", "DateTime"):
            value = date_end if any(norm(k) in norm(name) for k in end_keywords) else date_start
            answers.append({"id": pid, "answer": {"values": {"value": [value]}}})
            continue
        vals = _prompt_values(server, headers, doc_id, pid)
        cardinality = str(p.get("answer", {}).get("info", {}).get("@cardinality", "")).lower()
        if cardinality != "multiple" and len(vals) > 1:
            vals = vals[:1]
        if vals:
            answers.append({"id": pid, "answer": {"values": {"value": vals}}})
    dbg["answered"] = len(answers)

    try:
        body = {"parameters": {"parameter": answers}} if answers else None
        r = requests.put(url, json=body, headers=headers, verify=VERIFY_TLS, timeout=timeout)
        dbg["refresh"] = f"HTTP {r.status_code}"
    except requests.exceptions.Timeout:
        return None, f"refresh did not finish within {timeout}s"
    except requests.RequestException as e:
        return None, f"refresh request failed: {e}"

    total, ids = total_rows(server, headers, doc_id)
    if total is None:
        return None, "could not read row counts"
    dbg["rows"] = total
    return total, f"{len(ids)} queries, {total} rows"


def clear_prompts(server, headers, doc_id, log: list | None = None):
    """Empties saved prompt answers (one PUT with empty values), then verifies."""
    url = f"{_raylight(server)}/documents/{doc_id}/parameters"
    try:
        res = requests.get(url, headers=headers, verify=VERIFY_TLS, timeout=60)
        params = _as_list(res.json().get("parameters", {}).get("parameter", [])) if res.status_code == 200 else None
    except (requests.RequestException, ValueError) as e:
        return False, f"could not read prompts: {e}"
    if params is None:
        return False, f"prompts HTTP {res.status_code}"
    if not params:
        return True, "no prompts"
    ids = [p.get("id") for p in params]
    body = {"parameters": {"parameter": [{"id": i, "answer": {"values": {"value": []}}} for i in ids]}}
    try:
        requests.put(url, headers=headers, json=body, verify=VERIFY_TLS, timeout=60)
    except requests.RequestException as e:
        return False, f"PUT failed: {e}"

    still = []
    try:
        rr = requests.get(url, headers=headers, verify=VERIFY_TLS, timeout=60)
        for p in _as_list(rr.json().get("parameters", {}).get("parameter", [])):
            if _as_list(p.get("answer", {}).get("values", {}).get("value", [])):
                still.append(str(p.get("name")))
    except Exception:
        pass
    if log is not None:
        log.append(f"clear prompts; still filled: {still or 'none'}")
    if still:
        return False, f"mandatory prompts could not be cleared: {', '.join(still)}"
    return True, f"{len(ids)} prompts cleared"


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------
def _success_id(res) -> str | None:
    try:
        body = res.json()
        succ = body.get("success", body) if isinstance(body, dict) else None
        if isinstance(succ, dict) and succ.get("id") is not None:
            return str(succ["id"])
    except ValueError:
        pass
    return None


def download_wid(server, headers, doc_id):
    """Raw .wid file. Returns (bytes | None, content_type, error)."""
    attempts = []
    url = f"{_raylight(server)}/documents/{doc_id}"
    for accept in WID_ACCEPT_TYPES:
        h = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        h["Accept"] = accept
        try:
            res = requests.get(url, headers=h, verify=VERIFY_TLS, timeout=180)
        except requests.RequestException as e:
            attempts.append(f"{accept}: {e}")
            continue
        ct = (res.headers.get("Content-Type") or "").lower()
        if res.status_code == 200 and res.content and not any(t in ct for t in ("json", "xml", "html")):
            return res.content, ct, None
        attempts.append(f"{accept} -> HTTP {res.status_code} ({ct or '?'}, {len(res.content or b'')} bytes)")
    return None, None, " | ".join(attempts)


def _wid_multipart(wid_bytes: bytes) -> dict:
    infos = '<attachmentInfos><attachmentInfo name="document.wid" type="application/wid"/></attachmentInfos>'
    return {
        "attachmentInfos": (None, infos, "application/xml"),
        "attachmentContent": ("document.wid", wid_bytes, "application/octet-stream"),
    }


def upload_wid(server, headers, wid_bytes, name, folder_id):
    """Uploads a .wid as a new document, then saves it with a name into a folder. Returns (new_id, detail)."""
    h = {k: v for k, v in headers.items() if k.lower() != "content-type"}
    h["Accept"] = "application/json"
    try:
        res = requests.post(f"{_raylight(server)}/documents", headers=h, files=_wid_multipart(wid_bytes),
                            verify=VERIFY_TLS, timeout=300)
    except requests.RequestException as e:
        return None, f"upload failed: {e}"
    if res.status_code not in (200, 201):
        return None, f"upload HTTP {res.status_code}: {short(res)}"
    new_id = _success_id(res)
    if not new_id:
        return None, f"upload response has no id: {short(res)}"
    try:
        save = requests.put(f"{_raylight(server)}/documents/{new_id}", headers=headers,
                            json={"document": {"name": name, "folderId": int(folder_id)}},
                            verify=VERIFY_TLS, timeout=120)
    except requests.RequestException as e:
        return None, f"uploaded (id {new_id}) but save failed: {e}"
    if save.status_code not in (200, 201):
        return None, f"uploaded (id {new_id}) but save HTTP {save.status_code}: {short(save)}"
    return _success_id(save) or new_id, f"uploaded {new_id}"


def document_exists(server, headers, doc_id):
    if not doc_id or str(doc_id) in ("None", "?"):
        return None, "no id"
    name, _, raw = get_document_meta(server, headers, doc_id)
    if raw:
        return True, f"id {doc_id} exists ({name})"
    return False, f"id {doc_id} could not be read"


def webi_exists_by_name(server, headers, name):
    """True / False / None (could not check). Also matches names BO suffixed, e.g. 'x (1)'."""
    safe = name.replace("'", "''")
    entries, err = infostore_query(server, headers,
                                   f"SELECT SI_ID FROM CI_INFOOBJECTS WHERE SI_KIND='Webi' AND SI_NAME='{safe}'")
    if err is None and entries:
        return True
    entries2, err2 = infostore_query(server, headers,
                                     f"SELECT SI_ID FROM CI_INFOOBJECTS WHERE SI_KIND='Webi' AND SI_NAME LIKE '{safe}%'")
    if err2 is None:
        return len(entries2) > 0
    return False if err is None else None


def copy_exists_in_folder(server, headers, name, folder_id):
    """Is there a Webi document with this exact name inside the folder? Returns (found, detail)."""
    safe = name.replace("'", "''")
    entries, err = infostore_query(
        server, headers,
        f"SELECT SI_ID FROM CI_INFOOBJECTS WHERE SI_KIND='Webi' AND SI_PARENTID={int(folder_id)} AND SI_NAME='{safe}'")
    if err is None:
        return (True, f"copy found (SI_ID={entries[0].get('SI_ID')})") if entries else (False, "no copy with this name")
    try:
        res = requests.get(f"{server}/infostore/{int(folder_id)}/children", headers=headers,
                           params={"page": 1, "pageSize": 500}, verify=VERIFY_TLS, timeout=30)
        if res.status_code == 200:
            data = res.json()
            for it in _as_list(data.get("entries", data.get("entry", []))):
                kind = str(it.get("SI_KIND") or it.get("type") or "")
                if norm(it.get("SI_NAME") or it.get("name")) == norm(name) and (not kind or kind.lower() == "webi"):
                    return True, f"copy found (SI_ID={it.get('SI_ID') or it.get('id')})"
            return False, "no copy with this name"
        return None, f"folder could not be listed: HTTP {res.status_code}"
    except Exception as e:
        return None, f"folder could not be listed: {e}"


def backup_name(name: str, doc_id) -> str:
    return f"{name}_backup_{doc_id}"


def backup_document(server, headers, doc_id, name, folder_id, mode: str = "auto"):
    """Copies the report into `folder_id` before it is changed.

    1. POST /documents?sourceId=<id>  (server-side copy, preferred)
    2. download the .wid and upload it as a new document (never touches the original)
    mode="verify_only": do not copy; check that a same-named copy already exists in the folder
    (e.g. copies made by hand in BI Launch Pad).
    Returns (ok, message). When ok is False the report must not be converted.
    """
    if mode == "verify_only":
        found, detail = copy_exists_in_folder(server, headers, name, folder_id)
        if found:
            return True, f"Existing copy verified: {detail}"
        if found is False:
            return False, f"{detail}; report not converted. Copy it in BI Launch Pad first."
        return False, f"Copy could not be verified, stopped for safety: {detail}"

    target = backup_name(name, doc_id)
    attempts = []
    try:
        res = requests.post(f"{_raylight(server)}/documents", headers=headers,
                            params={"sourceId": str(doc_id)},
                            json={"document": {"name": target, "folderId": int(folder_id)}},
                            verify=VERIFY_TLS, timeout=120)
        if res.status_code in (200, 201):
            new_id = _success_id(res)
            if new_id and new_id != str(doc_id):
                ok, detail = document_exists(server, headers, new_id)
                if ok or webi_exists_by_name(server, headers, target):
                    return True, f"Backed up (server copy): {target}, id {new_id}"
                attempts.append(f"sourceId copy returned id {new_id} but it could not be verified ({detail})")
            elif webi_exists_by_name(server, headers, target):
                return True, f"Backed up (server copy): {target}"
            else:
                attempts.append(f"sourceId copy: HTTP {res.status_code} but no new document")
        else:
            attempts.append(f"sourceId copy: HTTP {res.status_code} {short(res)}")
    except requests.RequestException as e:
        attempts.append(f"sourceId copy failed: {e}")

    wid, _, dl_err = download_wid(server, headers, doc_id)
    if wid:
        new_id, detail = upload_wid(server, headers, wid, target, folder_id)
        if new_id and webi_exists_by_name(server, headers, target) is not False:
            return True, f"Backed up (.wid copy): {target}, id {new_id}"
        attempts.append(f".wid upload: {detail}")
    else:
        attempts.append(f".wid download: {dl_err}")
    return False, "Backup failed -> " + " || ".join(attempts)


def find_backup(server, headers, name, doc_id):
    """Latest backup created by `backup_document` for this report. Returns (id, backup_name, error)."""
    target = backup_name(name, doc_id)
    hits = find_reports_by_name(server, headers, target)
    if not hits:
        return None, target, f"No backup named '{target}' found."
    return max(hits, key=lambda h: int(h["id"]))["id"], target, None


def restore_from_backup(server, headers, doc_id, backup_id):
    """Writes the backup's .wid content over the original (same SI_ID, name and folder).

    Unlike reverse mapping this also works for .unx → .unv, because the whole document
    is replaced rather than re-pointed.
    """
    wid, _, err = download_wid(server, headers, backup_id)
    if not wid:
        return False, f"Backup content could not be downloaded: {err}"
    h = {k: v for k, v in headers.items() if k.lower() != "content-type"}
    h["Accept"] = "application/json"
    try:
        res = requests.put(f"{_raylight(server)}/documents/{doc_id}", headers=h, files=_wid_multipart(wid),
                           verify=VERIFY_TLS, timeout=300)
    except requests.RequestException as e:
        return False, f"Upload failed: {e}"
    if res.status_code not in (200, 201, 204):
        return False, f"Backup content could not be written: HTTP {res.status_code} {short(res)}"
    saved, msg = save_document(server, headers, doc_id)
    if saved or "nothing to save" in msg.lower():
        return True, "Original restored from backup and saved."
    return False, f"Content uploaded but not saved: {msg}"


# ---------------------------------------------------------------------------
# API discovery
# ---------------------------------------------------------------------------
WADL_PATHS = ["/raylight/v1/application.wadl", "/raylight/v1?_wadl", "/application.wadl", "/?_wadl",
              "/v1/application.wadl"]


def fetch_wadl(server, headers):
    """Tries the usual WADL locations. Returns [(url, status, text | None)]."""
    out = []
    h = {k: v for k, v in headers.items() if k.lower() != "content-type"}
    h["Accept"] = "application/vnd.sun.wadl+xml, application/xml, */*"
    for path in WADL_PATHS:
        url = f"{server}{path}"
        try:
            res = requests.get(url, headers=h, verify=VERIFY_TLS, timeout=20)
            ok = res.status_code == 200 and bool((res.text or "").strip())
            out.append((url, res.status_code, res.text if ok else None))
        except Exception as e:
            out.append((url, f"error: {e}", None))
    return out


def summarize_wadl(wadl_text: str, keywords=("copy", "wid", "save", "export", "download", "right", "secur", "acl")):
    """Lists 'METHOD path  mediaTypes' lines from a WADL document. Returns (all_lines, keyword_hits)."""
    import re
    text = re.sub(r"<(/?)\w+:", r"<\1", wadl_text or "")  # drop namespace prefixes
    lines = []
    for m in re.finditer(r'<resource[^>]*path="([^"]+)"[^>]*>(.*?)</resource>', text, flags=re.S):
        path, body = m.group(1), m.group(2)
        for method, mbody in re.findall(r'<method[^>]*name="([A-Z]+)"[^>]*>(.*?)</method>', body, flags=re.S):
            media = sorted(set(re.findall(r'mediaType="([^"]+)"', mbody)))
            lines.append(f"{method:6s} {path}    {', '.join(media) if media else '-'}")
    if not lines:
        lines = sorted(set(re.findall(r'path="([^"]+)"', text)))
    return lines, [ln for ln in lines if any(k in ln.lower() for k in keywords)]
