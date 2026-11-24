"""Čtení a zápis seznamů M3U."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
# Ⓢ, Ⓖ, Ⓨ a podobné značky ze seznamu Free-TV (bez HD, geoblokace, YouTube).
_TAGS = re.compile(r"\s*(\(\d{3,4}p\)|\[[^\]]*\]|[\u24b6-\u24cf])")


def clean_name(name: str) -> str:
    """Odstraní technické značky, třeba '(1080p)' nebo '[Not 24/7]' z iptv-org a 'Ⓖ' z Free-TV."""
    return _TAGS.sub("", name).strip() or name


@dataclass
class Entry:
    name: str
    url: str
    tvg_id: str = ""
    group: str = ""
    logo: str = ""
    user_agent: str = ""
    referrer: str = ""
    attrs: dict = field(default_factory=dict, compare=False, repr=False)   # všechny atributy z #EXTINF


def parse(text: str) -> list[Entry]:
    """Rozparsuje M3U včetně voleb #EXTVLCOPT pro user-agent a referrer."""
    out: list[Entry] = []
    info: str | None = None
    ua = ref = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            info, ua, ref = line, "", ""
        elif line.startswith("#EXTVLCOPT:"):
            key, _, val = line[len("#EXTVLCOPT:"):].partition("=")
            if key == "http-user-agent":
                ua = val
            elif key == "http-referrer":
                ref = val
        elif not line.startswith("#") and info is not None:
            quoted = False
            for index, char in enumerate(info):
                if char == '"':
                    quoted = not quoted
                elif char == "," and not quoted:
                    head, name = info[:index], info[index + 1:]
                    break
            else:
                info = None
                continue
            attrs = dict(_ATTR.findall(head))
            out.append(Entry(
                name=name.strip(),
                url=line,
                tvg_id=attrs.get("tvg-id", ""),
                group=attrs.get("group-title", ""),
                logo=attrs.get("tvg-logo", ""),
                user_agent=ua or attrs.get("http-user-agent", ""),
                referrer=ref or attrs.get("http-referrer", ""),
                attrs=attrs,
            ))
            info = None
    return out


def parse_catalog(text: str) -> list[Entry]:
    """Načte neprázdný seznam kanálů; chybová stránka ani HLS nesmí nahradit katalog."""
    body = text.lstrip("\ufeff \t\r\n")
    lines = body.splitlines()
    if not lines or lines[0].split(maxsplit=1)[0] != "#EXTM3U":
        raise ValueError("Odpověď není seznam M3U.")
    if any(line.strip().startswith(("#EXT-X-STREAM-INF:", "#EXT-X-TARGETDURATION:")) for line in lines):
        raise ValueError("Odpověď je přehrávací playlist HLS, ne seznam kanálů.")
    entries = parse(body)
    if not entries or any(not entry.name for entry in entries):
        raise ValueError("Seznam M3U neobsahuje pojmenované kanály.")
    return entries


def _q(v: str) -> str:
    return v.replace('"', "'")


def render(entries: list[Entry], epg_url: str = "") -> str:
    """Vytvoří M3U, kterému rozumí VLC, iPlayTV, UHF i Channels."""
    # Různé aplikace čtou různé názvy atributu, proto oba.
    head = "#EXTM3U" + (f' x-tvg-url="{_q(epg_url)}" url-tvg="{_q(epg_url)}"' if epg_url else "")
    lines = [head]
    for e in entries:
        attrs = [f'tvg-id="{_q(e.tvg_id)}"', f'tvg-name="{_q(e.name)}"']
        if e.logo:
            attrs.append(f'tvg-logo="{_q(e.logo)}"')
        attrs.append(f'group-title="{_q(e.group or "Ostatní")}"')
        if e.user_agent:
            attrs.append(f'http-user-agent="{_q(e.user_agent)}"')
        if e.referrer:
            attrs.append(f'http-referrer="{_q(e.referrer)}"')
        lines.append(f"#EXTINF:-1 {' '.join(attrs)},{e.name}")
        if e.user_agent:
            lines.append(f"#EXTVLCOPT:http-user-agent={e.user_agent}")
        if e.referrer:
            lines.append(f"#EXTVLCOPT:http-referrer={e.referrer}")
        lines.append(e.url)
    return "\n".join(lines) + "\n"
