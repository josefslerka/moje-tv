"""Katalog kanálů, ze kterého se vybírá na obrazovce Přidat."""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import sqlite3
from urllib.parse import urlparse

import httpx

from . import db, m3u
from .checker import base_id, check_stream, credential_url, is_allowed, is_restream
from .config import Config
from .epg import load_guides
from .metadata import MetadataIndex

log = logging.getLogger("tvserver.catalog")
_lock = asyncio.Lock()
IPTV_ORG = "iptv-org"
API_NAMES = ("channels", "streams", "feeds", "logos", "blocklist", "categories", "countries", "languages")
# iptv-org občas používá bibliografické kódy jazyků místo terminologických.
LANG_FIX = {"cze": "ces", "slo": "slk", "ger": "deu", "fre": "fra", "dut": "nld", "chi": "zho", "gre": "ell",
            "per": "fas", "rum": "ron", "arm": "hye", "baq": "eus", "bur": "mya", "geo": "kat", "ice": "isl",
            "mac": "mkd", "mao": "mri", "may": "msa", "tib": "bod", "wel": "cym", "alb": "sqi"}


CATEGORY_CS = {
    "adult": "Pro dospělé",
    "animation": "Animované", "auto": "Auto-moto", "business": "Byznys", "classic": "Klasika",
    "comedy": "Komedie", "cooking": "Vaření", "culture": "Kultura", "documentary": "Dokumenty",
    "education": "Vzdělávání", "entertainment": "Zábava", "family": "Rodina", "general": "Všeobecné",
    "interactive": "Interaktivní", "kids": "Děti", "legislative": "Parlament", "lifestyle": "Životní styl",
    "movies": "Filmy", "music": "Hudba", "news": "Zprávy", "outdoor": "Příroda a outdoor", "public": "Veřejnoprávní",
    "relax": "Relax", "religious": "Náboženství", "science": "Věda", "series": "Seriály", "shop": "Nákupy",
    "sports": "Sport", "travel": "Cestování", "weather": "Počasí",
}


# Přesné názvy žánrů používané dalšími playlisty. Zeměpisné skupiny (např.
# Andalucía nebo Español) nejsou žánry a zůstávají bez odhadu kategorie.
CATEGORY_GROUPS = {key.casefold(): [key] for key in CATEGORY_CS}
CATEGORY_GROUPS.update({label.casefold(): [key] for key, label in CATEGORY_CS.items()})
for _category, _labels in {
    "news": ("Zpravodajství", "Informativos", "National News", "Global News", "Local News"),
    "sports": ("Sporty", "Deportivos", "Deportivos Int.", "Sports On Now", "3X3 Basketball", "Bowling", "Pro Wrestling"),
    "kids": ("Dětské", "Infantiles"),
    "documentary": ("Dokumenty a fakta", "Documentaries"),
    "series": ("TV pořady", "TV Dramas"),
    "general": ("Generalistas",),
    "music": ("Musicales",),
    "religious": ("Religiosos",),
    "animation": ("Animated", "Anime"),
    "education": ("Educational",),
    "culture": ("Art",),
    "outdoor": ("Animals", "Environment"),
    "lifestyle": ("Health", "Home Improvement"),
    "cooking": ("Good Eats",),
    "movies": ("Movie Channels",),
    "entertainment": ("TV & Entertainment", "Black Entertainment", "Reality", "Game Show", "Games & Competition", "Pop Culture"),
}.items():
    CATEGORY_GROUPS.update({label.casefold(): [_category] for label in _labels})
CATEGORY_GROUPS.update({
    "business news": ["business", "news"], "science & nature": ["science", "outdoor"],
    "classic tv": ["classic", "series"], "kids & family": ["kids", "family"],
    "auto & motorsports": ["auto", "sports"], "children-music": ["kids", "music"],
})


def _langs(codes) -> str:
    out: list[str] = []
    for c in codes or []:
        c = LANG_FIX.get(c, c)
        if c not in out:
            out.append(c)
    return ",".join(out)


def _categories(channel: dict, fallback: list[str] = ()) -> str:
    categories = list(dict.fromkeys(channel.get("categories") or fallback))
    if channel.get("is_nsfw") and "adult" not in categories:
        categories.append("adult")
    return ",".join(categories)


def build_iptv_org(api: dict[str, list], guides: list[dict], cfg: Config,
                  metadata_index: MetadataIndex | None = None) -> list[dict]:
    """Spojí soubory API iptv-org do řádků katalogu a vyřadí nevhodné streamy."""
    channels = {c["id"]: c for c in api["channels"]}
    blocked = {b["channel"].lower() for b in api.get("blocklist", [])}
    langs: dict[tuple[str, str | None], list[str]] = {}
    feed_names: dict[tuple[str, str], str] = {}
    for f in api.get("feeds", []):
        feed_names[(f["channel"], f["id"])] = f.get("name") or f["id"]
        langs[(f["channel"], f["id"])] = f.get("languages") or []
        if f.get("is_main"):
            langs.setdefault((f["channel"], None), f.get("languages") or [])
    logos: dict[tuple[str, str | None], str] = {}
    for lg in api.get("logos", []):
        if lg.get("in_use", True) and lg.get("url"):
            logos.setdefault((lg["channel"], lg.get("feed")), lg["url"])
    with_epg = {g["channel"].lower() for g in guides if g.get("channel")}

    rows: dict[str, dict] = {}
    for s in api["streams"]:
        cid, url = s.get("channel") or "", s.get("url") or ""
        ch = channels.get(cid)
        if cid.lower() in blocked or (ch and ch.get("closed")):
            continue
        if not is_allowed(url, cfg) or url in rows:
            continue
        feed = s.get("feed")
        tvg_id = f"{cid}@{feed}" if cid and feed else cid
        method = "id"
        name = ""
        if ch is None:
            # Stream může mít vlastní název i bez záznamu stanice v channels.json.
            # Jednoznačné shody doplníme, ostatní ponecháme dohledatelné bez odhadů metadat.
            if credential_url(url):
                continue
            metadata_index = metadata_index or MetadataIndex(api)
            match = metadata_index.resolve(tvg_id, s.get("title") or "", url)
            if match.method == "excluded_by_source_rules":
                continue
            ch = match.channel or {}
            cid, tvg_id, method = ch.get("id") or cid, match.tvg_id, match.method
            feed = tvg_id.partition("@")[2] or None
            name = m3u.clean_name(s.get("title") or "")
        if cid.lower() in blocked:
            continue
        if not name:
            name = ch.get("name") or ""
            if name and feed and feed not in ("SD", "HD"):
                name += f" {feed_names.get((cid, feed), feed)}"
        if not name:
            continue
        rows[url] = {
            "url": url, "source": IPTV_ORG,
            "tvg_id": tvg_id, "name": name,
            "country": ch.get("country") or "",
            "categories": _categories(ch),
            "languages": _langs(langs.get((cid, feed)) or langs.get((cid, None))),
            "logo": logos.get((cid, feed)) or logos.get((cid, None)) or "",
            "quality": s.get("quality") or "",
            "labels": ",".join(s.get("labels") or []),
            "user_agent": s.get("user_agent") or "",
            "referrer": s.get("referrer") or "",
            "has_epg": int(bool(tvg_id) and base_id(tvg_id) in with_epg),
            "restream": int(is_restream(url, cfg)),
            "_metadata_method": method,
            "_metadata_matched": bool(ch),
        }
    return list(rows.values())


def build_playlist(entries: list[m3u.Entry], source: str, cfg: Config, api: dict[str, list], guides: list[dict],
                   skip_groups: list[str] = (), country: str = "", metadata_index: MetadataIndex | None = None) -> list[dict]:
    """Řádky katalogu z dalšího seznamu M3U (Free-TV, i.mjh.nz). Země, kategorie a jazyky
    doplní z iptv-org podle tvg-id, pak z atributů a skupin seznamu."""
    metadata_index = metadata_index or MetadataIndex(api, entries)
    blocked = {b["channel"].lower() for b in api.get("blocklist", [])}
    langs, main_langs, logos = {}, {}, {}
    for feed in api.get("feeds", []):
        key = feed["channel"].lower()
        langs[(key, feed["id"].lower())] = feed.get("languages") or []
        if feed.get("is_main"):
            main_langs.setdefault(key, feed.get("languages") or [])
    for logo in api.get("logos", []):
        if logo.get("in_use", True) and logo.get("url"):
            logos.setdefault((logo["channel"].lower(), (logo.get("feed") or "").lower()), logo["url"])
    codes = {c["code"] for c in api.get("countries", [])}
    by_name = {c["name"].lower(): c["code"] for c in api.get("countries", [])}
    language_codes = {row["code"] for row in api.get("languages", [])}
    language_names = {row["name"].casefold(): row["code"] for row in api.get("languages", [])}
    with_epg = {g["channel"].lower() for g in guides if g.get("channel")}

    rows: dict[str, dict] = {}
    for e in entries:
        if any(fnmatch.fnmatch(e.group, g) for g in skip_groups):
            continue
        if not is_allowed(e.url, cfg) or e.url in rows:
            continue
        own = (e.attrs.get("tvg-country") or "").split(";")[0].strip().upper()
        hint = next((value for value in (own, by_name.get(e.group.lower()), e.group.upper()) if value in codes), "")
        match = metadata_index.resolve(e.tvg_id, e.name, e.url, hint or country, bool(hint), e.attrs.get("tvg-name", ""), e.logo)
        b = base_id(match.tvg_id) if match.tvg_id else ""
        ch = match.channel or {}
        if b in blocked:
            continue
        cc = ch.get("country") or hint or country
        feed_id = match.tvg_id.partition("@")[2].lower()
        own_langs = []
        for value in (e.attrs.get("tvg-language") or "").replace(";", ",").split(","):
            value = value.strip()
            code = LANG_FIX.get(value.lower(), value.lower())
            if code in language_codes:
                own_langs.append(code)
            elif value.casefold() in language_names:
                own_langs.append(language_names[value.casefold()])
        fallback = CATEGORY_GROUPS.get(e.group.strip().casefold(), [])
        rows[e.url] = {
            "url": e.url, "source": source, "tvg_id": match.tvg_id, "name": m3u.clean_name(e.name),
            "country": cc, "categories": _categories(ch, fallback),
            "languages": _langs(langs.get((b, feed_id)) or main_langs.get(b) or own_langs),
            "logo": e.logo or logos.get((b, feed_id)) or logos.get((b, ""), ""), "quality": "",
            "labels": "Geo-blocked" if "Ⓖ" in e.name else "",
            "user_agent": e.user_agent, "referrer": e.referrer,
            "has_epg": int(bool(b) and b in with_epg), "restream": int(is_restream(e.url, cfg)),
            "_metadata_method": match.method, "_metadata_candidates": list(match.candidates),
            "_metadata_matched": match.channel is not None,
            "_metadata_explicit": (["country"] if hint else []) + (["languages"] if own_langs else [])
                                  + (["categories"] if fallback else []) + (["logo"] if e.logo else []),
        }
    return list(rows.values())


def _store(con: sqlite3.Connection, source: str, rows: list[dict], override: bool = True) -> None:
    """Uloží řádky jednoho zdroje. Vlastní seznamy (override) mají přednost před iptv-org:
    obnova iptv-org adresy, které už patří jinému zdroji, nepřepíše."""
    con.execute("DELETE FROM catalog WHERE source = ?", (source,))
    verb = "INSERT OR REPLACE" if override else "INSERT OR IGNORE"
    rows = [{"grp": "", **r} for r in rows]
    con.executemany(
        f"{verb} INTO catalog (url, source, tvg_id, name, country, categories, languages, logo, "
        "quality, labels, user_agent, referrer, has_epg, restream, grp) VALUES (:url, :source, :tvg_id, :name, :country, "
        ":categories, :languages, :logo, :quality, :labels, :user_agent, :referrer, :has_epg, :restream, :grp)", rows)


def fill_missing(row: dict, other: dict) -> None:
    """Doplní prázdné údaje stejné URL; rozdílné identity stanic se neslučují."""
    first, second = base_id(row.get("tvg_id", "")), base_id(other.get("tvg_id", ""))
    if first and second and first != second:
        return
    for field in ("country", "categories", "languages", "logo", "user_agent", "referrer"):
        if not row.get(field) and other.get(field):
            row[field] = other[field]


def playlist_metadata_index(api: dict[str, list], playlists: list[tuple[dict, list[m3u.Entry]]],
                            cfg: Config, guides: list[dict]) -> MetadataIndex:
    """Přesná URL propojí nezávisle určené položky; nejednoznačné názvy se nešíří dál."""
    index = MetadataIndex(api, [entry for _, entries in playlists for entry in entries])
    identified = []
    for pl, entries in playlists:
        identified.extend(build_playlist(entries, pl["name"], cfg, api, guides,
                                         pl.get("skip_groups", []), pl.get("country", ""), index))
    index.add_playlist_rows(identified)
    return index


async def load_api(client: httpx.AsyncClient, cfg: Config) -> dict[str, list]:
    async def get(name):
        response = await client.get(f"{cfg.catalog_api}/{name}.json")
        response.raise_for_status()
        return name, response.json()
    return dict(await asyncio.gather(*(get(name) for name in API_NAMES)))


async def refresh(con: sqlite3.Connection, cfg: Config) -> dict:
    """Stáhne API iptv-org a přestaví katalog."""
    if _lock.locked():
        return {"running": True}
    async with _lock:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
            api = await load_api(client, cfg)
            guides = await load_guides(client, cfg)

            async def get_list(pl):
                r = await client.get(pl["url"])
                r.raise_for_status()
                return m3u.parse_catalog(r.text)
            playlists = [pl for pl in cfg.catalog_playlists if pl.get("name") and pl.get("url")]
            downloaded = await asyncio.gather(*(get_list(pl) for pl in playlists), return_exceptions=True)
        valid_playlists = [(pl, result) for pl, result in zip(playlists, downloaded) if not isinstance(result, Exception)]
        metadata_index = await asyncio.to_thread(playlist_metadata_index, api, valid_playlists, cfg, guides)
        rows = await asyncio.to_thread(build_iptv_org, api, guides, cfg, metadata_index)
        previous = {row["url"]: dict(row) for row in con.execute("SELECT * FROM catalog")}
        # Další seznamy se smažou předem, aby jejich adresy nepřebíraly přednost před iptv-org.
        # Seznam, který se nepodařilo stáhnout, zůstane ve stavu z minula.
        extra: list[tuple[str, list[dict]]] = []
        for pl, res in zip(playlists, downloaded):
            if isinstance(res, Exception):
                log.warning("Seznam %s se nepodařilo stáhnout: %s", pl["name"], res)
                db.log(con, f"Katalog: seznam {pl['name']} se nepodařilo stáhnout ({type(res).__name__})")
                continue
            extra.append((pl["name"], build_playlist(res, pl["name"], cfg, api, guides,
                                                     pl.get("skip_groups", []), pl.get("country", ""), metadata_index)))
            con.execute("DELETE FROM catalog WHERE source = ?", (pl["name"],))
        names_now = [pl["name"] for pl in playlists]
        for old in set(json.loads(db.get_meta(con, "catalog_playlists") or "[]")) - set(names_now):
            con.execute("DELETE FROM catalog WHERE source = ?", (old,))
        db.set_meta(con, "catalog_playlists", json.dumps(names_now, ensure_ascii=False))
        # Neurčený stream API nesmí předběhnout stejnou URL s metadaty dalšího seznamu.
        extra_urls = {row["url"] for _, entries in extra for row in entries}
        rows = [row for row in rows if row["_metadata_matched"] or row["url"] not in extra_urls]
        by_url = {row["url"]: row for row in rows}
        for _, entries in extra:
            for row in entries:
                if row["url"] in by_url:
                    fill_missing(by_url[row["url"]], row)
                else:
                    by_url[row["url"]] = row
        for row in by_url.values():
            fill_missing(row, previous.get(row["url"], {}))
        _store(con, IPTV_ORG, rows, override=False)
        added = {}
        for name, r in extra:
            _store(con, name, r, override=False)
            added[name] = con.execute("SELECT COUNT(*) FROM catalog WHERE source = ?", (name,)).fetchone()[0]
        db.set_meta(con, "catalog_names", json.dumps({
            "countries": {c["code"]: {"name": c["name"], "flag": c.get("flag", "")} for c in api["countries"]},
            "categories": {c["id"]: CATEGORY_CS.get(c["id"], c["name"]) for c in api["categories"]},
            "languages": {lng["code"]: lng["name"] for lng in api["languages"]},
        }, ensure_ascii=False))
        db.set_meta(con, "last_catalog", db.now())
        db.log(con, f"Katalog: {len(rows)} streamů z iptv-org"
                    + "".join(f", {n} z {name}" for name, n in added.items()))
        con.commit()
        return {"streams": len(rows), "lists": added}


async def import_playlist(con: sqlite3.Connection, cfg: Config, url: str) -> dict:
    """Přidá do katalogu vlastní seznam M3U. Zablokovaní hostitelé se vynechají."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        r = await client.get(url)
        r.raise_for_status()
    entries = m3u.parse_catalog(r.text)
    source = urlparse(url).hostname or "vlastní"
    rows, skipped = [], 0
    for e in entries:
        if not is_allowed(e.url, cfg):
            skipped += 1
            continue
        rows.append({"url": e.url, "source": source, "tvg_id": e.tvg_id, "name": m3u.clean_name(e.name),
                     "country": "", "categories": "", "languages": "", "logo": e.logo, "quality": "",
                     "labels": "", "user_agent": e.user_agent, "referrer": e.referrer, "has_epg": 0,
                     "restream": int(is_restream(e.url, cfg))})
    _store(con, source, rows)
    db.log(con, f"Vlastní seznam {source}: {len(rows)} streamů, {skipped} vynecháno")
    con.commit()
    return {"source": source, "imported": len(rows), "skipped": skipped}


def names(con: sqlite3.Connection) -> dict:
    raw = db.get_meta(con, "catalog_names")
    result = json.loads(raw) if raw else {"countries": {}, "categories": {}, "languages": {}}
    result["categories"]["adult"] = CATEGORY_CS["adult"]
    return result


KEY = "CASE WHEN c.tvg_id != '' THEN c.tvg_id ELSE c.url END"


def _filters(q: str = "", country: str = "", category: str = "", language: str = "", source: str = "",
             epg_only: bool = False, restreams: bool = False, restreams_only: bool = False,
             exclude: str = "") -> tuple[list[str], list]:
    where, args = [], []
    if restreams_only:
        where.append("c.restream = 1")
    elif not restreams:
        where.append("c.restream = 0")
    for word in q.split():
        where.append("c.name LIKE ?"); args.append(f"%{word}%")
    if country and exclude != "country":
        where.append("c.country = ?"); args.append(country)
    if category and exclude != "category":
        where.append("INSTR(',' || c.categories || ',', ',' || ? || ',') > 0"); args.append(category)
    if language and exclude != "language":
        where.append("INSTR(',' || c.languages || ',', ',' || ? || ',') > 0"); args.append(language)
    if source and exclude != "source":
        where.append("c.source = ?"); args.append(source)
    if epg_only:
        where.append("c.has_epg = 1")
    return where, args


def facets(con: sqlite3.Connection, q: str = "", country: str = "", category: str = "", language: str = "",
           source: str = "", epg_only: bool = False, restreams: bool = False, restreams_only: bool = False) -> dict:
    """Počty kanálů při použití ostatních filtrů. Vlastní výběr dané nabídky se při počítání vynechá."""
    n = names(con)
    filters = dict(q=q, country=country, category=category, language=language, source=source,
                   epg_only=epg_only, restreams=restreams, restreams_only=restreams_only)

    def count(dimension: str, col: str) -> list[tuple[str, int]]:
        where, args = _filters(**filters, exclude=dimension)
        w = "WHERE " + " AND ".join(where) if where else ""
        channels: dict[str, set[str]] = {}
        for key, values in con.execute(f"SELECT {KEY}, c.{col} FROM catalog c {w}", args):
            for value in filter(None, values.split(",") if dimension != "source" else [values]):
                channels.setdefault(value, set()).add(key)
        # Nulový vybraný filtr musí zůstat viditelný, nesmí se sám změnit na „všechny“.
        if filters[dimension]:
            channels.setdefault(filters[dimension], set())
        return sorted(((value, len(keys)) for value, keys in channels.items()), key=lambda item: (-item[1], item[0]))

    where, args = _filters(**filters)
    w = "WHERE " + " AND ".join(where) if where else ""
    total, streams = con.execute(f"SELECT COUNT(DISTINCT {KEY}), COUNT(*) FROM catalog c {w}", args).fetchone()
    return {
        "countries": [{"id": k, "name": n["countries"].get(k, {}).get("name", k),
                       "flag": n["countries"].get(k, {}).get("flag", ""), "count": v} for k, v in count("country", "country")],
        "categories": [{"id": k, "name": n["categories"].get(k, k), "count": v} for k, v in count("category", "categories")],
        "languages": [{"id": k, "name": n["languages"].get(k, k), "count": v} for k, v in count("language", "languages")],
        "sources": [{"id": k, "count": v} for k, v in count("source", "source")],
        "total": total, "streams": streams,
        "catalog_total": con.execute(f"SELECT COUNT(DISTINCT {KEY}) FROM catalog c").fetchone()[0],
        "updated": db.get_meta(con, "last_catalog"),
    }


def search(con: sqlite3.Connection, q: str = "", country: str = "", category: str = "", language: str = "",
           source: str = "", epg_only: bool = False, limit: int = 60, offset: int = 0,
           restreams: bool = False, restreams_only: bool = False) -> dict:
    """Hledá v katalogu. Výsledky jsou seskupené po kanálech, každý se seznamem streamů.
    Restreamy jsou ve výsledcích jen na požádání, nebo samotné (restreams_only)."""
    where, args = _filters(q, country, category, language, source, epg_only, restreams, restreams_only)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    grouped = (f"SELECT {KEY} AS k, MAX(c.has_epg) AS e, MIN(c.name) AS nm FROM catalog c {w} GROUP BY k")
    total = con.execute(f"SELECT COUNT(*) FROM ({grouped})", args).fetchone()[0]
    keys = [r["k"] for r in con.execute(
        f"{grouped} ORDER BY e DESC, nm COLLATE NOCASE, k LIMIT ? OFFSET ?", args + [limit, offset])]
    if not keys:
        return {"total": total, "items": []}
    marks = ",".join("?" * len(keys))
    # Stejné filtry i pro streamy uvnitř kanálu, aby se nepřimíchaly jiné zdroje ani restreamy.
    extra = (" AND " + " AND ".join(where)) if where else ""
    rows = con.execute(f"SELECT c.*, {KEY} AS k FROM catalog c WHERE {KEY} IN ({marks}){extra} "
                       "ORDER BY c.quality DESC", keys + args).fetchall()
    in_list_url = {r["url"]: r["id"] for r in con.execute("SELECT id, url FROM channels")}
    in_list_id = {r["tvg_id"]: r["id"] for r in con.execute("SELECT id, tvg_id FROM channels WHERE tvg_id != ''")}
    n = names(con)
    by_key: dict[str, dict] = {}
    for r in rows:
        it = by_key.get(r["k"])
        if not it:
            cats = [x for x in r["categories"].split(",") if x]
            it = by_key[r["k"]] = {
                "key": r["k"], "name": r["name"], "group": r["grp"], "tvg_id": r["tvg_id"], "logo": r["logo"], "source": r["source"],
                "country": r["country"], "country_name": n["countries"].get(r["country"], {}).get("name", r["country"]),
                "flag": n["countries"].get(r["country"], {}).get("flag", ""),
                "categories": [n["categories"].get(c, c) for c in cats],
                "languages": [n["languages"].get(x, x) for x in r["languages"].split(",") if x],
                "has_epg": bool(r["has_epg"]), "channel_id": in_list_id.get(r["tvg_id"]) if r["tvg_id"] else None,
                "streams": [],
            }
        it["logo"] = it["logo"] or r["logo"]
        it["group"] = it["group"] or r["grp"]
        it["channel_id"] = it["channel_id"] or in_list_url.get(r["url"])
        it["streams"].append({"url": r["url"], "host": urlparse(r["url"]).hostname or "",
                              "quality": r["quality"], "labels": [x for x in r["labels"].split(",") if x],
                              "restream": bool(r["restream"])})
    for it in by_key.values():
        it["restream"] = all(s["restream"] for s in it["streams"])
        # Oficiální streamy mají v nabídce přednost před restreamy.
        it["streams"].sort(key=lambda s: s["restream"])
    return {"total": total, "items": [by_key[k] for k in keys if k in by_key]}


async def _check(con: sqlite3.Connection, client: httpx.AsyncClient, url: str) -> tuple[bool, str]:
    row = (con.execute("SELECT user_agent, referrer FROM channels WHERE url=?", (url,)).fetchone()
           or lookup(con, url))
    ok, detail = await check_stream(client, url, row["user_agent"] if row else "", row["referrer"] if row else "")
    db.record_check(con, url, ok, detail)
    con.commit()
    return ok, detail


async def resolve(con: sqlite3.Connection, urls: list[str]) -> list[dict]:
    """Otestuje streamy jednoho kanálu najednou. Funkční jsou v seznamu první."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=12) as client:
        async def one(u):
            ok, detail = await _check(con, client, u)
            return {"url": u, "ok": ok, "detail": detail}
        res = await asyncio.gather(*(one(u) for u in urls[:8]))
    return sorted(res, key=lambda r: not r["ok"])


def lookup(con: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM catalog WHERE url = ?", (url,)).fetchone()


async def test(con: sqlite3.Connection, url: str) -> tuple[bool, str]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=12) as client:
        return await _check(con, client, url)


def add_channel(con: sqlite3.Connection, url: str, name: str = "", group: str = "") -> int:
    """Přidá kanál z katalogu nebo vlastní odkaz. Vrací id kanálu."""
    row = lookup(con, url)
    nm = names(con)
    if row:
        cats = [c for c in row["categories"].split(",") if c]
        default_group = nm["categories"].get(cats[0], cats[0]) if cats else "Ostatní"
        if row["restream"]:
            default_group = "Restreamy"
        if row["grp"]:
            default_group = row["grp"]
        e = m3u.Entry(name=name or row["name"], url=url, tvg_id=row["tvg_id"], group=group or default_group,
                      logo=row["logo"], user_agent=row["user_agent"], referrer=row["referrer"])
        source = row["source"]
    else:
        if not name:
            raise ValueError("U vlastního odkazu je potřeba zadat název.")
        e = m3u.Entry(name=name, url=url, group=group or "Vlastní")
        source = "vlastní"
    db.import_entries(con, [e])
    cid = con.execute("SELECT id FROM channels WHERE url = ?", (url,)).fetchone()["id"]
    con.execute("UPDATE channels SET source = ?, enabled = 1 WHERE id = ?", (source, cid))
    db.log(con, f"Přidán kanál {e.name}", cid)
    con.commit()
    return cid
