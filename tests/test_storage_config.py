import json

from core.config import ROOT, load_config
from core.storage import Store, read_csv


def test_example_config_loads():
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.server_url.startswith("https://") and cfg.operators
    assert cfg.universe_names[cfg.universes["Sales"]] == "Sales"
    assert "brand" in cfg.rls_columns


def test_pending_add_dedupe_and_partial_remove(tmp_path):
    s = Store(tmp_path)
    s.add_pending("1", "Report", "Alice", [("10", "Sales"), ("20", "Finance")])
    s.add_pending("1", "Report", "Alice", [("10", "Sales")])
    assert len(read_csv(s.pending)) == 2
    s.remove_pending("1", universe_ids=["10"])
    left = read_csv(s.pending)
    assert list(left["Universe ID"].astype(str)) == ["20"]
    s.remove_pending("1")
    assert read_csv(s.pending).empty


def test_rollback_record_and_mark(tmp_path):
    s = Store(tmp_path)
    s.record_rollback("5", "R", "Sales", {"DP1": "10"}, "Bob")
    s.mark_reverted("5")
    df = read_csv(s.rollback)
    assert bool(df["Reverted"].iloc[0]) and json.loads(df["Previous mapping (JSON)"].iloc[0]) == {"DP1": "10"}


def test_error_tracker_keeps_one_row_per_report(tmp_path):
    s = Store(tmp_path)
    s.track_error("5", "R", "Bob", "first")
    s.track_error("5", "R", "Bob", "second")
    assert len(read_csv(s.errors)) == 1


def test_id_rules_parsing():
    assert Store.parse_id_rules("1=2\n bad \n3 = 4\nx=5") == {"1": "2", "3": "4"}
