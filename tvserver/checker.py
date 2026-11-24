"""Kontrola streamů a automatická oprava nefunkčních adres."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import sqlite3
from collections import defaultdict
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from . import db, webstream
from .config import Config
from .m3u import Entry, parse

log = logging.getLogger("tvserver.checker")

DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
MAX_BYTES = 256 * 1024
CONCURRENCY = 10
_lock = asyncio.Lock()


async def _fetch(client: httpx.AsyncClient, url: str, headers: dict) -> tuple[int, str, str]:
    """Stáhne začátek odpovědi. Vrací status, finální URL a text."""
    async with client.stream("GET", url, headers=headers) as r:
        buf = bytearray()
        async for chunk in r.aiter_bytes():
            buf += chunk
            if len(buf) >= MAX_BYTES:
                break
        return r.status_code, str(r.url), buf.decode("utf-8", errors="replace")


def _first_variant(text: str) -> str | None:
    lines = [l.strip() for l in text.splitlines()]
    for i, l in enumerate(lines):
        if l.startswith("#EXT-X-STREAM-INF"):
            for nxt in lines[i + 1:]:
                if nxt and not nxt.startswith("#"):
                    return nxt
    return None


async def check_stream(client: httpx.AsyncClient, url: str, user_agent: str = "",
                       referrer: str = "") -> tuple[bool, str]:
    """Ověří, že stream vrací živý playlist HLS (nebo manifest DASH)."""
    headers = {"User-Agent": user_agent or DEFAULT_UA}
    if referrer:
        headers["Referer"] = referrer
    if webstream.is_web(url):
        try:
            url = await webstream.resolve(url)
        except Exception as e:  # noqa: BLE001
            return False, f"živé vysílání: {e}"[:160]
    try:
        status, final, text = await _fetch(client, url, headers)
        if status != 200:
            return False, f"HTTP {status}"
        body = text.lstrip("﻿ \r\n\t")
        if "<MPD" in body[:2000]:
            return True, "DASH"
        if not body.startswith("#EXTM3U"):
            return False, "odpověď není playlist"
        variant = _first_variant(body)
        if variant:
            vurl = urljoin(final, variant)
            status, _, vtext = await _fetch(client, vurl, headers)
            if status != 200:
                return False, f"varianta HTTP {status}"
            if "#EXTINF" in vtext or "#EXT-X-TARGETDURATION" in vtext:
                return True, "HLS"
            return False, "varianta bez segmentů"
        if "#EXTINF" in body:
            return True, "HLS"
        return False, "prázdný playlist"
    except httpx.TimeoutException:
        return False, "časový limit"
    except httpx.HTTPError as e:
        return False, type(e).__name__
    except Exception as e:  # noqa: BLE001 - kontrola nesmí spadnout kvůli jednomu kanálu
        return False, f"chyba: {type(e).__name__}"


def is_allowed(url: str, cfg: Config) -> bool:
    """Smí se adresa převzít jako náhrada? Blokuje uniklé a pirátské zdroje."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    for b in cfg.blocked_hosts:
        b = b.lower()
        if host == b or host.endswith("." + b):
            return False
    if cfg.block_ip_hosts:
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
    return True


def credential_url(url: str) -> bool:
    """Rozpozná přihlašovací odkazy, včetně účtů Xtream ve veřejných seznamech."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    keys = {key.lower() for key in parse_qs(parsed.query)}
    return bool(parsed.username or parsed.password
                or host == "hotiptv.site" or host.endswith(".hotiptv.site")
                or {"username", "password"} <= keys
                or re.fullmatch(r"/(?:live/|movie/|series/)?[^/]+/[^/]+/\d+(?:\.(?:ts|m3u8|mp4))?", parsed.path))


def _host_in(host: str, hosts: list[str]) -> bool:
    return any(host == h.lower() or host.endswith("." + h.lower()) for h in hosts)


def is_restream(url: str, cfg: Config) -> bool:
    """Je adresa z cizího serveru, který přeposílá kanály jiných stanic?"""
    return _host_in((urlparse(url).hostname or "").lower(), cfg.restream_hosts)


def base_id(tvg_id: str) -> str:
    """iptv-org přidává za tvg-id variantu, třeba 'EbS.be@SD'. Porovnává se základ."""
    return tvg_id.split("@", 1)[0].lower()


async def load_candidates(client: httpx.AsyncClient, cfg: Config) -> dict[str, list[Entry]]:
    idx: dict[str, list[Entry]] = defaultdict(list)
    for src in cfg.repair_sources:
        try:
            r = await client.get(src, timeout=60)
            r.raise_for_status()
            for e in parse(r.text):
                if e.tvg_id:
                    idx[base_id(e.tvg_id)].append(e)
        except httpx.HTTPError as e:
            log.warning("Nepodařilo se stáhnout zdroj náhrad %s: %s", src, e)
    return idx


async def _repair(client, cfg, row, candidates, used: set[str],
                  checks: list[tuple[str, bool, str, str]]) -> tuple[Entry, str] | None:
    for cand in candidates.get(base_id(row["tvg_id"]), []):
        if cand.url == row["url"] or cand.url in used or not is_allowed(cand.url, cfg) or is_restream(cand.url, cfg):
            continue
        ok, detail = await check_stream(client, cand.url, cand.user_agent, cand.referrer)
        checked_at = db.now()
        checks.append((cand.url, ok, detail, checked_at))
        if ok:
            return cand, checked_at
    return None


async def run_check(con: sqlite3.Connection, cfg: Config, repair: bool = True) -> dict:
    """Zkontroluje všechny zapnuté kanály. Mrtvé zkusí opravit podle tvg-id."""
    if _lock.locked():
        return {"running": True}
    async with _lock:
        rows = con.execute("SELECT * FROM channels WHERE enabled = 1 ORDER BY sort, id").fetchall()
        sem = asyncio.Semaphore(CONCURRENCY)
        limits = httpx.Limits(max_connections=CONCURRENCY * 2)
        async with httpx.AsyncClient(follow_redirects=True, timeout=12, limits=limits) as client:

            async def one(r):
                async with sem:
                    ok, detail = await check_stream(client, r["url"], r["user_agent"], r["referrer"])
                    return r, ok, detail, db.now()

            results = await asyncio.gather(*(one(r) for r in rows))
            dead = [r for r, ok, _, _ in results if not ok]
            candidates = await load_candidates(client, cfg) if repair and dead else {}

            # Síťové operace dokončíme před zápisy, aby opravy nedržely SQLite transakci otevřenou.
            used = {r["url"] for r in con.execute("SELECT url FROM channels")}
            repairs, replacement_checks = {}, []
            for r, ok, _, _ in results:
                if not ok and candidates and r["tvg_id"]:
                    async with sem:
                        fixed = await _repair(client, cfg, r, candidates, used, replacement_checks)
                    if fixed:
                        repairs[r["id"]] = fixed
                        used.add(fixed[0].url)

            for url, ok, detail, checked_at in replacement_checks:
                db.record_check(con, url, ok, detail, checked_at)
            summary = {"checked": len(rows), "ok": 0, "dead": 0, "repaired": 0, "hidden": 0}
            for r, ok, detail, checked_at in results:
                db.record_check(con, r["url"], ok, detail, checked_at)
                if ok:
                    summary["ok"] += 1
                    continue
                fixed = repairs.get(r["id"])
                if fixed:
                    replacement, repaired_at = fixed
                    con.execute("UPDATE channels SET url=?, user_agent=?, referrer=? WHERE id=?",
                                (replacement.url, replacement.user_agent, replacement.referrer, r["id"]))
                    db.record_check(con, replacement.url, True, "opraveno", repaired_at)
                    db.log(con, f"{r['name']}: nová adresa {replacement.url} (stará: {r['url']}, {detail})", r["id"])
                    summary["repaired"] += 1
                    summary["ok"] += 1
                    continue
                fails = r["fail_count"] + 1
                summary["dead"] += 1
                if fails == cfg.hide_after_failures:
                    db.log(con, f"{r['name']}: skryto po {fails} neúspěšných kontrolách ({detail})", r["id"])
                    summary["hidden"] += 1
            db.set_meta(con, "last_check", db.now())
            db.log(con, "Kontrola: {checked} kanálů, {ok} v pořádku, {repaired} opraveno, "
                        "{dead} nefunkčních".format(**summary))
            con.commit()
            return summary
