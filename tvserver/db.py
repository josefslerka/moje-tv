"""Databáze kanálů v SQLite."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .m3u import Entry, clean_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY,
    tvg_id      TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL,
    grp         TEXT NOT NULL DEFAULT '',
    logo        TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL UNIQUE,
    user_agent  TEXT NOT NULL DEFAULT '',
    referrer    TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    sort        INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'unknown',   -- unknown | ok | dead
    fail_count  INTEGER NOT NULL DEFAULT 0,
    last_check  TEXT,
    last_ok     TEXT,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    channel_id  INTEGER,
    message     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_checks (
    url         TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    last_check  TEXT NOT NULL,
    last_ok     TEXT,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS programmes (
    channel_id  INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    start       TEXT NOT NULL,   -- UTC ISO 8601
    stop        TEXT NOT NULL,
    title       TEXT NOT NULL,
    descr       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (channel_id, start)
);
CREATE TABLE IF NOT EXISTS epg_status (
    channel_id  INTEGER PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,   -- URL souboru XMLTV#id kanálu, nebo prázdné
    how         TEXT NOT NULL,   -- manual | auto | none
    count       INTEGER NOT NULL DEFAULT 0,
    updated     TEXT
);
CREATE TABLE IF NOT EXISTS catalog (
    url         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,             -- iptv-org | vlastní seznam (název hostitele)
    tvg_id      TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL,
    country     TEXT NOT NULL DEFAULT '',
    categories  TEXT NOT NULL DEFAULT '',  -- čárkami oddělené id kategorií
    languages   TEXT NOT NULL DEFAULT '',  -- čárkami oddělené kódy jazyků
    logo        TEXT NOT NULL DEFAULT '',
    quality     TEXT NOT NULL DEFAULT '',
    labels      TEXT NOT NULL DEFAULT '',
    user_agent  TEXT NOT NULL DEFAULT '',
    referrer    TEXT NOT NULL DEFAULT '',
    has_epg     INTEGER NOT NULL DEFAULT 0,
    restream    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS catalog_name ON catalog(name);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS caption_api_usage (
    id              TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    channel_id      INTEGER,
    channel_name    TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL,
    generation_id   TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    http_status     INTEGER,
    cost_nanos      INTEGER,
    input_tokens    INTEGER,
    output_tokens   INTEGER
);
CREATE INDEX IF NOT EXISTS caption_api_usage_started ON caption_api_usage(started_at);
CREATE TABLE IF NOT EXISTS recordings (
    id              TEXT PRIMARY KEY,
    channel_id      INTEGER,
    channel_name    TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    state           TEXT NOT NULL,
    video           INTEGER NOT NULL,
    text_mode       TEXT,
    max_seconds     INTEGER NOT NULL,
    duration        REAL NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT '',
    text_error      TEXT NOT NULL DEFAULT ''
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Doplní sloupce, které přibyly v novějších verzích."""
    cols = {r["name"] for r in con.execute("PRAGMA table_info(channels)")}
    if "source" not in cols:
        con.execute("ALTER TABLE channels ADD COLUMN source TEXT NOT NULL DEFAULT ''")
    if "epg" not in cols:   # ruční zdroj programu ve tvaru "URL#id"
        con.execute("ALTER TABLE channels ADD COLUMN epg TEXT NOT NULL DEFAULT ''")
    ccols = {r["name"] for r in con.execute("PRAGMA table_info(catalog)")}
    if "restream" not in ccols:
        con.execute("ALTER TABLE catalog ADD COLUMN restream INTEGER NOT NULL DEFAULT 0")
    if "grp" not in ccols:   # navržená skupina při přidání (u vlastních výběrů)
        con.execute("ALTER TABLE catalog ADD COLUMN grp TEXT NOT NULL DEFAULT ''")
    if "favorite" not in cols:
        con.execute("ALTER TABLE channels ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
    con.execute("INSERT OR IGNORE INTO stream_checks (url, status, last_check, last_ok, detail) "
                "SELECT url, status, last_check, last_ok, detail FROM channels WHERE last_check IS NOT NULL")
    con.commit()


def record_check(con: sqlite3.Connection, url: str, ok: bool, detail: str, checked_at: str | None = None) -> str:
    """Uloží skutečnou kontrolu URL i před přidáním kanálu. Neúspěch zachová poslední úspěch.

    Výsledky souběžných kontrol mohou dorazit v jiném pořadí, než skončily;
    starší výsledek nesmí přepsat novější stav. Transakci potvrzuje volající.
    """
    ts = checked_at or now()
    status = "ok" if ok else "dead"
    con.execute(
        "INSERT INTO stream_checks (url, status, last_check, last_ok, detail) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET "
        "status=CASE WHEN excluded.last_check >= stream_checks.last_check THEN excluded.status ELSE stream_checks.status END, "
        "detail=CASE WHEN excluded.last_check >= stream_checks.last_check THEN excluded.detail ELSE stream_checks.detail END, "
        "last_check=MAX(stream_checks.last_check, excluded.last_check), "
        "last_ok=CASE WHEN stream_checks.last_ok IS NULL OR excluded.last_ok > stream_checks.last_ok "
        "THEN excluded.last_ok ELSE stream_checks.last_ok END",
        (url, status, ts, ts if ok else None, detail),
    )
    if ok:
        con.execute("UPDATE channels SET last_ok=? WHERE url=? AND (last_ok IS NULL OR last_ok < ?)", (ts, url, ts))
    con.execute(
        "UPDATE channels SET status=?, fail_count=CASE WHEN ? THEN 0 ELSE fail_count + 1 END, last_check=?, detail=? "
        "WHERE url=? AND (last_check IS NULL OR last_check <= ?)", (status, ok, ts, detail, url, ts),
    )
    return ts


def import_entries(con: sqlite3.Connection, entries: list[Entry]) -> tuple[int, int]:
    """Přidá kanály. Existující (stejná URL nebo stejné tvg-id a jméno) aktualizuje."""
    added = updated = 0
    start = con.execute("SELECT COALESCE(MAX(sort), 0) FROM channels").fetchone()[0]
    for i, e in enumerate(entries, 1):
        row = con.execute(
            "SELECT id, url FROM channels WHERE url = ? OR (tvg_id != '' AND tvg_id = ? AND name = ?)",
            (e.url, e.tvg_id, e.name),
        ).fetchone()
        if row:
            cid = row["id"]
            con.execute(
                "UPDATE channels SET tvg_id=?, name=?, grp=?, logo=?, url=?, user_agent=?, referrer=? WHERE id=?",
                (e.tvg_id, e.name, e.group, e.logo, e.url, e.user_agent, e.referrer, row["id"]),
            )
            updated += 1
        else:
            cursor = con.execute(
                "INSERT INTO channels (tvg_id, name, grp, logo, url, user_agent, referrer, sort) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (e.tvg_id, e.name, e.group, e.logo, e.url, e.user_agent, e.referrer, start + i),
            )
            cid = cursor.lastrowid
            added += 1
        if not row or row["url"] != e.url:
            check = con.execute("SELECT * FROM stream_checks WHERE url=?", (e.url,)).fetchone()
            state = ((check["status"], int(check["status"] == "dead"), check["last_check"], check["last_ok"], check["detail"])
                     if check else ("unknown", 0, None, None, ""))
            con.execute("UPDATE channels SET status=?, fail_count=?, last_check=?, last_ok=?, detail=? WHERE id=?",
                        (*state, cid))
    con.commit()
    return added, updated


def visible(con: sqlite3.Connection, hide_after: int, group: str = "", favorites: bool = False) -> list[sqlite3.Row]:
    """Kanály, které se mají zobrazit: zapnuté a ne opakovaně mrtvé. Oblíbené jsou první."""
    where, args = ["enabled = 1", "fail_count < ?"], [hide_after]
    if group:
        where.append("grp = ?"); args.append(group)
    if favorites:
        where.append("favorite = 1")
    return con.execute(
        f"SELECT * FROM channels WHERE {' AND '.join(where)} ORDER BY favorite DESC, sort, id", args,
    ).fetchall()


def all_channels(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM channels ORDER BY sort, id").fetchall()


def to_entry(r: sqlite3.Row) -> Entry:
    return Entry(name=clean_name(r["name"]), url=r["url"], tvg_id=r["tvg_id"] or f"ch{r['id']}", group=r["grp"],
                 logo=r["logo"], user_agent=r["user_agent"], referrer=r["referrer"])


def log(con: sqlite3.Connection, message: str, channel_id: int | None = None) -> None:
    con.execute("INSERT INTO log (ts, channel_id, message) VALUES (?,?,?)", (now(), channel_id, message))
    con.execute("DELETE FROM log WHERE id <= (SELECT MAX(id) - 2000 FROM log)")


def get_meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
