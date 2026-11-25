"""Row-level security check: does a test user only see the rows they are allowed to see?

Rules are per user and per dimension (e.g. "brand", "region"):
    {"TEST_USER": {"brand": {"allow": ["A"], "deny": []}, "region": {"allow": [], "deny": ["NORTH"]}}}
- allow: every value seen must contain one of these words
- deny:  no value seen may contain any of these words
Matching is accent- and case-insensitive and uses "contains".
"""

from __future__ import annotations

from bo.text import norm_upper

ID_WORDS = {"ID", "KEY", "CODE", "NO", "NUM", "NUMBER"}


def _is_id_column(name: str) -> bool:
    words = norm_upper(name).replace("-", " ").replace("_", " ").split()
    return any(w in ID_WORDS for w in words)


def pick_column(columns: list[str], candidates: list[str]) -> str | None:
    """First column that matches one of the candidate names.

    ID/code columns are skipped because rules are written against names, not keys.
    Order: exact match, contains, contains ignoring spaces, then exact match including ID columns.
    """
    name_cols = {c: norm_upper(c) for c in columns if not _is_id_column(c)}
    for cand in candidates:
        cn = norm_upper(cand)
        for c, n in name_cols.items():
            if n == cn:
                return c
    for cand in candidates:
        cn = norm_upper(cand)
        for c, n in name_cols.items():
            if cn and cn in n:
                return c
    for cand in candidates:
        cn = norm_upper(cand).replace(" ", "")
        for c, n in name_cols.items():
            if cn and cn in n.replace(" ", ""):
                return c
    for cand in candidates:
        cn = norm_upper(cand)
        for c in columns:
            if norm_upper(c) == cn:
                return c
    return None


def check_rows(rows: list[dict], column: str | None, allow: list[str], deny: list[str], label: str) -> list[str]:
    """Violations for one dimension."""
    if not column:
        return []
    values = sorted({r.get(column, "") for r in rows if r.get(column, "")})
    allow_n = [norm_upper(a) for a in allow if a.strip()]
    deny_n = [(d, norm_upper(d)) for d in deny if d.strip()]
    out = []
    for v in values:
        nv = norm_upper(v)
        if allow_n and not any(a in nv for a in allow_n):
            out.append(f"{label} '{v}' not allowed (only {', '.join(allow)})")
        for raw, d in deny_n:
            if d in nv:
                out.append(f"{label} '{v}' is denied (contains '{raw}')")
    return out


def check_user(rows: list[dict], columns: list[str], user_rule: dict, column_candidates: dict) -> tuple[list[str], dict]:
    """Checks all dimensions of one user's rule. Returns (violations, {dimension: column_used})."""
    violations, used = [], {}
    for dim, cands in column_candidates.items():
        col = pick_column(columns, cands)
        used[dim] = col
        rule = user_rule.get(dim, {})
        violations += check_rows(rows, col, rule.get("allow", []), rule.get("deny", []), dim.upper())
    return violations, used
