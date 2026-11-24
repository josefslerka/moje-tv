"""Přeposílání streamů pro prohlížeč u kanálů, které nepovolují CORS.

Proxy není otevřená: přímo přepošle jen stream, který je v seznamu kanálů nebo
v katalogu. Adresy uvnitř playlistu (varianty, segmenty, klíče) přepíše na
podepsané odkazy zpět na sebe, takže jiné adresy než ty ze streamu odmítne.
Je určená jen pro domácí síť, server se nemá vystavovat do internetu.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from urllib.parse import quote, urljoin

import httpx

from . import db
from .checker import DEFAULT_UA

_URI_ATTR = re.compile(r'URI="([^"]+)"')


def _secret(con: sqlite3.Connection) -> bytes:
    s = db.get_meta(con, "proxy_secret")
    if not s:
        s = secrets.token_hex(32)
        db.set_meta(con, "proxy_secret", s)
        con.commit()
    return s.encode()


def _b64(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def _unb64(v: str) -> str:
    return base64.urlsafe_b64decode(v + "=" * (-len(v) % 4)).decode()


def sign(con: sqlite3.Connection, url: str) -> str:
    return hmac.new(_secret(con), url.encode(), hashlib.sha256).hexdigest()[:24]


def link(con: sqlite3.Connection, url: str) -> str:
    return f"/proxy?u={quote(_b64(url))}&s={sign(con, url)}"


def headers_for(con: sqlite3.Connection, url: str) -> dict | None:
    """Hlavičky pro známý stream, nebo None, když stream není v seznamu ani v katalogu."""
    row = (con.execute("SELECT user_agent, referrer FROM channels WHERE url = ?", (url,)).fetchone()
           or con.execute("SELECT user_agent, referrer FROM catalog WHERE url = ?", (url,)).fetchone())
    if row is None:
        return None
    h = {"User-Agent": row["user_agent"] or DEFAULT_UA}
    if row["referrer"]:
        h["Referer"] = row["referrer"]
    return h


def decode(con: sqlite3.Connection, u: str, s: str) -> tuple[str, bool]:
    """Vrátí cílovou adresu a jestli je podepsaná."""
    url = _unb64(u)
    return url, bool(s) and hmac.compare_digest(s, sign(con, url))


def rewrite(con: sqlite3.Connection, text: str, base: str) -> str:
    """Přepíše všechny adresy v playlistu HLS na podepsané odkazy přes proxy."""
    out = []
    for line in text.splitlines():
        t = line.strip()
        if not t:
            out.append(line)
        elif t.startswith("#"):
            out.append(_URI_ATTR.sub(lambda m: f'URI="{link(con, urljoin(base, m.group(1)))}"', line))
        else:
            out.append(link(con, urljoin(base, t)))
    return "\n".join(out) + "\n"


def is_playlist(content_type: str, head: bytes) -> bool:
    ct = content_type.lower()
    return "mpegurl" in ct or head.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"#EXTM3U")


async def fetch(client: httpx.AsyncClient, url: str, headers: dict) -> httpx.Response:
    req = client.build_request("GET", url, headers=headers)
    return await client.send(req, stream=True)
