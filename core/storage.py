"""Local audit trail: CSV logs, pending conversions, rollback records, scan cache.

Streamlit serves every browser session from one process, so writes are guarded by a lock.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd

_LOCK = threading.Lock()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def append_csv(path: Path, row: dict) -> None:
    """Appends one row; the header is written with a BOM the first time so Excel opens it correctly."""
    df = pd.DataFrame([row])
    with _LOCK:
        first = not path.exists() or path.stat().st_size == 0
        df.to_csv(path, mode="w" if first else "a", header=first, index=False,
                  encoding="utf-8-sig" if first else "utf-8")


def write_csv(path: Path, df: pd.DataFrame) -> None:
    with _LOCK:
        df.to_csv(path, index=False, encoding="utf-8-sig")


class Store:
    def __init__(self, data_dir: Path):
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            data_dir = Path.cwd() / "bo_toolkit_data"
            data_dir.mkdir(exist_ok=True)
        self.dir = data_dir
        self.log = data_dir / "activity_log.csv"
        self.errors = data_dir / "error_tracker.csv"
        self.rollback = data_dir / "rollback_records.csv"
        self.pending = data_dir / "pending_conversions.csv"
        self.id_rules = data_dir / "id_rules.txt"
        self.rls_rules = data_dir / "rls_rules.json"
        self.rls_results = data_dir / "rls_results.csv"
        self.scan_cache = data_dir / "report_universe_cache.json"

    # --- activity -------------------------------------------------------
    def log_action(self, operator, bo_user, doc_id, doc_name, universe, status, query_count, detail):
        append_csv(self.log, {
            "Timestamp": now(), "Operator": operator, "BO user": bo_user, "Report ID": doc_id,
            "Report name": doc_name, "Target universe": universe, "Result": status,
            "Queries": query_count, "Detail": detail,
        })

    def track_error(self, doc_id, doc_name, operator, reason):
        df = read_csv(self.errors)
        if not df.empty and str(doc_id) in df["Report ID"].astype(str).values:
            return
        append_csv(self.errors, {"Timestamp": now(), "Report ID": doc_id, "Report name": doc_name,
                                 "Operator": operator, "Reason": reason, "Checked": False})

    def record_rollback(self, doc_id, doc_name, universe, old_map: dict, operator):
        append_csv(self.rollback, {"Timestamp": now(), "Report ID": doc_id, "Report name": doc_name,
                                   "New universe": universe, "Previous mapping (JSON)": json.dumps(old_map),
                                   "Operator": operator, "Reverted": False})

    def mark_reverted(self, doc_id):
        df = read_csv(self.rollback)
        if df.empty:
            return
        df.loc[df["Report ID"].astype(str) == str(doc_id), "Reverted"] = True
        write_csv(self.rollback, df)

    # --- pending --------------------------------------------------------
    def add_pending(self, doc_id, doc_name, operator, pending: list[tuple[str, str]]):
        df = read_csv(self.pending)
        existing = set()
        if not df.empty:
            existing = set(zip(df["Report ID"].astype(str), df["Universe ID"].astype(str)))
        for uid, uname in pending:
            if (str(doc_id), str(uid)) not in existing:
                append_csv(self.pending, {"Timestamp": now(), "Report ID": doc_id, "Report name": doc_name,
                                          "Waiting for universe": uname, "Universe ID": uid, "Operator": operator})

    def remove_pending(self, doc_id, universe_ids=None):
        df = read_csv(self.pending)
        if df.empty:
            return
        same = df["Report ID"].astype(str) == str(doc_id)
        keep = ~same
        if universe_ids is not None:
            keep |= same & ~df["Universe ID"].astype(str).isin([str(u) for u in universe_ids])
        write_csv(self.pending, df[keep])

    # --- id rules -------------------------------------------------------
    def load_id_rules_text(self) -> str:
        return self.id_rules.read_text(encoding="utf-8") if self.id_rules.exists() else ""

    def save_id_rules_text(self, text: str):
        self.id_rules.write_text(text, encoding="utf-8")

    @staticmethod
    def parse_id_rules(text: str) -> dict[str, str]:
        out = {}
        for line in (text or "").splitlines():
            if "=" in line:
                a, b = (x.strip() for x in line.split("=", 1))
                if a.isdigit() and b.isdigit():
                    out[a] = b
        return out

    # --- json helpers ---------------------------------------------------
    def load_json(self, path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def save_json(self, path: Path, data):
        with _LOCK:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
