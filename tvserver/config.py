"""Načtení konfigurace z config.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8090
    database: Path = Path("data/tv.db")
    caption_runtime: Path = Path("data/caption_runtime.json")
    check_interval_hours: float = 24
    hide_after_failures: int = 2
    repair_sources: list[str] = field(default_factory=list)
    blocked_hosts: list[str] = field(default_factory=list)
    block_ip_hosts: bool = True
    restream_hosts: list[str] = field(default_factory=list)
    catalog_api: str = "https://iptv-org.github.io/api"
    catalog_interval_hours: float = 168
    catalog_playlists: list[dict] = field(default_factory=list)
    wiki_dir: Path = Path("data/wiki")
    obsidian_vault: str = ""
    obsidian_prefix: str = "moje-tv/wiki"
    wiki_interval_hours: float = 24
    epg_interval_hours: float = 12
    epg_hours_ahead: float = 48
    epg_guides_index: str = "https://iptv-org.github.io/api/guides.json"
    epg_mjh_feeds: list[str] = field(default_factory=list)
    epg_map: dict[str, str] = field(default_factory=dict)
    base_dir: Path = Path(".")


def load(path: str | os.PathLike | None = None) -> Config:
    """Načte konfiguraci. Cesty v ní jsou relativní ke složce se souborem."""
    p = Path(path or os.environ.get("TVSERVER_CONFIG", "config.toml")).resolve()
    data = tomllib.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    cfg = Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})
    cfg.base_dir = p.parent
    db = Path(cfg.database)
    cfg.database = db if db.is_absolute() else cfg.base_dir / db
    runtime = Path(cfg.caption_runtime)
    cfg.caption_runtime = runtime if runtime.is_absolute() else cfg.base_dir / runtime
    w = Path(cfg.wiki_dir)
    cfg.wiki_dir = (w if w.is_absolute() else cfg.base_dir / w).resolve()
    return cfg
