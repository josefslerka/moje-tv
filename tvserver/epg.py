"""Programový průvodce: párování kanálů, stahování XMLTV a výstup pro aplikace."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import httpx

from . import db
from .checker import base_id
from .config import Config
from .m3u import clean_name

log = logging.getLogger("tvserver.epg")
_lock = asyncio.Lock()
GUIDES_MAX_AGE = 7 * 24 * 3600   # katalog iptv-org se stahuje nejvýš jednou týdně
MJH = "https://i.mjh.nz/{feed}.xml.gz"


# --- čas ---------------------------------------------------------------------

def parse_time(value: str) -> datetime:
    """XMLTV čas '20261103050000 +0100' -> datetime v UTC."""
    value = value.strip()
    stamp, _, tz = value.partition(" ")
    dt = datetime.strptime(stamp[:14], "%Y%m%d%H%M%S")
    if tz:
        sign = -1 if tz[0] == "-" else 1
        tz = tz.lstrip("+-")
        offset = timedelta(hours=int(tz[:2]), minutes=int(tz[2:4] or 0)) * sign
        return (dt - offset).replace(tzinfo=timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def fmt_time(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%Y%m%d%H%M%S +0000")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# --- párování kanálů se zdroji ---------------------------------------------

def resolve(channels: list[sqlite3.Row], cfg: Config, guides: list[dict]) -> dict[int, tuple[str, str, str]]:
    """Pro každý kanál najde (URL souboru, id v souboru, způsob). Ruční mapa má přednost."""
    manual = {k.split("@", 1)[0].lower(): v for k, v in cfg.epg_map.items()}
    auto: dict[str, dict[str, str]] = {}
    feeds = set(cfg.epg_mjh_feeds)
    for g in guides:
        if g.get("site") != "i.mjh.nz" or not g.get("channel"):
            continue
        feed, _, sid = (g.get("site_id") or "").partition("#")
        if feed in feeds and sid:
            auto.setdefault(g["channel"].lower(), {}).setdefault(feed, sid)

    out: dict[int, tuple[str, str, str]] = {}
    for ch in channels:
        own = ch["epg"] if "epg" in ch.keys() else ""
        if own and "#" in own:
            url, _, xid = own.rpartition("#")
            out[ch["id"]] = (url, xid, "manual")
            continue
        b = base_id(ch["tvg_id"]) if ch["tvg_id"] else ""
        if b in manual:
            url, _, xid = manual[b].rpartition("#")
            out[ch["id"]] = (url, xid, "manual")
            continue
        options = auto.get(b, {})
        feed = next((f for f in cfg.epg_mjh_feeds if f in options), None)
        if feed:
            out[ch["id"]] = (MJH.format(feed=feed), options[feed], "auto")
    return out


# --- stahování a čtení XMLTV -------------------------------------------------

def _cache_dir(cfg: Config) -> Path:
    d = cfg.database.parent / "epg_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _download(client: httpx.AsyncClient, url: str, dest: Path) -> Path:
    tmp = dest.with_suffix(".part")
    async with client.stream("GET", url) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            async for chunk in r.aiter_bytes():
                f.write(chunk)
    tmp.replace(dest)
    return dest


async def load_guides(client: httpx.AsyncClient, cfg: Config) -> list[dict]:
    path = _cache_dir(cfg) / "guides.json"
    if not path.exists() or time.time() - path.stat().st_mtime > GUIDES_MAX_AGE:
        try:
            await _download(client, cfg.epg_guides_index, path)
        except httpx.HTTPError as e:
            log.warning("Katalog průvodců se nepodařilo stáhnout: %s", e)
            if not path.exists():
                return []
    return json.loads(path.read_text(encoding="utf-8"))


def read_xmltv(path: Path, wanted: set[str], since: datetime, until: datetime) -> dict[str, list[tuple]]:
    """Projde soubor XMLTV (i zabalený gzipem) a vrátí pořady pro hledaná id."""
    with path.open("rb") as raw:
        magic = raw.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    out: dict[str, list[tuple]] = {w: [] for w in wanted}
    # Plex mění první část id ("region-kanál"), druhá zůstává. Když přesné id
    # v souboru není, použije se kanál se stejnou druhou částí.
    suffix = {w.rsplit("-", 1)[1]: w for w in wanted if "-" in w}
    present: set[str] = set()
    alias: dict[str, str] = {}
    with opener(path, "rb") as f:
        for _, el in ET.iterparse(f, events=("end",)):
            if el.tag != "programme":
                if el.tag == "channel":
                    cid = el.get("id", "")
                    present.add(cid)
                    tail = cid.rsplit("-", 1)[-1] if "-" in cid else None
                    if tail in suffix and suffix[tail] != cid:
                        alias.setdefault(cid, suffix[tail])
                    el.clear()
                continue
            cid = el.get("channel")
            if cid not in out and cid in alias and alias[cid] not in present:
                cid = alias[cid]
            if cid in out:
                try:
                    start, stop = parse_time(el.get("start", "")), parse_time(el.get("stop", ""))
                except ValueError:
                    el.clear()
                    continue
                if stop > since and start < until:
                    title = (el.findtext("title") or "").strip()
                    desc = (el.findtext("desc") or "").strip()
                    if title:
                        out[cid].append((iso(start), iso(stop), title, desc))
            el.clear()
    return out


# --- obnova ------------------------------------------------------------------

async def refresh(con: sqlite3.Connection, cfg: Config) -> dict:
    """Stáhne program pro všechny zapnuté kanály a uloží ho do databáze."""
    if _lock.locked():
        return {"running": True}
    async with _lock:
        channels = con.execute("SELECT * FROM channels WHERE enabled = 1").fetchall()
        now = datetime.now(timezone.utc)
        since, until = now - timedelta(hours=6), now + timedelta(hours=cfg.epg_hours_ahead)
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
            guides = await load_guides(client, cfg)
            mapping = resolve(channels, cfg, guides)
            by_url: dict[str, set[str]] = {}
            for url, xid, _ in mapping.values():
                by_url.setdefault(url, set()).add(xid)

            parsed: dict[tuple[str, str], list[tuple]] = {}
            failed: set[str] = set()
            for url, ids in by_url.items():
                dest = _cache_dir(cfg) / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".xml")
                try:
                    await _download(client, url, dest)
                except httpx.HTTPError as e:
                    log.warning("Program z %s se nepodařilo stáhnout: %s", url, e)
                    failed.add(url)
                    continue
                try:
                    res = await asyncio.to_thread(read_xmltv, dest, ids, since, until)
                except ET.ParseError as e:
                    log.warning("Soubor %s není platné XMLTV: %s", url, e)
                    failed.add(url)
                    continue
                for xid, progs in res.items():
                    parsed[(url, xid)] = progs

        ts = db.now()
        summary = {"channels": len(channels), "with_epg": 0, "programmes": 0, "failed_sources": len(failed)}
        for ch in channels:
            m = mapping.get(ch["id"])
            if not m:
                con.execute("INSERT OR REPLACE INTO epg_status (channel_id, source, how, count, updated) "
                            "VALUES (?, '', 'none', 0, ?)", (ch["id"], ts))
                continue
            url, xid, how = m
            if url in failed:
                continue   # starý program necháme, ať se nic neztratí kvůli výpadku zdroje
            progs = parsed.get((url, xid), [])
            con.execute("DELETE FROM programmes WHERE channel_id = ?", (ch["id"],))
            con.executemany("INSERT OR REPLACE INTO programmes (channel_id, start, stop, title, descr) "
                            "VALUES (?,?,?,?,?)", [(ch["id"], *p) for p in progs])
            con.execute("INSERT OR REPLACE INTO epg_status (channel_id, source, how, count, updated) "
                        "VALUES (?,?,?,?,?)", (ch["id"], f"{url}#{xid}", how, len(progs), ts))
            if progs:
                summary["with_epg"] += 1
                summary["programmes"] += len(progs)
        con.execute("DELETE FROM programmes WHERE stop < ?", (iso(since),))
        db.set_meta(con, "last_epg", ts)
        db.log(con, "Program: {with_epg} z {channels} kanálů, {programmes} pořadů".format(**summary))
        con.commit()
        return summary


# --- výstupy -----------------------------------------------------------------

def xmltv_id(ch: sqlite3.Row) -> str:
    return ch["tvg_id"] or f"ch{ch['id']}"


def build_xmltv(con: sqlite3.Connection, channels: list[sqlite3.Row]) -> str:
    """Soubor XMLTV s id kanálů shodnými s tvg-id v playlistu."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="tvserver">']
    for ch in channels:
        parts.append(f"  <channel id={quoteattr(xmltv_id(ch))}>"
                     f"<display-name>{escape(clean_name(ch['name']))}</display-name>"
                     + (f"<icon src={quoteattr(ch['logo'])}/>" if ch["logo"] else "") + "</channel>")
    ids = {ch["id"]: xmltv_id(ch) for ch in channels}
    if ids:
        q = f"SELECT * FROM programmes WHERE channel_id IN ({','.join('?' * len(ids))}) ORDER BY channel_id, start"
        for p in con.execute(q, list(ids)):
            parts.append(f'  <programme start="{fmt_time(p["start"])}" stop="{fmt_time(p["stop"])}" '
                         f"channel={quoteattr(ids[p['channel_id']])}>"
                         f"<title>{escape(p['title'])}</title>"
                         + (f"<desc>{escape(p['descr'])}</desc>" if p["descr"] else "") + "</programme>")
    parts.append("</tv>")
    return "\n".join(parts) + "\n"


def now_next(con: sqlite3.Connection, channel_ids: list[int]) -> dict[int, dict]:
    """Co běží teď a co bude dál, pro každý kanál."""
    t = iso(datetime.now(timezone.utc))
    out: dict[int, dict] = {}
    for cid in channel_ids:
        rows = con.execute("SELECT start, stop, title FROM programmes WHERE channel_id = ? AND stop > ? "
                           "ORDER BY start LIMIT 2", (cid, t)).fetchall()
        cur = rows[0] if rows and rows[0]["start"] <= t else None
        nxt = rows[1] if cur and len(rows) > 1 else (rows[0] if rows and not cur else None)
        out[cid] = {"now": dict(cur) if cur else None, "next": dict(nxt) if nxt else None}
    return out


def schedule(con: sqlite3.Connection, channel_id: int, limit: int = 12) -> list[dict]:
    t = iso(datetime.now(timezone.utc))
    return [dict(r) for r in con.execute(
        "SELECT start, stop, title, descr FROM programmes WHERE channel_id = ? AND stop > ? "
        "ORDER BY start LIMIT ?", (channel_id, t, limit))]
