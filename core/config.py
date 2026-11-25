"""Loads `config.toml` (falls back to `config.example.toml`).

Everything environment-specific — server URL, team members, universe ids — lives in
config.toml, which is git-ignored. The repository only ships the example file.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Config:
    server_url: str = ""
    operators: list[str] = field(default_factory=list)
    backup_folder_name: str = "Backup Reports"
    backup_folder_prefix: str = "Backup"
    universes: dict[str, str] = field(default_factory=dict)          # target .unx name -> id
    preferred_universes: dict[str, str] = field(default_factory=dict)
    date_start: str = "2026-01-01T00:00:00"
    date_end: str = "2026-01-31T23:59:59"
    date_end_keywords: tuple[str, ...] = ("end", "until", "finish")
    data_dir: Path = Path.home() / "Documents" / "BO_Toolkit_Data"
    rls_columns: dict[str, list[str]] = field(default_factory=dict)
    exclude_folders: str = "archive, backup"
    source: str = "defaults"

    @property
    def universe_names(self) -> dict[str, str]:
        """id -> name"""
        return {str(v): k for k, v in self.universes.items()}

    @property
    def unx_ids(self) -> set[str]:
        return {str(v) for v in self.universes.values()}


def load_config(path: Path | None = None) -> Config:
    candidates = [path] if path else [ROOT / "config.toml", ROOT / "config.example.toml"]
    for p in candidates:
        if p and p.exists():
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
            break
    else:
        return Config()

    server = raw.get("server", {})
    backup = raw.get("backup", {})
    conv = raw.get("conversion", {})
    storage = raw.get("storage", {})
    cfg = Config(
        server_url=server.get("url", ""),
        operators=list(raw.get("team", {}).get("operators", [])),
        backup_folder_name=backup.get("folder_name", "Backup Reports"),
        backup_folder_prefix=backup.get("folder_prefix", "Backup"),
        universes={str(k): str(v) for k, v in raw.get("universes", {}).items()},
        preferred_universes={str(k): str(v) for k, v in raw.get("universes_preferred", {}).items()},
        date_start=conv.get("date_start", Config.date_start),
        date_end=conv.get("date_end", Config.date_end),
        date_end_keywords=tuple(conv.get("date_end_keywords", Config.date_end_keywords)),
        rls_columns={k: list(v) for k, v in raw.get("rls", {}).get("columns", {}).items()},
        exclude_folders=raw.get("inventory", {}).get("exclude_folders", "archive, backup"),
        source=p.name,
    )
    if storage.get("data_dir"):
        cfg.data_dir = Path(storage["data_dir"]).expanduser()
    return cfg
