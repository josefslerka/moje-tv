"""Živé vysílání z YouTube a Twitche přes yt-dlp.

Adresa stránky (třeba https://www.youtube.com/@WION/live) zůstává v seznamu kanálů.
Server z ní při přehrávání zjistí aktuální manifest HLS a na něj přesměruje.
Manifest platí několik hodin a je vázaný na veřejnou IP adresu, takže funguje
pro zařízení v domácí síti.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from urllib.parse import urlparse

log = logging.getLogger("tvserver.webstream")

HOSTS = ("youtube.com", "youtu.be", "twitch.tv")
DEFAULT_TTL = 3600
TIMEOUT = 60
_cache: dict[str, tuple[str, float]] = {}
_locks: dict[str, asyncio.Lock] = {}
_EXPIRE = re.compile(r"[/=&?]expire[/=](\d{10})")


def is_web(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in HOSTS)


def _ttl(manifest: str) -> float:
    """Jak dlouho manifest držet: do deseti minut před vypršením, nejdéle pět hodin."""
    m = _EXPIRE.search(manifest)
    if not m:
        return DEFAULT_TTL
    return max(60.0, min(int(m.group(1)) - time.time() - 600, 5 * 3600))


def _env() -> dict:
    # yt-dlp potřebuje na YouTube JavaScript (deno). Pod launchd PATH Homebrew neobsahuje.
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([env.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"])
    return env


async def _ytdlp(url: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-playlist", "-f", "bv*+ba/b",
        "--print", "%(formats.-1.manifest_url,url)s", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_env())
    try:
        out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("yt-dlp nestihl odpovědět")
    lines = [l for l in out.decode().splitlines() if l.startswith("http")]
    if proc.returncode != 0 or not lines:
        msg = err.decode().strip().splitlines()
        raise RuntimeError(msg[-1].removeprefix("ERROR: ") if msg else "vysílání neběží")
    return lines[0]


async def resolve(url: str) -> str:
    """Vrátí adresu manifestu HLS pro živé vysílání. Výsledek se drží v paměti."""
    hit = _cache.get(url)
    if hit and hit[1] > time.time():
        return hit[0]
    lock = _locks.setdefault(url, asyncio.Lock())
    async with lock:
        hit = _cache.get(url)
        if hit and hit[1] > time.time():
            return hit[0]
        manifest = await _ytdlp(url)
        _cache[url] = (manifest, time.time() + _ttl(manifest))
        return manifest
