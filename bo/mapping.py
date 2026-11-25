"""Re-pointing Webi data providers from one universe to another (e.g. .unv → .unx)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import requests

from bo.client import API_TIMEOUT, VERIFY_TLS, infostore_query, short
from bo.documents import (dataprovider_source_id, get_dataprovider, get_dataprovider_sql,
                          get_dataproviders, save_document)
from bo.sqltools import compare_structure
from bo.text import norm

ALREADY_CONVERTED = "ALREADY_CONVERTED"


# ---------------------------------------------------------------------------
# Universe names and target resolution
# ---------------------------------------------------------------------------
def universe_key(label_or_name: str) -> str:
    """'Sales.unx (ID: 123)' or 'Sales' -> 'sales'."""
    s = str(label_or_name).split("(ID")[0]
    s = re.sub(r"\.un[xv]\b", "", s, flags=re.IGNORECASE).strip()
    return norm(s)


def build_name_rules(universes: dict[str, str], preferred: dict[str, str] | None = None) -> dict[str, list]:
    """{universe_key: [(name, id), ...]} from {name: id}.

    If several target universes share a name, `preferred` ({name: id}) picks one.
    """
    rules: dict[str, list] = {}
    for name, uid in universes.items():
        rules.setdefault(universe_key(name), []).append((name, str(uid)))
    for name, pref_id in (preferred or {}).items():
        key = universe_key(name)
        opts = rules.get(key, [])
        chosen = [o for o in opts if o[1] == str(pref_id)]
        if len(opts) > 1 and chosen:
            rules[key] = chosen
    return rules


def source_universe_name(server, headers, dp: dict, cache: dict) -> tuple[str, str]:
    """Name of the universe a data provider currently uses. Returns (name, where_found)."""
    for f in ("dataSourceName", "universeName"):
        if dp.get(f):
            return str(dp[f]), f"dp.{f}"
    path = dp.get("dataSourcePath")
    # dataSourcePath is often just a folder ("[Universes]/Sales/"), which is not a name
    if path and not str(path).rstrip().endswith("/") and "/" not in str(path).strip("/"):
        return str(path), "dp.dataSourcePath"

    ds_id, doc_id, dp_id = str(dp.get("dataSourceId")), str(dp.get("_docId", "")), str(dp.get("id"))
    key = f"{doc_id}:{ds_id}"
    if key in cache:
        return cache[key]

    if doc_id and dp_id:
        d = get_dataprovider(server, headers, doc_id, dp_id)
        name = d.get("dataSourceName") or d.get("universeName") or d.get("dataSourcePath")
        if name:
            cache[key] = (str(name), "dp details")
            return cache[key]

    if doc_id:
        try:
            r = requests.get(f"{server}/raylight/v1/documents/{doc_id}/datasources", headers=headers,
                             verify=VERIFY_TLS, timeout=API_TIMEOUT)
            if r.status_code == 200:
                dss = r.json().get("datasources", {}).get("datasource", [])
                for ds in [dss] if isinstance(dss, dict) else dss:
                    if str(ds.get("id")) == ds_id and (ds.get("name") or ds.get("dataSourceName")):
                        cache[key] = (str(ds.get("name") or ds.get("dataSourceName")), "datasources")
                        return cache[key]
        except Exception:
            pass

    entries, _ = infostore_query(server, headers, f"SELECT SI_NAME FROM CI_INFOOBJECTS WHERE SI_ID={ds_id}")
    if entries and entries[0].get("SI_NAME"):
        cache[key] = (str(entries[0]["SI_NAME"]), "cms")
        return cache[key]
    cache[key] = ("", "not found")
    return cache[key]


def make_name_resolver(server, headers, key_to_id: dict[str, str], id_overrides: dict[str, str] | None = None):
    """Resolver that picks the target universe whose name equals the source universe name.

    `id_overrides` ({source_id: target_id}) wins and covers sources whose name cannot be read.
    resolver(dp) -> (target_id | None, reason, pending_universe_name | None)
    """
    cache: dict = {}
    id_overrides = id_overrides or {}

    def resolver(dp):
        ds_id = str(dp.get("dataSourceId"))
        if ds_id in id_overrides:
            return id_overrides[ds_id], f"manual rule {ds_id} → {id_overrides[ds_id]}", None
        name, where = source_universe_name(server, headers, dp, cache)
        if not name:
            return None, f"source universe name unreadable (dataSourceId {ds_id})", f"Unknown universe (ID {ds_id})"
        target = key_to_id.get(universe_key(name))
        if target:
            return target, f"'{name}' [{where}] matched by name", None
        clean = re.sub(r"\.un[xv]\b", "", name, flags=re.IGNORECASE).strip()
        return None, f"no target universe defined yet for '{name}' [{where}]", clean or name

    return resolver


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------
def find_unmapped(mapping_json) -> tuple[list[str], list[str]]:
    """Objects the target universe cannot map.

    Response shape: {"mappings": {"content": {"mapping": [{"@status": "Ok", "source": {...}, "target": {...}}]}}}
    - blocking: no target at all → the query must not be converted
    - ambiguous: status is not "Ok" but BO still proposes a target → applied, flagged for manual review
    """
    blocking, ambiguous = [], []

    def scan(node):
        if isinstance(node, dict):
            if "source" in node:
                status = str(node.get("@status", node.get("status", "Ok")))
                target = node.get("target") or {}
                target_id = target.get("id") if isinstance(target, dict) else target
                if status.lower() != "ok":
                    src = node.get("source") or {}
                    src_txt = (src.get("name") or src.get("id") or str(src)) if isinstance(src, dict) else str(src)
                    if target_id:
                        ambiguous.append(f"{src_txt} → {target_id} [{status}]")
                    else:
                        blocking.append(f"{src_txt} [{status}]")
            for v in node.values():
                scan(v)
        elif isinstance(node, list):
            for v in node:
                scan(v)

    scan(mapping_json)
    return blocking, ambiguous


def _mapping_url(server, doc_id):
    return f"{server}/raylight/v1/documents/{doc_id}/dataproviders/mappings"


def apply_mapping(server, headers, doc_id, dp_id, target_id, mapping, debug: dict | None = None):
    """Applies a mapping proposal by POSTing it back to the same endpoint, then verifies
    that the data provider now points at the target. (PUT only echoes the proposal.)"""
    params = {"originDataproviderIds": dp_id, "targetDatasourceId": int(target_id)}
    try:
        res = requests.post(_mapping_url(server, doc_id), headers=headers, params=params, json=mapping,
                            verify=VERIFY_TLS, timeout=60)
        status, body = res.status_code, short(res)
    except requests.RequestException as e:
        status, body = "request error", str(e)
    now = dataprovider_source_id(server, headers, doc_id, dp_id)
    if debug is not None:
        debug["apply"] = f"POST mappings -> HTTP {status}; dataSourceId={now}; {body[:160]}"
    if now == str(target_id):
        return True, "applied"
    return False, f"mapping did not change dataSourceId (HTTP {status}): {body[:160]}"


@dataclass
class ConversionResult:
    query_count: int = 0
    applied: int = 0
    error: str | None = None
    old_map: dict = field(default_factory=dict)        # {dp_id: previous dataSourceId}
    skipped: list = field(default_factory=list)        # queries with no target yet
    ambiguous: list = field(default_factory=list)      # applied but need a manual check
    pending: list = field(default_factory=list)        # [(target_key, universe_name)]
    sql_summary: str = ""
    debug: list = field(default_factory=list)

    @property
    def sql_changed(self) -> bool:
        return "⚠️" in self.sql_summary


def convert_document(server, headers, doc_id, target_id=None, apply=True, resolver=None,
                     universe_names: dict | None = None) -> ConversionResult:
    """Maps every data provider of a report to its target universe.

    Either a fixed `target_id` for all queries or a `resolver(dp)` per query.
    With apply=False nothing is changed (dry run). After each applied mapping the old
    and new SQL are compared structurally. When `error` is set the report must NOT be saved.
    """
    r = ConversionResult()
    dps, err = get_dataproviders(server, headers, doc_id)
    if err:
        r.error = err
        return r
    r.query_count = len(dps)
    if not dps:
        r.error = "No data providers found (wrong id or missing rights)."
        return r

    # Re-open the original so it is the session's active document (a copy may be active after a backup).
    try:
        requests.get(f"{server}/raylight/v1/documents/{doc_id}", headers=headers, verify=VERIFY_TLS,
                     timeout=API_TIMEOUT)
    except Exception:
        pass

    errors, sql_diffs = [], []
    sql_same = sql_checked = already = 0
    for dp in dps:
        dp_id, dp_name = str(dp.get("id")), dp.get("name", str(dp.get("id")))
        r.old_map[dp_id] = str(dp.get("dataSourceId", "?"))
        dbg = {"report": str(doc_id), "dp": f"{dp_name} ({dp_id})", "from": str(dp.get("dataSourceId")),
               "type": str(dp.get("dataSourceType"))}
        r.debug.append(dbg)

        if resolver is not None:
            target, why, pending_name = resolver(dp)
            dbg["resolver"] = why
            if not target:
                r.skipped.append(f"{dp_name}: {why}")
                if pending_name:
                    r.pending.append((f"NAME:{norm(pending_name)}", pending_name))
                continue
        else:
            target = target_id
        dbg["to"] = str(target)

        if str(dp.get("dataSourceId")) == str(target):
            dbg["status"] = "already on target"
            already += 1
            continue

        params = {"originDataproviderIds": dp_id, "targetDatasourceId": int(target)}
        try:
            res = requests.get(_mapping_url(server, doc_id), headers=headers, params=params,
                               verify=VERIFY_TLS, timeout=API_TIMEOUT)
        except requests.RequestException as e:
            errors.append(f"[{dp_name}] mapping request failed: {e}")
            continue
        if res.status_code != 200:
            body = short(res)
            if "not exist" in body.lower():
                # Target universe not published yet: skip this query, keep the report convertible.
                uname = (universe_names or {}).get(str(target), f"ID {target}")
                r.skipped.append(f"{dp_name}: target universe '{uname}' ({target}) does not exist yet")
                r.pending.append((str(target), uname))
                continue
            errors.append(f"[{dp_name}] mapping HTTP {res.status_code}: {body}")
            continue
        try:
            mapping = res.json()
        except ValueError:
            errors.append(f"[{dp_name}] mapping response is not JSON")
            continue

        blocking, ambiguous = find_unmapped(mapping)
        if blocking:
            errors.append(f"[{dp_name}] no match in target universe: {', '.join(blocking)[:300]}")
            continue
        if ambiguous:
            r.ambiguous.append(f"[{dp_name}] ambiguous mapping applied, please check: {', '.join(ambiguous)[:300]}")
        if not apply:
            r.applied += 1
            continue

        old_sql, _ = get_dataprovider_sql(server, headers, doc_id, dp_id)
        ok, detail = apply_mapping(server, headers, doc_id, dp_id, target, mapping, dbg)
        if not ok:
            errors.append(f"[{dp_name}] {detail}")
            continue
        r.applied += 1
        new_sql, _ = get_dataprovider_sql(server, headers, doc_id, dp_id)
        same, msg = compare_structure(old_sql, new_sql)
        dbg["sql_check"] = msg
        if same is False:
            sql_diffs.append(f"[{dp_name}] {msg}")
        elif same:
            sql_same += 1
        sql_checked += 1

    seen = set()
    r.pending = [p for p in r.pending if not (p[0] in seen or seen.add(p[0]))]
    if sql_checked:
        r.sql_summary = (f"🔎 SQL check: {sql_same}/{sql_checked} queries consistent"
                         + (f"; ⚠️ {len(sql_diffs)} changed: {' || '.join(sql_diffs)}" if sql_diffs else " ✅"))
    if errors:
        r.error = " | ".join(errors)
    elif r.applied == 0 and already == r.query_count:
        r.error = ALREADY_CONVERTED
    return r


def revert_mapping(server, headers, doc_id, old_map: dict):
    """Maps every data provider back to its recorded previous universe and saves.

    BO does not allow .unx → .unv, so this only works between universes of the same kind;
    restoring from a backup is the safer path.
    """
    dps, err = get_dataproviders(server, headers, doc_id)
    if err:
        return False, err
    errors, applied, already = [], 0, 0
    for dp in dps:
        dp_id, dp_name = str(dp.get("id")), dp.get("name", str(dp.get("id")))
        old = str(old_map.get(dp_id, "")).strip()
        if not old.isdigit():
            errors.append(f"[{dp_name}] previous universe id not recorded")
            continue
        if str(dp.get("dataSourceId")) == old:
            already += 1
            continue
        res = requests.get(_mapping_url(server, doc_id), headers=headers,
                           params={"originDataproviderIds": dp_id, "targetDatasourceId": int(old)},
                           verify=VERIFY_TLS, timeout=API_TIMEOUT)
        if res.status_code != 200:
            body = short(res)
            if "unx" in body and "unv" in body and "not allowed" in body.lower():
                errors.append(f"[{dp_name}] BO does not allow mapping .unx back to .unv")
            else:
                errors.append(f"[{dp_name}] mapping HTTP {res.status_code}: {body}")
            continue
        mapping = res.json()
        blocking, ambiguous = find_unmapped(mapping)
        if blocking or ambiguous:  # stricter than the forward direction on purpose
            errors.append(f"[{dp_name}] cannot map back cleanly: {', '.join(blocking + ambiguous)[:200]}")
            continue
        ok, detail = apply_mapping(server, headers, doc_id, dp_id, old, mapping)
        if ok:
            applied += 1
        else:
            errors.append(f"[{dp_name}] {detail}")

    if errors:
        return False, f"Not saved ({applied} mapped back, {already} already original): " + " | ".join(errors)
    if applied == 0:
        return True, f"All {already} queries already use the previous universe."
    saved, msg = save_document(server, headers, doc_id)
    if not saved:
        return False, f"Mapped back but not saved: {msg}"
    return True, f"{applied} queries mapped back and saved."
