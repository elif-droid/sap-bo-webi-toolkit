"""Pure helpers for working with data provider SQL (no network calls)."""

from __future__ import annotations

import html
import json
import re

from bo.text import norm


# ---------------------------------------------------------------------------
# Structural comparison
# ---------------------------------------------------------------------------
def split_top_level(text: str, seps: list[str]) -> list[str]:
    """Splits `text` on separators that are not inside parentheses.

    Not a real SQL parser — just enough to count SELECT columns and WHERE conditions.
    Word separators should be passed with surrounding spaces (" AND "), which naturally
    avoids matching the "AND" inside "BRAND".
    """
    parts, buf, depth, i = [], [], 0, 0
    upper = text.upper()
    seps_upper = sorted((s.upper() for s in seps), key=len, reverse=True)
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            match = next((s for s in seps_upper if upper.startswith(s, i)), None)
            if match:
                parts.append("".join(buf).strip())
                buf = []
                i += len(match)
                continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def summarize(sql: str | None) -> dict:
    """Rough structure of a query: SELECT column count, WHERE condition count, JOIN count."""
    if not sql:
        return {"select_cols": 0, "where_conditions": 0, "join_count": 0, "has_where": False, "length": 0}
    txt = sql.strip()
    upper = txt.upper()

    select_cols = 0
    sel, frm = upper.find("SELECT"), upper.find(" FROM ")
    if sel != -1 and frm > sel:
        select_cols = len(split_top_level(txt[sel + 6:frm], [","]))

    where_conditions, has_where = 0, False
    w = upper.find(" WHERE ")
    if w != -1:
        has_where = True
        end = len(txt)
        for stopper in (" GROUP BY ", " ORDER BY ", " HAVING "):
            si = upper.find(stopper, w)
            if si != -1:
                end = min(end, si)
        where_conditions = len(split_top_level(txt[w + 7:end], [" AND ", " OR "]))

    return {
        "select_cols": select_cols,
        "where_conditions": where_conditions,
        "join_count": upper.count(" JOIN "),
        "has_where": has_where,
        "length": len(txt),
    }


def compare_structure(old_sql: str | None, new_sql: str | None) -> tuple[bool | None, str]:
    """Compares the *shape* of two queries.

    Table and column names are expected to change when a report moves to a new universe,
    so the text is not compared. A changed number of columns, filters or joins, however,
    usually means something was lost in the mapping.
    Returns (same, message); `same` is None when one of the queries is missing.
    """
    if old_sql is None or new_sql is None:
        return None, "Could not compare: one of the queries could not be read."
    o, n = summarize(old_sql), summarize(new_sql)
    diffs = []
    if o["select_cols"] != n["select_cols"]:
        diffs.append(f"SELECT columns {o['select_cols']} → {n['select_cols']}")
    if o["has_where"] != n["has_where"]:
        diffs.append(f"WHERE clause {'present' if o['has_where'] else 'absent'} → "
                     f"{'present' if n['has_where'] else 'absent'}")
    elif o["where_conditions"] != n["where_conditions"]:
        diffs.append(f"WHERE conditions {o['where_conditions']} → {n['where_conditions']}")
    if o["join_count"] != n["join_count"]:
        diffs.append(f"JOINs {o['join_count']} → {n['join_count']}")
    if diffs:
        return False, "⚠️ SQL structure changed: " + "; ".join(diffs)
    return True, (f"✅ SQL structure consistent ({o['select_cols']} columns, "
                  f"{o['where_conditions']} conditions, {o['join_count']} joins)")


# ---------------------------------------------------------------------------
# Extracting SQL from API responses
# ---------------------------------------------------------------------------
def _looks_like_sql(s) -> bool:
    return isinstance(s, str) and bool(re.search(r"\bselect\b.+\bfrom\b", s, re.I | re.S))


def extract_sql(text: str | None) -> str | None:
    """Finds the SELECT ... FROM statement inside a JSON or XML data provider response."""
    t = text or ""
    try:
        found = []

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if str(k).lower() in ("query", "sql", "querystring", "statement", "$") and _looks_like_sql(v):
                        found.append(v)
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(json.loads(t))
        if found:
            return max(found, key=len).strip()
    except ValueError:
        pass
    for m in re.finditer(r"<(query|sql|statement|queryString)\b[^>]*>(.*?)</\1>", t, re.I | re.S):
        body = m.group(2)
        cdata = re.search(r"<!\[CDATA\[(.*?)\]\]>", body, re.S)
        s = html.unescape(cdata.group(1) if cdata else body).strip()
        if _looks_like_sql(s):
            return s
    if _looks_like_sql(t) and "<" not in t[:50]:
        return t.strip()
    return None


# ---------------------------------------------------------------------------
# Searching pulled SQL
# ---------------------------------------------------------------------------
STOP_WORDS = {
    "which", "where", "what", "report", "reports", "query", "queries", "sql", "in", "is", "are",
    "the", "a", "an", "of", "and", "or", "with", "used", "uses", "using", "contains", "find", "show",
    "does", "do", "any", "for", "to",
}


def _snorm(s) -> str:
    return norm(s).replace("_", " ")


def search_terms(q: str) -> list[str]:
    """Terms to search for. "Quoted phrases" stay together; filler words are dropped."""
    q = str(q or "")
    phrases = [_snorm(p) for p in re.findall(r'"([^"]+)"', q) if p.strip()]
    rest = re.sub(r'"[^"]+"', " ", q)
    tokens = [t for t in re.findall(r"[\w\.\-\[\]]+", _snorm(rest)) if t not in STOP_WORDS and len(t) > 1]
    return phrases + tokens


def search(results: list[dict], query: str, mode: str = "all") -> tuple[list[str], list[dict]]:
    """Searches report name + data provider name + SQL text.

    `results` items: {"name": str, "dps": [{"name": str, "sql": str}]}. mode: "all" | "any".
    Returns (terms, hits) where hits are {report, dp, count, lines[(no, text)]}.
    """
    terms = search_terms(query)
    hits = []
    if not terms:
        return terms, hits
    for r in results:
        for d in r.get("dps", []):
            sql = d.get("sql", "") or ""
            nsql = _snorm(sql)
            nname = _snorm(f"{r.get('name', '')} {d.get('name', '')}")
            present = [t for t in terms if t in nsql or t in nname]
            if not (len(present) == len(terms) if mode == "all" else present):
                continue
            lines = [(i, ln.strip()) for i, ln in enumerate(sql.splitlines(), 1)
                     if any(t in _snorm(ln) for t in terms)]
            hits.append({"report": r.get("name", ""), "dp": d.get("name", ""),
                         "count": sum(nsql.count(t) for t in terms), "lines": lines})
    hits.sort(key=lambda h: -h["count"])
    return terms, hits
